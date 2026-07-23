#!/usr/bin/env python3
"""
validate_adk.py — offline validation of generated watsonx Orchestrate artifacts.

    python3 validate_adk.py <project_dir>

No tenant, no network, no `orchestrate env activate`. It loads the installed ADK
and runs the real pydantic models over every generated YAML, which is the exact
check that would have caught the Kubernetes-shaped agent files this repo shipped
before profiles existed.

Two things learned the hard way and encoded below:

1. `kind: Agent` with `metadata:`/`spec:` is rejected by AgentSpec with a pydantic
   enum error. That is the whole bug, catchable in milliseconds.

2. An agent spec with NO `llm` field makes the ADK go looking for the tenant's
   default model, and on a machine with an expired token that call ends the
   process with sys.exit(1). So `llm` is checked structurally FIRST, before the
   model is ever constructed. Otherwise the validator itself dies offline, which
   is a spectacular way to fail a gate that exists to work offline.

Exit: 0 = every file valid, 1 = at least one invalid, 2 = the ADK is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

Finding = Tuple[str, str, str]     # (severity, file, message)

REQUIRED_AGENT_FIELDS = ("name", "description", "llm")
KUBERNETES_TELLS = ("apiVersion", "metadata", "spec")


def _load_yaml(path: Path) -> Any:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _looks_like_agent(doc: Dict[str, Any]) -> bool:
    kind = str(doc.get("kind", "")).lower()
    return kind in ("native", "external", "assistant", "agent") or "instructions" in doc


def _looks_like_kb(doc: Dict[str, Any]) -> bool:
    kind = str(doc.get("kind", "")).lower()
    return kind in ("knowledge_base", "knowledgebase") or "vector_index" in doc


def _structural(doc: Dict[str, Any], rel: str) -> List[Finding]:
    """Cheap checks that must run before any ADK model is constructed."""
    out: List[Finding] = []
    tells = [k for k in KUBERNETES_TELLS if k in doc]
    if tells:
        out.append((
            "ERROR", rel,
            f"top-level {tells} — this is the Kubernetes shape. The ADK spec is FLAT: "
            f"spec_version, kind, name, description, llm, style, instructions, tools, "
            f"collaborators, knowledge_base all sit at the top level. There is no "
            f"metadata: block and no spec: block."
        ))
    if str(doc.get("kind", "")) in ("Agent", "KnowledgeBase", "Tool"):
        out.append((
            "ERROR", rel,
            f"kind: {doc['kind']} is CamelCase. Valid agent kinds are native, external, "
            f"assistant; the knowledge base kind is knowledge_base."
        ))
    if doc.get("spec_version") not in (None, "v1"):
        out.append(("ERROR", rel,
                    f"spec_version: {doc['spec_version']!r} — v1 is the only version the "
                    f"installed ADK knows."))
    return out


def _validate_agent(doc: Dict[str, Any], rel: str) -> List[Finding]:
    out = _structural(doc, rel)
    for fieldname in REQUIRED_AGENT_FIELDS:
        if not doc.get(fieldname):
            msg = f"missing required field '{fieldname}'"
            if fieldname == "llm":
                msg += (" — without it the ADK asks the tenant for a default model, so the "
                        "file cannot be validated (or imported) offline. Write it: "
                        "watsonx/<provider>/<model>.")
            out.append(("ERROR", rel, msg))
    if out:
        return out                      # do not construct the model on a known-bad doc

    from ibm_watsonx_orchestrate.agent_builder.agents.types import AgentSpec
    try:
        AgentSpec.model_validate(doc)
    except Exception as exc:
        first = str(exc).splitlines()
        out.append(("ERROR", rel, "AgentSpec rejected it: " + " | ".join(first[:6])))
        return out

    name = doc.get("name")
    if name in (doc.get("collaborators") or []):
        out.append(("ERROR", rel, f"agent '{name}' lists itself as a collaborator."))
    if doc.get("tools") and doc.get("style") not in ("react", "planner"):
        out.append(("WARN", rel,
                    f"agent has tools but style={doc.get('style', 'default')!r}. Tool calling "
                    f"wants react (or planner); with the default style the model may never "
                    f"reach for them."))
    if not str(doc.get("instructions") or "").strip():
        out.append(("WARN", rel, "no instructions — the agent's entire behaviour is unspecified."))
    return out


def _validate_kb(doc: Dict[str, Any], rel: str) -> List[Finding]:
    out = _structural(doc, rel)
    for legacy, correct in (("storage", "vector_index"), ("chunking", "vector_index")):
        if legacy in doc:
            out.append(("ERROR", rel,
                        f"top-level '{legacy}:' block does not exist in KnowledgeBaseSpec. "
                        f"Chunking and embedding settings live under '{correct}:'."))
    if out:
        return out

    from ibm_watsonx_orchestrate.agent_builder.agents.types import KnowledgeBaseSpec
    try:
        KnowledgeBaseSpec.model_validate(doc)
    except Exception as exc:
        out.append(("ERROR", rel,
                    "KnowledgeBaseSpec rejected it: " + " | ".join(str(exc).splitlines()[:6])))
        return out

    if not doc.get("documents") and not doc.get("conversational_search_tool"):
        out.append(("WARN", rel,
                    "no documents and no conversational_search_tool — this knowledge base "
                    "imports cleanly and then answers every question with silence."))
    return out


def _validate_tools(root: Path) -> List[Finding]:
    """Catch the two import-time mistakes that folklore keeps reintroducing."""
    out: List[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(root))
        if "ConnectionType.API_KEY" in text and "ConnectionType.API_KEY_AUTH" not in text:
            out.append(("ERROR", rel,
                        "ConnectionType.API_KEY does not exist in the ADK. The member is "
                        "API_KEY_AUTH; as written the module raises AttributeError on import."))
        if "expect_credentials" in text and "expected_credentials" not in text:
            out.append(("ERROR", rel,
                        "@expect_credentials is not a real decorator in any installed version. "
                        "Credentials go in the expected_credentials argument of @tool."))
    return out


def main(argv: List[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    root = Path(args[0] if args else ".").resolve()
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory.\n"
              f"  Fix: python3 validate_adk.py <generated project dir>")
        return 1

    try:
        import ibm_watsonx_orchestrate  # noqa: F401
        import yaml                      # noqa: F401
    except ImportError as exc:
        print(f"ERROR: {exc}.\n"
              f"  This gate validates generated YAML against the REAL ADK models. Without\n"
              f"  the package installed the check cannot run and must not be reported as a\n"
              f"  pass — a Kubernetes-shaped agent file is well-formed YAML.\n"
              f"  Fix: pip install ibm-watsonx-orchestrate pyyaml")
        return 2

    findings: List[Finding] = []
    checked = 0
    for path in sorted(list(root.rglob("*.yaml")) + list(root.rglob("*.yml"))):
        rel = str(path.relative_to(root))
        try:
            doc = _load_yaml(path)
        except Exception as exc:
            findings.append(("ERROR", rel, f"not valid YAML — {exc}"))
            continue
        if not isinstance(doc, dict):
            continue
        if _looks_like_kb(doc):
            findings += _validate_kb(doc, rel)
            checked += 1
        elif _looks_like_agent(doc):
            findings += _validate_agent(doc, rel)
            checked += 1

    findings += _validate_tools(root)

    errors = [f for f in findings if f[0] == "ERROR"]
    warns = [f for f in findings if f[0] == "WARN"]
    print(f"ADK offline validation: {root}")
    print(f"specs checked: {checked}")
    print("-" * 72)
    for sev, rel, msg in findings:
        print(f"[{sev}] {rel}: {msg}")
    if not findings:
        print("No findings.")
    print("-" * 72)
    print(f"{'INVALID' if errors else 'VALID'}: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
