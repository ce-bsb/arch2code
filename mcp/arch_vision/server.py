#!/usr/bin/env python3
"""
arch_vision — the MCP server that gives IBM Bob the ability to READ architecture
images using the multimodal models on watsonx.ai.

WHY THIS SERVER EXISTS (read this before replacing it with something else)
-------------------------------------------------------------------------
Bob does not ingest images through the normal path:
  - context mentions (@file): the docs are explicit — "binary files not supported"
  - read_file: extracts text from .docx/.pdf/.xlsx, it does not interpret pixels
  - Bob's tool list (read/search/list/write/apply_diff/execute_command/
    use_mcp_tool/switch_mode/ask_followup_question) has no vision tool at all

In other words: "@/drawing.png explain this architecture" does NOT work. The
supported way to get pixels inside Bob is MCP — the `mcp` tool group and
`use_mcp_tool`. That is what this server does.

Good side effect: interpreting the drawing becomes an explicit, auditable call,
with a versioned model and a versioned prompt. With a regulated customer that is
worth more than the convenience of dragging the image into the chat.

TOOLS
  arch_vision_list_intake        lists artifacts and the extraction path for each
  arch_vision_describe_diagram   free-form technical description (exploration)
  arch_vision_extract_architecture  structured extraction (components/connections)
  arch_vision_verify_element     independent verification of ONE claim

The split between extract and verify is deliberate: different prompts, different
passes, different framings. Asking "what do you see?" and then "is there really an
arrow from A to B?" produces decorrelated error. Asking again the same way only
confirms the same bias — that is why arch-critic uses verify, not extract.

CONFIGURATION: see .env.example
"""

import base64
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("arch_vision")

# --------------------------------------------------------------------------- #
# Self-configuration (do NOT depend on what Bob injects)
# --------------------------------------------------------------------------- #
# Bob's docs only show literal values in `env` and `cwd` of mcp.json; ${env:VAR}
# and ${workspaceFolder} are VS Code conventions and are NOT documented for Bob.
# Rather than bet on that, the server:
#   1. reads the .env sitting next to it   -> the apikey never enters a versioned file
#   2. derives the project root from __file__ -> a wrong `cwd` in mcp.json breaks nothing
# Practical effect: mcp.json carries no secret and no machine-specific path.

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parents[1]          # <root>/mcp/arch_vision/server.py


