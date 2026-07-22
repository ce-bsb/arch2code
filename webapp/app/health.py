"""Startup and on-demand health probes.

The whole point of this module is that a broken machine produces a readable
screen instead of a mystery. Two rules follow from that:

  * NO PROBE EVER RAISES. run_health_checks converts any exception into an
    error-level ProbeResult, because a health check that crashes tells the user
    nothing.
  * A FAILING PROBE NEVER PREVENTS STARTUP. The UI has to be reachable in order
    to display what is wrong.

Probes block modes INDEPENDENTLY. A machine with no Bob install can still run
the entire Mode A vision preview; a machine with no watsonx credentials can
still run the Mode B pipeline. `blocks` on each ProbeResult is what encodes
that, and HealthCache.assert_allows is where it is enforced.

No probe ever reports a secret. The watsonx probe reports key NAMES only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from . import bobcli
from .config import Settings, load_dotenv_into
from .errors import PreconditionFailed
from .models import HealthReport, ProbeResult, RunMode

log = logging.getLogger("arch2code.health")

__all__ = [
    "REQUIRED_MODES", "GATE_LITERALS", "run_health_checks", "HealthCache",
    "probe_bob_binary", "probe_bob_version", "probe_bob_chat_modes",
    "probe_gate_string", "probe_python_interpreter", "probe_mcp_server",
    "probe_watsonx_env", "probe_project_root", "probe_scripts",
    "probe_runs_writable", "probe_pillow",
]

#: The six arch2code chat modes. All six must appear in the --chat-mode choices
#: when `bob --help` runs with cwd=settings.bob_cwd.
#:
#: `arch2code` itself is never spawned as a subprocess — the webapp is the
#: orchestrator, because the stage-3 gate must be a human decision in the UI
#: rather than a model deciding to switch_mode. It is still asserted here: its
#: absence proves the CWD is wrong or custom_modes.yaml is not loading, which is
#: the same root cause that would break the other five.
REQUIRED_MODES: tuple[str, ...] = (
    "arch2code", "arch-intake", "arch-analyst",
    "arch-critic", "arch-scaffold", "arch-validator",
)

#: Verified in .bob/custom_modes.yaml (lines 53, 54, 255, 297, 301) and in
#: .bob/rules-arch-critic/01-review-rubric.md (lines 52, 54). The repo was
#: migrated from Portuguese to English; if it is ever re-translated these
#: literals move and parse_gate stops recognising a verdict, so the probe warns
#: loudly rather than letting the pipeline silently lose its gate.
GATE_LITERALS: tuple[str, ...] = ("VERDICT: APPROVED", "VERDICT: BLOCKED")

#: Modules the vision path needs inside ARCH2CODE_PYTHON.
_REQUIRED_IMPORTS: tuple[str, ...] = ("mcp", "httpx", "pydantic", "PIL")

_CWD_REMEDY = (
    "Bob resolves --chat-mode from the .bob/custom_modes.yaml of its working "
    "directory: `bob --help` lists 10 chat modes from this repository root and "
    "only 4 from anywhere else. Check that ARCH2CODE_BOB_CWD (or "
    "ARCH2CODE_PROJECT_ROOT) points at the directory containing .bob/, and that "
    ".bob/custom_modes.yaml parses."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# Bob probes
# ----------------------------------------------------------------------


async def probe_bob_binary(
    settings: Settings, help_result: bobcli.BobHelp | None = None
) -> ProbeResult:
    """Can we execute Bob at all?"""
    if not settings.bob_bin:
        return ProbeResult(
            id="bob_binary",
            level="error",
            title="No Bob binary configured",
            detail=(
                "ARCH2CODE_BOB_BIN is unset and there is no `bob` on PATH, so "
                "the pipeline cannot be started."
            ),
            remedy=(
                "Set ARCH2CODE_BOB_BIN to an executable on PATH (e.g. \"bob\") "
                "or to the bundle invocation (e.g. "
                "\"node /path/to/bundle/bob.js\"), then press Retry. Mode A "
                "(vision preview) does not need Bob and remains available."
            ),
            blocks=["pipeline"],
            data={"bob_bin": []},
        )

    result = help_result or await bobcli.probe_bob(settings)
    argv = " ".join(settings.bob_bin)

    if not result.ok and not result.chat_modes:
        return ProbeResult(
            id="bob_binary",
            level="error",
            title="Bob could not be executed",
            detail=(
                f"`{argv} --help` exited {result.exit_code} in "
                f"{settings.bob_cwd}. stderr: {(result.stderr or '').strip()[:500] or '<empty>'}"
            ),
            remedy=(
                f"Run `{argv} --help` by hand from {settings.bob_cwd}. If the "
                "binary is elsewhere, correct ARCH2CODE_BOB_BIN; both \"bob\" "
                "and \"node /path/bob.js\" are accepted."
            ),
            blocks=["pipeline"],
            data={"bob_bin": list(settings.bob_bin), "exit_code": result.exit_code},
        )

    return ProbeResult(
        id="bob_binary",
        level="ok",
        title="Bob binary responds",
        detail=f"`{argv} --help` succeeded in {result.duration_ms} ms.",
        data={
            "bob_bin": list(settings.bob_bin),
            "cwd": str(settings.bob_cwd),
            "duration_ms": result.duration_ms,
        },
    )


async def probe_bob_version(
    settings: Settings, help_result: bobcli.BobHelp | None = None
) -> ProbeResult:
    """Which Bob is this? Informational: an unknown version blocks nothing."""
    result = help_result or await bobcli.probe_bob(settings)
    if not result.version:
        return ProbeResult(
            id="bob_version",
            level="warn",
            title="Bob version could not be determined",
            detail=(
                "Neither --version nor --help printed a recognisable version "
                "string. This app was verified against Bob 1.0.6."
            ),
            remedy=(
                "Nothing is blocked. If stages start failing in unfamiliar ways, "
                "check the installed version by hand — the flag contract "
                "(positional prompt, deprecated -p) may have moved."
            ),
            data={},
        )
    return ProbeResult(
        id="bob_version",
        level="ok",
        title=f"Bob {result.version}",
        detail=f"Verified against 1.0.6; this install reports {result.version}.",
        data={"version": result.version},
    )


async def probe_bob_chat_modes(
    settings: Settings, help_result: bobcli.BobHelp | None = None
) -> ProbeResult:
    """Assert all six arch2code slugs are in the --chat-mode choices.

    This is the single most informative probe in the app: it proves in one shot
    that the binary runs, that the CWD is right and that custom_modes.yaml is
    being loaded.
    """
    result = help_result or await bobcli.probe_bob(settings)
    found = tuple(result.chat_modes)

    if not found:
        return ProbeResult(
            id="bob_chat_modes",
            level="error",
            title="Could not read Bob's chat modes",
            detail=(
                f"`--help` (exit {result.exit_code}) produced no parsable "
                f"--chat-mode choices list when run from {settings.bob_cwd}."
            ),
            remedy=_CWD_REMEDY,
            blocks=["pipeline"],
            data={"cwd": str(settings.bob_cwd), "exit_code": result.exit_code},
        )

    missing = [slug for slug in REQUIRED_MODES if slug not in found]
    if missing:
        return ProbeResult(
            id="bob_chat_modes",
            level="error",
            title=f"{len(missing)} of the 6 arch2code chat modes are missing",
            detail=(
                f"Missing: {', '.join(missing)}. Bob offered {len(found)} modes "
                f"from {settings.bob_cwd}: {', '.join(found)}."
            ),
            remedy=_CWD_REMEDY,
            blocks=["pipeline"],
            data={
                "missing": missing,
                "available": list(found),
                "cwd": str(settings.bob_cwd),
                "custom_modes": str(settings.custom_modes_path),
            },
        )

    return ProbeResult(
        id="bob_chat_modes",
        level="ok",
        title="All six arch2code chat modes are available",
        detail=(
            f"{len(found)} chat modes offered from {settings.bob_cwd}, including "
            f"{', '.join(REQUIRED_MODES)}."
        ),
        data={
            "available": list(found),
            "required": list(REQUIRED_MODES),
            "cwd": str(settings.bob_cwd),
            "approval_modes": list(result.approval_modes),
        },
    )


def probe_gate_string(settings: Settings) -> ProbeResult:
    """Is the stage-3 gate literal still what the parser looks for?

    A warning, never a block: the pipeline still runs, but every verdict would
    parse as `absent` and every gate would demand a human decision with no
    machine reading behind it.
    """
    sources: list[Path] = [settings.custom_modes_path]
    rules_dir = settings.critic_rules_dir
    if rules_dir.is_dir():
        sources += sorted(p for p in rules_dir.rglob("*") if p.is_file())

    hits: dict[str, list[str]] = {literal: [] for literal in GATE_LITERALS}
    readable = 0
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        readable += 1
        for literal in GATE_LITERALS:
            if literal in text:
                hits[literal].append(str(path.relative_to(settings.project_root))
                                     if path.is_relative_to(settings.project_root)
                                     else str(path))

    missing = [literal for literal, where in hits.items() if not where]
    if not readable:
        return ProbeResult(
            id="gate_string",
            level="error",
            title="The Bob mode definitions are unreadable",
            detail=f"Could not read {settings.custom_modes_path}.",
            remedy=(
                "Point ARCH2CODE_PROJECT_ROOT at the repository containing "
                ".bob/custom_modes.yaml."
            ),
            blocks=["pipeline"],
            data={"checked": [str(p) for p in sources[:5]]},
        )

    if missing:
        return ProbeResult(
            id="gate_string",
            level="warn",
            title="The stage-3 gate string was not found in the harness",
            detail=(
                f"Expected {', '.join(repr(m) for m in missing)} in "
                ".bob/custom_modes.yaml and .bob/rules-arch-critic/. The harness "
                "may have been re-translated."
            ),
            remedy=(
                "Read the arch-critic instructions and update GATE_APPROVED / "
                "GATE_BLOCKED in app/pipeline.py to match. Until then every "
                "verdict.md will parse as `absent` and each gate will reach you "
                "with no machine reading behind it — which is safe, but manual."
            ),
            data={"missing": missing, "found": {k: v for k, v in hits.items() if v}},
        )

    return ProbeResult(
        id="gate_string",
        level="ok",
        title="Stage-3 gate string confirmed",
        detail=(
            "'VERDICT: APPROVED' and 'VERDICT: BLOCKED' are both present in the "
            "arch-critic instructions."
        ),
        data={"found": hits},
    )


# ----------------------------------------------------------------------
# Interpreter probes
# ----------------------------------------------------------------------


async def _import_check(settings: Settings) -> dict[str, object]:
    """Ask ARCH2CODE_PYTHON which of the required modules it can import.

    One subprocess answers both probe_python_interpreter and probe_pillow, and
    it reports per-module status so the remedy can name the missing package
    rather than a generic 'imports failed'.
    """
    script = (
        "import json,sys\n"
        f"mods={list(_REQUIRED_IMPORTS)!r}\n"
        "out={}\n"
        "for m in mods:\n"
        "    try:\n"
        "        __import__(m); out[m]=True\n"
        "    except Exception as e:\n"
        "        out[m]='%s: %s' % (type(e).__name__, e)\n"
        "print(json.dumps({'python': sys.version.split()[0], "
        "'executable': sys.executable, 'modules': out}))\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.python_bin, "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        return {"error": "the interpreter did not answer within 60s"}
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    text = out.decode("utf-8", errors="replace").strip()
    for line in reversed(text.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"error": err.decode("utf-8", errors="replace").strip()[:500]
                     or "the interpreter produced no parsable output"}


async def probe_python_interpreter(
    settings: Settings, check: dict[str, object] | None = None
) -> ProbeResult:
    """Can ARCH2CODE_PYTHON import mcp, httpx, pydantic and PIL?

    A real failure mode, not a formality: the system python3 on this machine is
    3.9.6 and has none of them. Catching it here turns what would otherwise
    surface as an opaque MCP handshake timeout into a named variable to fix.
    """
    result = check if check is not None else await _import_check(settings)
    remedy = (
        f"Set ARCH2CODE_PYTHON to an interpreter that has them "
        f"(the default is /opt/anaconda3/bin/python), or install them into "
        f"{settings.python_bin}: "
        f"`{settings.python_bin} -m pip install -r webapp/requirements.txt`. "
        f"The system python3 is 3.9.6 here and has none of them."
    )

    if "error" in result:
        return ProbeResult(
            id="python_interpreter",
            level="error",
            title="The configured Python interpreter did not respond",
            detail=f"{settings.python_bin}: {result['error']}",
            remedy=remedy,
            blocks=["vision"],
            data={"python_bin": settings.python_bin},
        )

    modules = result.get("modules") or {}
    failed = {k: v for k, v in modules.items() if v is not True}  # type: ignore[union-attr]
    if failed:
        return ProbeResult(
            id="python_interpreter",
            level="error",
            title=f"{settings.python_bin} cannot import {', '.join(failed)}",
            detail="; ".join(f"{k}: {v}" for k, v in failed.items()),
            remedy=remedy,
            blocks=["vision"],
            data={
                "python_bin": settings.python_bin,
                "python_version": result.get("python"),
                "failed": list(failed),
            },
        )

    return ProbeResult(
        id="python_interpreter",
        level="ok",
        title=f"Python {result.get('python', '?')} ready",
        detail=(
            f"{settings.python_bin} imports "
            f"{', '.join(_REQUIRED_IMPORTS)} successfully."
        ),
        data={
            "python_bin": settings.python_bin,
            "python_version": result.get("python"),
            "modules": list(_REQUIRED_IMPORTS),
        },
    )


async def probe_pillow(
    settings: Settings, check: dict[str, object] | None = None
) -> ProbeResult:
    """Pillow specifically: capture_diagram.py exits 1 without it.

    Without normalization there is no EXIF correction, and a rotated phone photo
    makes a vision model produce confident garbage.
    """
    result = check if check is not None else await _import_check(settings)
    modules = result.get("modules") or {} if "error" not in result else {}
    status = modules.get("PIL") if isinstance(modules, dict) else None

    if status is True:
        return ProbeResult(
            id="pillow",
            level="ok",
            title="Pillow available",
            detail=f"{settings.python_bin} can import PIL, so capture_diagram.py "
                   f"can normalize images.",
            data={},
        )

    return ProbeResult(
        id="pillow",
        level="error",
        title="Pillow is not importable",
        detail=(
            f"{settings.python_bin} cannot import PIL"
            + (f" ({status})" if isinstance(status, str) else "")
            + ". capture_diagram.py exits 1 without it, so no image can be "
              "normalized and no bbox would be valid."
        ),
        remedy=f"`{settings.python_bin} -m pip install pillow`, then press Retry.",
        blocks=["vision"],
        data={"python_bin": settings.python_bin},
    )


async def probe_mcp_server(settings: Settings) -> ProbeResult:
    """stdio handshake with mcp/arch_vision/server.py plus list_tools."""
    try:
        from .vision import TOOLS, ArchVisionClient
    except ImportError as exc:  # pragma: no cover - during integration only
        return ProbeResult(
            id="mcp_server",
            level="error",
            title="The vision client module is unavailable",
            detail=f"app.vision could not be imported: {exc}",
            remedy="This is a build defect; app/vision.py is missing or broken.",
            blocks=["vision"],
            data={},
        )

    if not settings.mcp_server_path.is_file():
        return ProbeResult(
            id="mcp_server",
            level="error",
            title="The arch_vision MCP server was not found",
            detail=f"{settings.mcp_server_path} does not exist.",
            remedy=(
                "Point ARCH2CODE_PROJECT_ROOT at the repository containing "
                "mcp/arch_vision/server.py."
            ),
            blocks=["vision"],
            data={"path": str(settings.mcp_server_path)},
        )

    # Validate the interpreter that BOB will spawn, which is not the one this
    # probe uses. Bob reads .bob/mcp.json; this process reads ARCH2CODE_PYTHON.
    # When they disagree the handshake below still succeeds and the probe still
    # reports green, while Bob fails with `spawn <path> ENOENT`, drops the
    # arch_vision tools from its tool list, and runs the analyst stage BLIND —
    # it never calls vision and never says so. Checking the file Bob actually
    # reads is the only way this class of failure is visible from here.
    mcp_json = settings.project_root / ".bob" / "mcp.json"
    try:
        declared = json.loads(mcp_json.read_text(encoding="utf-8"))
        command = declared["mcpServers"]["arch_vision"]["command"]
    except Exception as exc:  # noqa: BLE001 - any unreadable shape is the same problem
        return ProbeResult(
            id="mcp_server",
            level="error",
            title="Bob's MCP registration is unreadable",
            detail=f"{mcp_json}: {exc}",
            remedy=(
                "Recreate it with `python3 mcp/arch_vision/configure_bob.py`, "
                "which writes an interpreter it has proven can import mcp."
            ),
            blocks=["pipeline"],
            data={"path": str(mcp_json)},
        )

    resolved = shutil.which(command) if "/" not in command else (
        command if os.access(command, os.X_OK) else None
    )
    if resolved is None:
        return ProbeResult(
            id="mcp_server",
            level="error",
            title="Bob cannot start the vision server",
            detail=(
                f"{mcp_json} tells Bob to spawn {command!r}, which does not exist "
                f"here. Bob fails with ENOENT, loses the four arch_vision tools, "
                f"and the analyst stage then runs without vision instead of stopping."
            ),
            remedy=(
                "Run `python3 mcp/arch_vision/configure_bob.py` on this machine. "
                "Do not commit the result: it hard-codes a local interpreter path."
            ),
            blocks=["pipeline"],
            data={"declared_command": command, "path": str(mcp_json)},
        )

    # Existing is not enough: `python3` resolves on almost any machine and is
    # very often the system interpreter that has no mcp. Bob would then fail on
    # import rather than on spawn — a different message, the same blind analyst.
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved, "-c", "import mcp, httpx, pydantic",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, import_err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        import_rc = proc.returncode
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        import_rc, import_err = 1, str(exc).encode()

    if import_rc != 0:
        return ProbeResult(
            id="mcp_server",
            level="error",
            title="Bob's interpreter cannot import the MCP libraries",
            detail=(
                f"{mcp_json} tells Bob to spawn {resolved}, which cannot import "
                f"mcp, httpx and pydantic. Bob starts the process and it dies, "
                f"so the arch_vision tools never register."
            ),
            remedy=(
                "Run `python3 mcp/arch_vision/configure_bob.py`: it tests each "
                "candidate interpreter and writes one that has proven it imports "
                "them. Do not commit the result — it hard-codes a local path."
            ),
            blocks=["pipeline"],
            data={"declared_command": command, "resolved": resolved,
                  "stderr": (import_err or b"").decode("utf-8", "replace")[-300:]},
        )

    started = time.monotonic()
    try:
        names = await ArchVisionClient(settings).list_tools()
    except Exception as exc:  # noqa: BLE001 - a probe reports, never propagates
        detail = getattr(exc, "detail", None) or str(exc)
        return ProbeResult(
            id="mcp_server",
            level="error",
            title=getattr(exc, "title", None) or "The arch_vision MCP server did not answer",
            detail=str(detail)[:800],
            remedy=getattr(exc, "remedy", None) or (
                f"Run `{settings.python_bin} {settings.mcp_server_path}` in a "
                "terminal and read the first error it prints."
            ),
            blocks=["vision"],
            data={"path": str(settings.mcp_server_path)},
        )

    missing = [name for name in TOOLS if name not in names]
    duration_ms = int((time.monotonic() - started) * 1000)
    if missing:
        return ProbeResult(
            id="mcp_server",
            level="error",
            title="The MCP server is missing expected tools",
            detail=f"Missing: {', '.join(missing)}. Offered: {', '.join(names) or '<none>'}.",
            remedy=(
                "mcp/arch_vision/server.py is older or newer than this app "
                "expects. Both must define "
                "arch_vision_list_intake, arch_vision_describe_diagram, "
                "arch_vision_extract_architecture and arch_vision_verify_element."
            ),
            blocks=["vision"],
            data={"missing": missing, "tools": list(names)},
        )

    return ProbeResult(
        id="mcp_server",
        level="ok",
        title="arch_vision MCP server responds",
        detail=f"Handshake completed in {duration_ms} ms; all four tools present.",
        data={"tools": list(names), "duration_ms": duration_ms},
    )


def probe_watsonx_env(settings: Settings) -> ProbeResult:
    """Presence of the watsonx credentials. NAMES ONLY, never a value."""
    merged: dict[str, str] = dict(os.environ)
    file_keys = load_dotenv_into(settings.mcp_env_file, merged)

    def present(key: str) -> bool:
        return bool((merged.get(key) or "").strip())

    has_key = present("WATSONX_APIKEY")
    has_target = present("WATSONX_PROJECT_ID") or present("WATSONX_SPACE_ID")
    # Deliberately a list of NAMES. No value from this file is ever returned by
    # the API, written to run.json or put in the event log.
    names_present = sorted(
        key for key in set(file_keys) | set(os.environ)
        if key.startswith("WATSONX_") and present(key)
    )

    if not has_key or not has_target:
        missing = []
        if not has_key:
            missing.append("WATSONX_APIKEY")
        if not has_target:
            missing.append("WATSONX_PROJECT_ID or WATSONX_SPACE_ID")
        return ProbeResult(
            id="watsonx_env",
            level="error",
            title="watsonx credentials are incomplete",
            detail=(
                f"Missing: {', '.join(missing)}. Checked the process environment "
                f"and {settings.mcp_env_file}."
            ),
            remedy=(
                f"Add the missing keys to {settings.mcp_env_file} (git-ignored) "
                "or export them in the shell that runs ./run.sh, then press "
                "Retry. Mode B (pipeline) does not need them."
            ),
            blocks=["vision"],
            data={"env_file": str(settings.mcp_env_file),
                  "keys_present": names_present},
        )

    return ProbeResult(
        id="watsonx_env",
        level="ok",
        title="watsonx credentials present",
        detail=(
            f"{len(names_present)} WATSONX_* variables are set "
            f"({', '.join(names_present)}). Values are never read into the app, "
            "only into the environment of child processes."
        ),
        data={"keys_present": names_present,
              "env_file_exists": settings.mcp_env_file.is_file()},
    )


# ----------------------------------------------------------------------
# Filesystem probes
# ----------------------------------------------------------------------


def probe_project_root(settings: Settings) -> ProbeResult:
    """Does the configured project root actually look like this repository?"""
    expected = {
        ".bob/custom_modes.yaml": settings.custom_modes_path,
        "mcp/arch_vision/server.py": settings.mcp_server_path,
    }
    missing = [name for name, path in expected.items() if not path.exists()]

    if missing:
        return ProbeResult(
            id="project_root",
            level="error",
            title="ARCH2CODE_PROJECT_ROOT does not look like the arch2code repo",
            detail=f"{settings.project_root} is missing: {', '.join(missing)}.",
            remedy=(
                "Set ARCH2CODE_PROJECT_ROOT to the repository root (the "
                "directory containing .bob/ and mcp/). ./run.sh exports it as "
                "the parent of webapp/, which is correct for an in-tree "
                "checkout."
            ),
            blocks=["vision", "pipeline"],
            data={"project_root": str(settings.project_root), "missing": missing},
        )

    return ProbeResult(
        id="project_root",
        level="ok",
        title="Project root resolved",
        detail=(
            f"{settings.project_root} contains .bob/ and mcp/. Bob will run with "
            f"cwd={settings.bob_cwd}."
        ),
        data={
            "project_root": str(settings.project_root),
            "bob_cwd": str(settings.bob_cwd),
            #: True when a Mode B run will write into THIS repository's .arch/
            #: tree. Unavoidable: the mode fileRegex patterns are anchored at
            #: the workspace root and that trail is the pipeline's whole value.
            "writes_into_project": settings.bob_cwd == settings.project_root,
        },
    )


def probe_scripts(settings: Settings) -> ProbeResult:
    """The three deterministic helper scripts, which need no credentials."""
    required = {"capture_diagram.py": settings.capture_script}
    optional = {
        "parse_drawio.py": settings.drawio_script,
        "validate_air.py": settings.validate_air_script,
    }

    def readable(path: Path) -> bool:
        return path.is_file() and os.access(path, os.R_OK)

    missing_required = [n for n, p in required.items() if not readable(p)]
    missing_optional = [n for n, p in optional.items() if not readable(p)]

    if missing_required:
        return ProbeResult(
            id="deterministic_scripts",
            level="error",
            title="capture_diagram.py is missing",
            detail=(
                f"Expected at {settings.capture_script}. Without it no image can "
                "be normalized, and bboxes are only valid against the "
                "normalized PNG."
            ),
            remedy=(
                "Check that ARCH2CODE_PROJECT_ROOT points at the repository "
                "containing .bob/skills/diagram-intake/scripts/."
            ),
            blocks=["vision"],
            data={"missing": missing_required},
        )

    if missing_optional:
        return ProbeResult(
            id="deterministic_scripts",
            level="warn",
            title=f"{len(missing_optional)} helper script(s) missing",
            detail=(
                f"Missing: {', '.join(missing_optional)}. capture_diagram.py is "
                "present, so the vision path works."
            ),
            remedy=(
                "parse_drawio.py is the zero-cost deterministic path for "
                ".drawio/.xml uploads; validate_air.py is the AIR schema gate. "
                "Restore them from .bob/skills/ to get both back."
            ),
            data={"missing": missing_optional},
        )

    return ProbeResult(
        id="deterministic_scripts",
        level="ok",
        title="All three deterministic scripts present",
        detail=(
            "capture_diagram.py, parse_drawio.py and validate_air.py are "
            "readable. None of them needs a credential."
        ),
        data={n: str(p) for n, p in {**required, **optional}.items()},
    )


def probe_runs_writable(settings: Settings) -> ProbeResult:
    """Can we actually persist run state and uploads?

    The event log is the primary store, so an unwritable runs/ is not a
    degraded mode — it is a hard stop for both modes.
    """
    checks = {"runs": settings.runs_root, "uploads": settings.uploads_root}
    failures: dict[str, str] = {}
    for name, root in checks.items():
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe_file = root / ".write-probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
        except OSError as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"

    if failures:
        return ProbeResult(
            id="runs_writable",
            level="error",
            title="Run state cannot be written",
            detail="; ".join(f"{k}: {v}" for k, v in failures.items()),
            remedy=(
                f"Make {settings.runs_root} and {settings.uploads_root} writable "
                "by the user running ./run.sh. Every event and every uploaded "
                "diagram is persisted there; nothing is kept only in memory."
            ),
            blocks=["vision", "pipeline"],
            data={"failures": failures},
        )

    return ProbeResult(
        id="runs_writable",
        level="ok",
        title="Run state directories writable",
        detail=f"{settings.runs_root} and {settings.uploads_root} are writable.",
        data={"runs_root": str(settings.runs_root),
              "uploads_root": str(settings.uploads_root)},
    )


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

#: The order probes appear in the UI: what blocks everything first, then Bob,
#: then vision.
_PROBE_ORDER = (
    "project_root", "runs_writable",
    "bob_binary", "bob_version", "bob_chat_modes", "gate_string",
    "python_interpreter", "pillow", "watsonx_env", "mcp_server",
    "deterministic_scripts",
)


def _failed_probe(probe_id: str, exc: BaseException) -> ProbeResult:
    return ProbeResult(
        id=probe_id,
        level="error",
        title=f"The {probe_id} probe crashed",
        detail=f"{type(exc).__name__}: {exc}",
        remedy=(
            "This is a defect in the health check itself, not necessarily in "
            "your environment. The traceback is in the terminal running "
            "./run.sh."
        ),
        data={},
    )


async def _guard(
    probe_id: str, factory: Callable[[], Awaitable[ProbeResult]]
) -> ProbeResult:
    try:
        return await factory()
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - a probe must never propagate
        log.exception("probe %s failed", probe_id)
        return _failed_probe(probe_id, exc)


async def run_health_checks(settings: Settings) -> HealthReport:
    """Run every probe concurrently. Never raises.

    The two expensive shared operations — `bob --help` and the interpreter
    import check — are performed once and handed to the probes that need them,
    so a recheck costs one Bob launch rather than three.
    """
    started = time.monotonic()

    bob_task = asyncio.create_task(_safe_bob_help(settings))
    imports_task = asyncio.create_task(_safe_import_check(settings))
    bob_help, imports = await asyncio.gather(bob_task, imports_task)

    async def sync(fn: Callable[[Settings], ProbeResult]) -> ProbeResult:
        return await asyncio.to_thread(fn, settings)

    results = await asyncio.gather(
        _guard("project_root", lambda: sync(probe_project_root)),
        _guard("runs_writable", lambda: sync(probe_runs_writable)),
        _guard("bob_binary", lambda: probe_bob_binary(settings, bob_help)),
        _guard("bob_version", lambda: probe_bob_version(settings, bob_help)),
        _guard("bob_chat_modes", lambda: probe_bob_chat_modes(settings, bob_help)),
        _guard("gate_string", lambda: sync(probe_gate_string)),
        _guard("python_interpreter",
               lambda: probe_python_interpreter(settings, imports)),
        _guard("pillow", lambda: probe_pillow(settings, imports)),
        _guard("watsonx_env", lambda: sync(probe_watsonx_env)),
        _guard("mcp_server", lambda: probe_mcp_server(settings)),
        _guard("deterministic_scripts", lambda: sync(probe_scripts)),
    )

    order = {probe_id: i for i, probe_id in enumerate(_PROBE_ORDER)}
    probes = sorted(results, key=lambda p: order.get(p.id, len(order)))
    blocking = [p for p in probes if p.level == "error" and p.blocks]

    log.info(
        "health: %d probes in %d ms, %d blocking failure(s)",
        len(probes), int((time.monotonic() - started) * 1000), len(blocking),
    )
    return HealthReport(
        ok=not any(p.level == "error" for p in probes),
        checked_at=_now(),
        blocking_failures=len(blocking),
        probes=probes,
    )


async def _safe_bob_help(settings: Settings) -> bobcli.BobHelp | None:
    try:
        return await bobcli.probe_bob(settings)
    except Exception:  # noqa: BLE001 - reported by probe_bob_binary instead
        log.exception("bob --help probe raised")
        return None


async def _safe_import_check(settings: Settings) -> dict[str, object] | None:
    try:
        return await _import_check(settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("interpreter import check raised")
        return {"error": f"{type(exc).__name__}: {exc}"}


class HealthCache:
    """The cached report, plus the gate that keeps a doomed run from starting."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.report: HealthReport | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> HealthReport:
        if self.report is None:
            return await self.refresh()
        return self.report

    async def refresh(self) -> HealthReport:
        """Re-run every probe and replace the cache.

        This is what the UI banner's Retry button calls, so fixing an env var
        never requires restarting the app.
        """
        async with self._lock:
            self.report = await run_health_checks(self.settings)
            return self.report

    def assert_allows(self, mode: RunMode) -> None:
        """Raise PreconditionFailed (424) if a probe blocks `mode`.

        Refusing here is kinder than letting the run start: a pipeline run that
        cannot possibly work still costs Bobcoin before it fails.
        """
        report = self.report
        if report is None:
            return  # not probed yet; the run is allowed and will fail loudly
        offenders = report.blocks(mode)
        if not offenders:
            return
        raise PreconditionFailed(
            "health_blocks_mode",
            f"The environment cannot run a {mode} run right now",
            "; ".join(f"{p.title}: {p.detail}" for p in offenders),
            remedy=" | ".join(p.remedy for p in offenders if p.remedy) or (
                "Fix the failing probes shown in the health banner, then press "
                "Retry."
            ),
            mode=mode,
            probes=[p.model_dump(mode="json") for p in offenders],
        )
