#!/usr/bin/env python3
"""
parse_drawio.py — extracts the graph from a .drawio / .xml file (mxGraphModel).

Usage:
    python3 parse_drawio.py <file.drawio> [--out .arch/intake/<run>/extraction.json]

WHY THIS SCRIPT EXISTS
----------------------
When a structured source exists, using a vision model is an engineering error:
you trade an exact read for a probabilistic one, pay tokens and on top of that
import hallucination risk. The .drawio already contains the nodes, the edges, the
direction and the labels, in XML. This script reads that with 100% fidelity, zero
inference cost and traceable evidence (the mxCell id).

The vision path (arch_vision_extract_architecture) only comes in when there is NO
structured source: napkin photo, whiteboard, screenshot, scanned PDF.

FORMAT
------
A .drawio can store the diagram in two ways:
  1) plain XML inside <diagram>
  2) compressed: base64 -> raw deflate -> URL-encode  (the app's default)
This script handles both. Multiple tabs (<diagram>) become separate pages.

Output: JSON in the raw extraction format consumed by the arch-analyst mode.
It does not produce an AIR — it produces an extraction. Translating to AIR is the
analyst's job, because it takes judgement (what is a "service"?), and judgement
does not belong in a parser.
"""

import base64
import json
import re
import sys
import urllib.parse
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

PARSER_VERSION = "parse_drawio.py@1.0"

# Shape -> kind heuristics. These are HINTS, not truth: the field goes out as
# `shape_hint` and the final kind is decided by the arch-analyst, with the human.
SHAPE_HINTS = [
    (r"mxgraph\.flowchart\.database|shape=cylinder|shape=datastore", "database"),
    (r"mxgraph\.aws\d*\.|mxgraph\.azure|mxgraph\.gcp", "cloud_resource"),
    (r"shape=queue|mxgraph\.\w*\.queue|kafka|rabbit|mq\b", "queue"),
    (r"shape=cloud", "external"),
    (r"shape=actor|shape=umlActor", "actor"),
    (r"ellipse", "event_or_actor"),
    (r"rhombus", "decision"),
    (r"shape=process|rounded=1", "service"),
]

# Common edge labels -> protocol. Same idea: a hint, not a decision.
PROTOCOL_HINTS = [
    (r"\bhttps\b", "https"),
    # "POST /pedidos" is the literal label of edge e2 in the .drawio fixture — keep.
    (r"\b(get|post|put|patch|delete)\s+/", "http"),
    (r"\bhttp\b|\brest\b|\bapi\b", "http"),
    (r"\bgrpc\b", "grpc"), (r"\bgraphql\b", "graphql"),
    # 'evento' (PT) matches the fixture edge label "evento PedidoCriado" — keep.
    # Dropping the Portuguese alternative silently downgrades e4 to `unknown`.
    (r"\bkafka\b|\bevento?\b|\bevent\b", "kafka"),
    (r"\bamqp\b|\brabbit\b|\bmq\b", "amqp"),
    (r"\bjdbc\b|\bsql\b|\bselect\b|\binsert\b", "sql"),
    (r"\bs3\b|\bbucket\b", "s3"), (r"\bws\b|\bwebsocket\b", "websocket"),
]


def _decode_diagram(node: ET.Element) -> Optional[ET.Element]:
    """Returns the <mxGraphModel> of a tab, compressed or not."""
    inner = node.find("mxGraphModel")
    if inner is not None:
        return inner

    payload = (node.text or "").strip()
    if not payload:
        return None
    try:
        raw = base64.b64decode(payload)
        # -15 = raw deflate, no zlib header. That is what drawio writes.
        xml = zlib.decompress(raw, -15).decode("utf-8")
        xml = urllib.parse.unquote(xml)
        return ET.fromstring(xml)
    except Exception as exc:  # noqa: BLE001 — degrade with a warning, do not blow up
        print(f"  warn: could not decode the tab '{node.get('name')}': {exc}",
              file=sys.stderr)
        return None