def _load_dotenv(path: Path) -> None:
    """Minimal .env parser. No dependency: one more dep is one more failure when
    bringing the server up inside Bob, where the traceback stays hidden."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        # The real environment wins over .env: allows overriding per session.
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(SERVER_DIR / ".env")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
WATSONX_URL = os.getenv("WATSONX_URL", "https://us-south.ml.cloud.ibm.com").rstrip("/")
WATSONX_APIKEY = os.getenv("WATSONX_APIKEY", "")
WATSONX_PROJECT_ID = os.getenv("WATSONX_PROJECT_ID", "")
WATSONX_SPACE_ID = os.getenv("WATSONX_SPACE_ID", "")
IAM_URL = os.getenv("IAM_URL", "https://iam.cloud.ibm.com/identity/token")

# Check the id in YOUR project's catalog before running: the watsonx.ai catalog
# changes from version to version and varies by region. A wrong id returns 404,
# not an obvious model error.
# Default: llama-4-maverick. IBM docs ("Third-party foundation models"): the
# llama-4 family is natively multimodal (MoE, early fusion), "optimized for visual
# recognition, image reasoning and captioning". Maverick has 128 experts (Scout
# has 16) — better on a dense diagram.
#
# Do NOT use llama-3-2-*-vision: it was withdrawn from several watsonx projects
# (it shows up with a red icon in the catalog). A multimodal model does not always
# have "vision" in its name — confirm YOUR catalog with:
# python3 mcp/arch_vision/preflight.py --probe
VISION_MODEL_ID = os.getenv("WATSONX_VISION_MODEL_ID",
                            "meta-llama/llama-4-maverick-17b-128e-instruct-fp8")
API_VERSION = os.getenv("WATSONX_API_VERSION", "2024-10-08")
TIMEOUT = float(os.getenv("ARCH_VISION_TIMEOUT", "120"))
MAX_IMAGE_MB = float(os.getenv("ARCH_VISION_MAX_IMAGE_MB", "5"))

_inbox_env = os.getenv("ARCH_INTAKE_INBOX", ".arch/intake/inbox")
_inbox_path = Path(_inbox_env)
# A relative path resolves from the PROJECT ROOT, not from the process cwd:
# we do not know which cwd Bob starts the server with.
INBOX = _inbox_path if _inbox_path.is_absolute() else (PROJECT_ROOT / _inbox_path)

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
DETERMINISTIC_EXT = {".drawio", ".xml", ".puml", ".mmd", ".md", ".json", ".yaml", ".yml"}

_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


# --------------------------------------------------------------------------- #
# Infra
# --------------------------------------------------------------------------- #
class ArchVisionError(RuntimeError):
    """An error with an actionable message — the agent needs to know what to do next."""


def _check_config() -> None:
    """Report ALL the missing configuration at once.

    Reporting one variable at a time puts the agent in a loop: fix one, hit the
    next, fix it, hit the next. A complete message settles it in one round — the
    same reason a good compiler lists every error.
    """
    missing = []
    if not WATSONX_APIKEY:
        missing.append("WATSONX_APIKEY")
    if not (WATSONX_PROJECT_ID or WATSONX_SPACE_ID):
        missing.append("WATSONX_PROJECT_ID (or WATSONX_SPACE_ID)")
    if not missing:
        return
    raise ArchVisionError(
        f"Missing configuration: {', '.join(missing)}. "
        f"Copy mcp/arch_vision/.env.example to .env, fill it in, and reference it in "
        f"`env` in .bob/mcp.json (Settings -> MCP -> Edit Project MCP). "
        f"In the meantime: if the artifact has a structured source (.drawio/.puml/.mmd), "
        f"the vision path is unnecessary — run parse_drawio.py."
    )


def _iam_token() -> str:
    """Cached IAM token. Renews 60s before expiry."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    _check_config()

    r = httpx.post(
        IAM_URL,
        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey",
              "apikey": WATSONX_APIKEY},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if r.status_code != 200:
        raise ArchVisionError(
            f"IAM rejected the apikey (HTTP {r.status_code}). Check that the key is "
            f"valid and has access to the watsonx project. Response: {r.text[:200]}")
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    return _token_cache["token"]


