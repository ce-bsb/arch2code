#!/usr/bin/env python3
"""
validate_air.py — validates an AIR against the schema and against the semantic
rules that JSON Schema cannot express.

Usage:
    python3 validate_air.py .arch/air/<run>/air.json [--gate]

    --gate  also applies the stage 3 (arch-critic) blocking criteria.
            Without --gate, only the structure is validated (arch-analyst usage).

Output: readable report on stdout. Exit code 0 = valid, 1 = invalid.

Why schema and semantics are separate: the schema guarantees the JSON has the
right shape. The rules below guarantee it makes SENSE as an architecture — broken
reference, orphan component, synchronous cycle. An AIR can be perfectly valid
against the schema and still describe a system that locks up in production.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "air.schema.json"

# Gate thresholds — they mirror .bob/rules-arch-critic/01-review-rubric.md
MIN_OVERALL_CONFIDENCE = 0.75
VERIFY_SECOND_PASS_BELOW = 0.85

Finding = Tuple[str, str]  # (severity, message)  severity: ERROR | WARN


# ---------------------------------------------------------------------------
# Layer 1 — schema
# ---------------------------------------------------------------------------
def validate_schema(air: Dict[str, Any]) -> List[Finding]:
    try:
        import jsonschema
    except ImportError:
        return [("WARN", "jsonschema not installed (pip install jsonschema); "
                         "only the semantic rules were applied")]

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    out: List[Finding] = []
    for err in sorted(validator.iter_errors(air), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        out.append(("ERROR", f"schema at {loc}: {err.message}"))
    return out


# ---------------------------------------------------------------------------
# Layer 2 — architecture semantics
# ---------------------------------------------------------------------------
def validate_semantics(air: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    comps = {c["id"]: c for c in air.get("components", [])}
    conns = air.get("connections", [])

    # Broken references: a connection pointing at a component that does not exist.
    # Classic symptom of a vision extraction that read a loose label as a node.
    for c in conns:
        for end in ("from", "to"):
            if c.get(end) not in comps:
                out.append(("ERROR", f"connection '{c['id']}': {end}='{c.get(end)}' "
                                     f"does not exist in components[]"))
        if c.get("from") == c.get("to"):
            out.append(("WARN", f"connection '{c['id']}': self-reference on "
                                f"'{c.get('from')}' — confirm with the human"))

    # Orphan components.
    touched = {c.get("from") for c in conns} | {c.get("to") for c in conns}
    for cid, comp in comps.items():
        if cid not in touched and comp.get("kind") != "actor":
            out.append(("WARN", f"component '{cid}' ({comp['name']}) has no "
                                f"connection at all — an arrow lost in extraction?"))

    # Boundaries pointing at components that do not exist.
    for b in air.get("boundaries", []):
        for cid in b.get("contains", []):
            if cid not in comps:
                out.append(("ERROR", f"boundary '{b['id']}' contains '{cid}', "
                                     f"which does not exist"))

    # Inline tools (components[].tools[]) share the id namespace with components.
    # A collision here is not cosmetic: every agentic profile turns an id into a
    # file name, so two `abre_disputa` write over each other and one tool silently
    # disappears from the generated project.
    tool_owner: Dict[str, str] = {}
    for cid, comp in comps.items():
        for tl in comp.get("tools") or []:
            tid = tl.get("id")
            if tid in comps:
                out.append(("ERROR", f"tool '{tid}' on component '{cid}' reuses a "
                                     f"components[] id — same id, two different things"))
            elif tid in tool_owner:
                out.append(("ERROR", f"tool '{tid}' is declared on both "
                                     f"'{tool_owner[tid]}' and '{cid}' — one file, two definitions; "
                                     f"give them distinct ids or hoist the shared tool"))
            else:
                tool_owner[tid] = cid

    # unknowns.about and assumptions/hypotheses.relates_to must reference real ids.
    known_ids = set(comps) | {c["id"] for c in conns} | set(tool_owner)
    for u in air.get("unknowns", []):
        if u.get("about") and u["about"] not in known_ids:
            out.append(("WARN", f"unknown '{u['id']}' references '{u['about']}', "
                                f"which is not a known id"))
    for h in air.get("experiment_plan", {}).get("hypotheses", []):
        for rid in h.get("relates_to", []):
            if rid not in known_ids:
                out.append(("WARN", f"hypothesis '{h['id']}' references '{rid}', "
                                    f"which is not a known id"))

    # Synchronous cycle: A -> B -> A with sync. A distributed deadlock waiting to happen.
    sync_edges: Dict[str, set] = {}
    for c in conns:
        if c.get("sync") == "sync":
            sync_edges.setdefault(c["from"], set()).add(c["to"])
    for cycle in _find_cycles(sync_edges):
        out.append(("ERROR", "synchronous cycle: " + " -> ".join(cycle + [cycle[0]]) +
                             " — blocking circular coupling"))

    return out


def _find_cycles(graph: Dict[str, set]) -> List[List[str]]:
    """DFS with a stack. Returns each cycle once, canonicalized by its lowest node."""
    cycles, seen = [], set()

    def walk(node: str, path: List[str], onpath: set) -> None:
        for nxt in graph.get(node, ()):
            if nxt in onpath:
                cyc = path[path.index(nxt):]
                key = tuple(sorted(cyc))
                if key not in seen:
                    seen.add(key)
                    cycles.append(cyc)
            elif nxt in graph:
                walk(nxt, path + [nxt], onpath | {nxt})

    for start in list(graph):
        walk(start, [start], {start})
    return cycles


# ---------------------------------------------------------------------------
# Layer 3 — arch-critic gates
# ---------------------------------------------------------------------------
def validate_gate(air: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    meta = air.get("meta", {})

    conf = meta.get("overall_confidence", 0)
    if conf < MIN_OVERALL_CONFIDENCE:
        out.append(("ERROR", f"overall_confidence={conf:.2f} below the minimum of "
                             f"{MIN_OVERALL_CONFIDENCE} — reprocess or ask the human"))

    blocking = [u for u in air.get("unknowns", []) if u.get("blocking") and not u.get("answer")]
    for u in blocking:
        out.append(("ERROR", f"open blocking unknown '{u['id']}': {u['question']}"))

    for a in air.get("assumptions", []):
        if not (a.get("impact") or "").strip():
            out.append(("ERROR", f"assumption '{a['id']}' has no declared impact"))

    # Hand-drawn is where the vision model invents arrows the most: require a second pass.
    if meta.get("source_kind") in {"napkin", "whiteboard"} and meta.get("extraction_path") == "vision":
        for c in air.get("connections", []):
            if c.get("confidence", 1) < VERIFY_SECOND_PASS_BELOW and not c.get("verified_by_second_pass"):
                out.append(("ERROR", f"connection '{c['id']}' with confidence "
                                     f"{c.get('confidence'):.2f} did not go through the "
                                     f"independent verification (arch_vision_verify_element)"))

    hyps = air.get("experiment_plan", {}).get("hypotheses", [])
    if not hyps:
        out.append(("ERROR", "experiment_plan.hypotheses is empty — without a hypothesis "
                             "the prototype becomes a demo, not an experiment"))
    if not air.get("experiment_plan", {}).get("out_of_scope"):
        out.append(("ERROR", "experiment_plan.out_of_scope is empty — define what will NOT be done"))

    return out


# ---------------------------------------------------------------------------
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    gate = "--gate" in sys.argv
    if not args:
        print(__doc__)
        return 2

    path = Path(args[0])
    if not path.exists():
        print(f"ERROR: {path} does not exist")
        return 1

    try:
        air = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in {path}: {e}")
        return 1

    findings = validate_schema(air) + validate_semantics(air)
    if gate:
        findings += validate_gate(air)

    errors = [f for f in findings if f[0] == "ERROR"]
    warns = [f for f in findings if f[0] == "WARN"]

    print(f"AIR: {path}")
    print(f"run_id: {air.get('meta', {}).get('run_id', '?')}   "
          f"source: {air.get('meta', {}).get('source_artifact', '?')}   "
          f"path: {air.get('meta', {}).get('extraction_path', '?')}")
    print(f"components: {len(air.get('components', []))}   "
          f"connections: {len(air.get('connections', []))}   "
          f"unknowns: {len(air.get('unknowns', []))}   "
          f"assumptions: {len(air.get('assumptions', []))}")
    print("-" * 72)

    for sev, msg in findings:
        print(f"[{sev}] {msg}")
    if not findings:
        print("No findings.")

    print("-" * 72)
    verdict = "INVALID" if errors else "VALID"
    suffix = " (gate applied)" if gate else ""
    print(f"{verdict}{suffix}: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