def _strip_html(text: str) -> str:
    """drawio labels come with HTML. Line breaks are preserved as a space."""
    text = re.sub(r"<br\s*/?>", " ", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _hint(patterns: List, text: str, default: str) -> str:
    low = (text or "").lower()
    for pat, val in patterns:
        if re.search(pat, low):
            return val
    return default


def _geometry(cell: ET.Element) -> Optional[Dict[str, float]]:
    geo = cell.find("mxGeometry")
    if geo is None:
        return None
    try:
        return {k: float(geo.get(k)) for k in ("x", "y", "width", "height")
                if geo.get(k) is not None} or None
    except (TypeError, ValueError):
        return None


def parse_page(model: ET.Element, page_name: str) -> Dict[str, Any]:
    root = model.find("root")
    if root is None:
        return {"page": page_name, "nodes": [], "edges": []}

    cells = root.findall("mxCell") + root.findall(".//object")
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    labels_by_id: Dict[str, str] = {}

    for cell in cells:
        # <object> wraps an mxCell and carries custom attributes
        inner = cell.find("mxCell") if cell.tag == "object" else cell
        if inner is None:
            continue
        cid = cell.get("id") or inner.get("id")
        style = inner.get("style") or ""
        label = _strip_html(cell.get("label") or inner.get("value") or "")
        if cid:
            labels_by_id[cid] = label

        if inner.get("edge") == "1":
            edges.append({
                "cell_id": cid,
                "source": inner.get("source"),
                "target": inner.get("target"),
                "label": label or None,
                "style": style,
                "protocol_hint": _hint(PROTOCOL_HINTS, f"{label} {style}", "unknown"),
                # Edge with no explicit arrowhead: the direction is NOT trustworthy.
                "directed": "endArrow=none" not in style,
                "dashed": "dashed=1" in style,
            })
        elif inner.get("vertex") == "1":
            if not label and "group" not in style:
                continue  # decorative shape with no label
            nodes.append({
                "cell_id": cid,
                "label": label,
                "style": style,
                "shape_hint": _hint(SHAPE_HINTS, style, "unknown"),
                "geometry": _geometry(inner),
                "parent": inner.get("parent"),
                "is_container": "container=1" in style or "swimlane" in style,
                "custom_attrs": {k: v for k, v in cell.attrib.items()
                                 if k not in ("id", "label")} if cell.tag == "object" else {},
            })

    # Edges attached to a cell that never became a node (e.g. a dangling edge or a
    # label-only cell)
    node_ids = {n["cell_id"] for n in nodes}
    for e in edges:
        e["dangling"] = e["source"] not in node_ids or e["target"] not in node_ids
        e["source_label"] = labels_by_id.get(e["source"] or "")
        e["target_label"] = labels_by_id.get(e["target"] or "")

    return {"page": page_name, "nodes": nodes, "edges": edges}


def parse_file(path: Path) -> Dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()

    diagrams = root.findall(".//diagram")
    if not diagrams and root.tag == "mxGraphModel":
        pages = [parse_page(root, "default")]  # .xml exported directly
    else:
        pages = []
        for d in diagrams:
            model = _decode_diagram(d)
            if model is not None:
                pages.append(parse_page(model, d.get("name") or f"page-{len(pages)+1}"))

    import hashlib
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    n_nodes = sum(len(p["nodes"]) for p in pages)
    n_edges = sum(len(p["edges"]) for p in pages)

    warnings: List[str] = []
    for p in pages:
        for e in p["edges"]:
            if e["dangling"]:
                warnings.append(f"[{p['page']}] edge {e['cell_id']} is dangling "
                                f"(source/target has no node) — confirm with the human")
            if not e["directed"]:
                # 'no arrowhead' is a load-bearing substring: tests/smoke_test.sh
                # asserts on it. Keep it if you reword the rest.
                warnings.append(f"[{p['page']}] edge {e['cell_id']} has no arrowhead: "
                                f"direction is indeterminate, becomes an unknown in the AIR")
        for n in p["nodes"]:
            if n["shape_hint"] == "unknown" and not n["is_container"]:
                warnings.append(f"[{p['page']}] node '{n['label']}' has no shape hint: "
                                f"the analyst must decide the kind")

    return {
        "extraction_version": "1.0",
        "source_artifact": str(path),
        "source_sha256": digest,
        "source_kind": "drawio",
        "extraction_path": "deterministic",
        "extractor": PARSER_VERSION,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        # Determinism: an exact read of the XML. The confidence here is the PARSER's,
        # not the interpretation's — the analyst still has to decide what each node means.
        "overall_confidence": 1.0,
        "confidence_note": "Exact extraction from the XML. Confidence 1.0 refers to "
                           "the READING, not to the architectural INTERPRETATION.",
        "pages": pages,
        "stats": {"pages": len(pages), "nodes": n_nodes, "edges": n_edges},
        "warnings": warnings,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2

    src = Path(args[0])
    if not src.exists():
        print(f"ERROR: {src} does not exist")
        return 1

    try:
        result = parse_file(src)
    except ET.ParseError as e:
        print(f"ERROR: invalid XML in {src}: {e}")
        return 1

    out_path = None
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    s = result["stats"]
    print(f"source: {src}")
    print(f"sha256: {result['source_sha256'][:16]}...")
    print(f"read  : {s['pages']} page(s), {s['nodes']} node(s), {s['edges']} edge(s)")
    for p in result["pages"]:
        print(f"\n  [{p['page']}]")
        for n in p["nodes"]:
            flag = " (container)" if n["is_container"] else ""
            print(f"    node {n['cell_id']:>6}  {n['label'][:38]:<38} ~{n['shape_hint']}{flag}")
        for e in p["edges"]:
            arrow = "-->" if e["directed"] else "---"
            lbl = f'  "{e["label"]}"' if e["label"] else ""
            print(f"    edge {e['cell_id']:>6}  {str(e['source_label'])[:16]:<16} "
                  f"{arrow} {str(e['target_label'])[:16]:<16} ~{e['protocol_hint']}{lbl}")

    if result["warnings"]:
        print(f"\n  {len(result['warnings'])} warning(s) for the arch-analyst:")
        for w in result["warnings"]:
            print(f"    - {w}")

    if out_path:
        print(f"\nwritten: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