def _encode_image(path_str: str) -> str:
    """Read the image and return a data URL. Validates type and size with an
    actionable error."""
    p = Path(path_str)
    # The agent passes the path as it typed it in the chat (almost always relative
    # to the root). Without this, the server only finds the file if the cwd happens
    # to match by luck.
    if not p.is_absolute() and not p.exists():
        candidate = PROJECT_ROOT / p
        if candidate.exists():
            p = candidate
    if not p.exists():
        hint = ""
        if INBOX.exists():
            names = [f.name for f in INBOX.iterdir() if f.is_file()][:8]
            hint = f" Available in {INBOX}: {names}" if names else ""
        raise ArchVisionError(f"File not found: {path_str}.{hint}")

    if p.suffix.lower() in DETERMINISTIC_EXT:
        raise ArchVisionError(
            f"'{p.name}' is a structured source. Do not use vision here: run "
            f".bob/skills/diagram-intake/scripts/parse_drawio.py for an exact "
            f"reading, with no inference cost and no hallucination risk.")

    mime = mimetypes.guess_type(str(p))[0]
    if mime not in ALLOWED_IMAGE_TYPES:
        raise ArchVisionError(
            f"Type '{mime}' is not supported (accepted: {sorted(ALLOWED_IMAGE_TYPES)}). "
            f"Run capture_diagram.py to normalize it to PNG first.")

    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > MAX_IMAGE_MB:
        raise ArchVisionError(
            f"The image is {size_mb:.1f} MB and exceeds the {MAX_IMAGE_MB} MB limit. "
            f"Run capture_diagram.py, which resizes to <=1568px.")

    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _chat(messages: List[Dict[str, Any]], max_tokens: int, temperature: float) -> str:
    """Call the watsonx.ai chat endpoint."""
    _check_config()

    body: Dict[str, Any] = {
        "model_id": VISION_MODEL_ID,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if WATSONX_PROJECT_ID:
        body["project_id"] = WATSONX_PROJECT_ID
    else:
        body["space_id"] = WATSONX_SPACE_ID

    r = httpx.post(
        f"{WATSONX_URL}/ml/v1/text/chat?version={API_VERSION}",
        headers={"Authorization": f"Bearer {_iam_token()}",
                 "Content-Type": "application/json", "Accept": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        raise ArchVisionError(
            f"HTTP 404 for model_id='{VISION_MODEL_ID}'. That id probably does not "
            f"exist in your region/project. List the catalog at "
            f"{WATSONX_URL}/ml/v1/foundation_model_specs?version={API_VERSION} and "
            f"adjust WATSONX_VISION_MODEL_ID.")
    if r.status_code != 200:
        raise ArchVisionError(f"watsonx.ai HTTP {r.status_code}: {r.text[:400]}")

    try:
        return r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise ArchVisionError(f"Unexpected response from watsonx.ai: {e} :: {r.text[:300]}")


def _parse_json(text: str) -> Any:
    """Extract JSON from a response that may arrive with a markdown fence or a preamble."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(),
                  flags=re.MULTILINE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: first balanced object
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ArchVisionError(
        "The model did not return valid JSON. That usually means an illegible image "
        "or a model with no multimodal capability. First 300 chars: " + text[:300])


def _msg(prompt: str, image_url: Optional[str] = None) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    return [{"role": "user", "content": content}]



def _erro_json(e: Exception, tool: str) -> str:
    """Every error becomes actionable JSON. Never let an exception escape a tool.

    If one escapes, FastMCP returns non-JSON content and the agent gets a traceback
    instead of knowing what to do. Network errors are the most common case on a
    corporate network (proxy/VPN) and were exactly what used to escape."""
    import ssl as _ssl

    import httpx as _h
    if isinstance(e, ArchVisionError):
        msg = str(e)
    elif "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(e, _ssl.SSLError):
        # A corporate proxy doing TLS inspection re-signs the certificate. Python
        # does not trust the internal CA and refuses the connection. This is not VPN
        # and not firewall: it is the certificate chain — the fix is a different one,
        # and without this hint people spend hours poking at the VPN for nothing.
        # Common on bank networks and on IBM's own.
        msg = ("TLS refused: the certificate was not validated "
               "(CERTIFICATE_VERIFY_FAILED). This is a corporate proxy doing TLS "
               "inspection, not VPN and not firewall. Point Python at your company's "
               "CA before starting Bob:\n"
               "  export SSL_CERT_FILE=/path/corporate-ca.pem\n"
               "  export REQUESTS_CA_BUNDLE=/path/corporate-ca.pem\n"
               "Or install the CA into certifi:  python3 -m certifi  shows the bundle "
               "in use. NEVER disable verification on a customer network. "
               f"Detail: {e}")
    elif isinstance(e, (_h.ConnectError, _h.ConnectTimeout)):
        msg = (f"Could not reach {WATSONX_URL} (or IAM). Typical causes: corporate "
               f"VPN off, a proxy that needs configuring (HTTPS_PROXY), or a firewall "
               f"blocking outbound traffic. Detail: {e}. "
               f"In the meantime, the deterministic path (.drawio/.puml/.mmd) does "
               f"not depend on the network: use parse_drawio.py.")
    elif isinstance(e, (_h.ReadTimeout, _h.TimeoutException)):
        msg = (f"{TIMEOUT}s timeout talking to watsonx.ai. Large image or slow model: "
               f"run capture_diagram.py to shrink the image, or raise "
               f"ARCH_VISION_TIMEOUT in .env.")
    elif isinstance(e, _h.HTTPError):
        msg = f"HTTP error talking to watsonx.ai: {type(e).__name__}: {e}"
    else:
        msg = (f"Unexpected error in {tool}: {type(e).__name__}: {e}. "
               f"Run `python3 mcp/arch_vision/preflight.py` to isolate the cause.")
    return json.dumps({"error": msg, "tool": tool}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = """You are a software architecture extractor. Analyze the image \
and return ONLY a JSON object: no preamble, no markdown, no explanation.

CORE RULE: report what IS drawn. Do not complete the drawing.
- Do not invent a component that does not appear.
- Do not create an arrow you cannot see. An arrow that "would make sense" is an arrow that is missing.
- Do not attribute technology that is not written down. No label -> "tech": null.
- Illegible label -> record it in unknowns, do not guess.
- Direction: only assert it if the arrowhead is visible. Otherwise "sync":"unknown"
  plus an unknown describing the ambiguity.

A hand-drawn sketch is where most mistakes happen. When in doubt, lower the
confidence and create an unknown. An honest extraction with gaps is worth more
than a complete one built on invention.

Schema:
{
 "components":[{"id":"snake_case","name":"literal label as read","kind":"service|ui|gateway|database|cache|queue|topic|job|function|storage|external|actor|unknown","tech":null,"responsibilities":[],"confidence":0.0,"evidence":{"kind":"bbox","value":[x,y,w,h],"label_text":"text as read"}}],
 "connections":[{"id":"snake_case","from":"component id","to":"component id","protocol":"http|https|grpc|graphql|amqp|kafka|mqtt|jdbc|sql|s3|file|websocket|unknown","sync":"sync|async|unknown","label":null,"payload":null,"confidence":0.0,"evidence":{"kind":"bbox","value":[x,y,w,h],"label_text":null}}],
 "boundaries":[{"id":"snake_case","name":"...","kind":"vpc|namespace|cluster|zone|onprem|cloud|dmz|account|logical","contains":["ids"],"confidence":0.0}],
 "unknowns":[{"id":"snake_case","about":"id or null","question":"closed question for the human","options":["a","b"],"blocking":true}],
 "overall_confidence":0.0,
 "legibility_notes":"what got in the way of reading it"
}

bbox: [x,y,width,height] NORMALIZED from 0 to 1 relative to the image.
confidence: 0.0 to 1.0. Be severe — 0.9+ only for what is unambiguous.
"""

VERIFY_PROMPT = """You are an independent verifier. Look ONLY at the image and \
answer whether the claim below is true.

You are NOT helping to finish somebody's work. You are checking whether somebody \
read it wrong. If the claim describes something you cannot clearly see, the \
correct answer is "uncertain" or "false" — never "true out of politeness". \
Confirming a misreading is the worst possible outcome here.

CLAIM: {claim}

Return ONLY JSON:
{{"verdict":"true|false|uncertain","confidence":0.0,"observed":"what you actually see in the relevant region","contradiction":"if false, what is there instead; otherwise null"}}
"""


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="arch_vision_list_intake",
    annotations={"readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
)
async def arch_vision_list_intake(
    directory: Annotated[Optional[str], Field(
        default=None,
        description="Directory to list. Default: .arch/intake/inbox")] = None,
) -> str:
    """List the diagram artifacts waiting to be processed and report, for each one,
    the correct extraction path (deterministic vs vision).

    Use this at the start of stage 1 to find out what there is to process.
    Artifacts marked 'deterministic' must NOT go through vision: a structured
    source exists (.drawio/.puml/.mmd) and reading it is exact and free.

    Returns JSON with name, size, inferred type and recommended tool.
    """
    d = Path(directory) if directory else INBOX
    if not d.exists():
        return json.dumps({"error": f"{d} does not exist",
                           "hint": "Create the directory and put the drawing there. "
                                   "See the 'Intake' section of the README."},
                          ensure_ascii=False)

    items = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext in DETERMINISTIC_EXT:
            path_kind, tool = "deterministic", "parse_drawio.py (do NOT use vision)"
        elif mimetypes.guess_type(str(p))[0] in ALLOWED_IMAGE_TYPES:
            path_kind, tool = "vision", "arch_vision_extract_architecture"
        elif ext == ".pdf":
            path_kind, tool = "hybrid", "read_file first; vision only if it is a pure image"
        else:
            path_kind, tool = "unknown", "ask the human"
        items.append({"path": str(p), "size_kb": round(p.stat().st_size / 1024, 1),
                      "extraction_path": path_kind, "recommended_tool": tool})

    try:
        return json.dumps({"directory": str(d), "count": len(items),
                           "artifacts": items}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _erro_json(e, "arch_vision_list_intake")




@mcp.tool(
    name="arch_vision_describe_diagram",
    annotations={"readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def arch_vision_describe_diagram(
    image_path: Annotated[str, Field(
        description="Path to the normalized PNG/JPEG, e.g. "
                    "'.arch/intake/20260716-1430-pedidos/rascunho.normalized.png'",
        min_length=1)],
    focus: Annotated[Optional[str], Field(
        default=None,
        description="A specific question to focus the reading, e.g. 'which protocols "
                    "appear on the edges?'. With no focus, it describes the whole diagram.")] = None,
) -> str:
    """Describe a software architecture diagram technically, in free-form text,
    using a multimodal model on watsonx.ai.

    Use it for initial exploration, or when structured extraction fails and you
    need to understand what is in the image. Do NOT use it as the source for the
    AIR — for that, use arch_vision_extract_architecture, which returns validatable
    JSON.

    Prerequisite: WATSONX_APIKEY and WATSONX_PROJECT_ID configured in .bob/mcp.json.
    """
    try:
        img = _encode_image(image_path)
        prompt = ("Describe this software architecture: visible components, how they "
                  "connect, direction of flow and legible labels. Say explicitly "
                  "whatever is illegible or ambiguous — do not fill a gap with a "
                  "plausible assumption.")
        if focus:
            prompt += f"\n\nSpecific focus: {focus}"
        out = _chat(_msg(prompt, img), max_tokens=1500, temperature=0.2)
        return json.dumps({"image": image_path, "model": VISION_MODEL_ID,
                           "description": out}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _erro_json(e, "arch_vision_describe_diagram")




@mcp.tool(
    name="arch_vision_extract_architecture",
    annotations={"readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def arch_vision_extract_architecture(
    image_path: Annotated[str, Field(
        description="Path to the normalized PNG/JPEG of the diagram", min_length=1)],
    source_kind: Annotated[Literal["napkin", "whiteboard", "screenshot", "pdf"], Field(
        default="screenshot",
        description="Nature of the artifact. 'napkin'/'whiteboard' turn on an extra "
                    "skepticism instruction — handwriting is where arrows get invented most.")] = "screenshot",
    hint: Annotated[Optional[str], Field(
        default=None,
        description="Context from the human that helps the reading, e.g. 'this is a "
                    "payment flow; the box on the right is a mainframe'.")] = None,
) -> str:
    """Extract the architecture structure from an image as JSON: components,
    connections, boundaries and unknowns, each element with a confidence and an
    evidence bbox.

    This is the main tool of the vision path (stage 1 of the arch2code pipeline),
    for artifacts with NO structured source: a photo of a napkin, a whiteboard, a
    screenshot, a scanned PDF. If a .drawio/.puml/.mmd of the same drawing exists,
    use parse_drawio.py: it is exact and costs no inference.

    The model is instructed NOT to complete the drawing: whatever is unclear
    becomes unknowns[], not a guess. Connections with confidence < 0.85 on a
    handwritten artifact must go through arch_vision_verify_element before becoming
    part of the AIR.

    Returns JSON aligned with air.schema.json (the components/connections/
    boundaries/unknowns sections). It is NOT a complete AIR: the architectural
    judgement is missing, and that is the arch-analyst mode's job.
    """
    try:
        img = _encode_image(image_path)
        prompt = EXTRACT_PROMPT
        if source_kind in ("napkin", "whiteboard"):
            prompt += ("\nWARNING: handwritten artifact. A hand-drawn stroke is "
                       "ambiguous by nature: a line may not be an arrow, a box may be "
                       "an annotation. Lower the confidence and prefer unknowns.\n")
        if hint:
            prompt += f"\nContext given by the human (use it, but do not treat it as seen in the image): {hint}\n"

        raw = _chat(_msg(prompt, img), max_tokens=4000, temperature=0.0)
        data = _parse_json(raw)

        # Provenance metadata: without this nobody can audit the extraction later.
        data["_provenance"] = {
            "model": VISION_MODEL_ID, "source_artifact": image_path,
            "source_kind": source_kind, "extraction_path": "vision",
            "prompt_version": "extract@1.1",
        }

        # Referential integrity: a vision model loves creating an edge to a node it
        # never listed. We flag it here instead of letting it break further down.
        ids = {c.get("id") for c in data.get("components", [])}
        broken = [c["id"] for c in data.get("connections", [])
                  if c.get("from") not in ids or c.get("to") not in ids]
        low = [c["id"] for c in data.get("connections", [])
               if c.get("confidence", 1) < 0.85]
        data["_quality"] = {
            "broken_refs": broken,
            "connections_needing_verification": low,
            "action_required": (
                "Call arch_vision_verify_element for every id in "
                "connections_needing_verification before assembling the AIR."
                if low or broken else "No additional verification required."),
        }
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        return _erro_json(e, "arch_vision_extract_architecture")




@mcp.tool(
    name="arch_vision_verify_element",
    annotations={"readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def arch_vision_verify_element(
    image_path: Annotated[str, Field(
        description="The same image used in the extraction", min_length=1)],
    claim: Annotated[str, Field(
        # The component names inside the example claim come from the AIR/the media
        # (labels drawn in Portuguese in the fixtures) — do not translate them.
        description="ONE verifiable claim, in natural language, e.g. 'is there an "
                    "arrow from the API Gateway to the Servico de Pedidos?'. One "
                    "claim per call — a compound claim produces an ambiguous verdict.",
        min_length=10, max_length=500)],
) -> str:
    """Independently verify ONE claim about the diagram, with a different prompt and
    a different pass from the extraction.

    Used by the arch-critic mode (stage 3) and mandatory for connections with
    confidence < 0.85 on a handwritten artifact. The adversarial framing ("somebody
    may have read this wrong") produces error decorrelated from the extraction —
    that is why it catches what the extraction did not. Asking again the same way
    would only confirm the same bias.

    Returns {"verdict":"true|false|uncertain","confidence":..,"observed":..,
    "contradiction":..}. 'uncertain' is a legitimate answer and must become a
    question for the human, not a nudge toward 'true'.
    """
    try:
        img = _encode_image(image_path)
        raw = _chat(_msg(VERIFY_PROMPT.format(claim=claim), img),
                    max_tokens=800, temperature=0.0)
        data = _parse_json(raw)
        data["_claim"] = claim
        data["_model"] = VISION_MODEL_ID
        data["_prompt_version"] = "verify@1.1"
        if data.get("verdict") in ("false", "uncertain"):
            data["_action"] = ("Divergence or uncertainty: do NOT approve the AIR. "
                               "Record an unknown and ask the human.")
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        return _erro_json(e, "arch_vision_verify_element")


if __name__ == "__main__":
    missing = [k for k, v in {"WATSONX_APIKEY": WATSONX_APIKEY}.items() if not v]
    if missing:
        print(f"[arch_vision] warning: {missing} not set; the vision tools will "
              f"return an actionable error until it is configured.", file=sys.stderr)
    print(f"[arch_vision] root={PROJECT_ROOT}", file=sys.stderr)
    print(f"[arch_vision] inbox={INBOX} (exists={INBOX.exists()})", file=sys.stderr)
    print(f"[arch_vision] model={VISION_MODEL_ID} region={WATSONX_URL}", file=sys.stderr)
    mcp.run(transport="stdio")
