"""draw.io / diagrams.net — ``.drawio``, ``.xml``, and the XML hidden in a ``.png``.

A ``.drawio`` stores its graph one of two ways: plain ``<mxGraphModel>`` inside
``<diagram>``, or the app's default — base64, then **raw** deflate (window ``-15``,
no zlib header), then URL-encoding. Both are handled.

The draw.io PNG case is the interesting one. draw.io's "Export as PNG" writes the
entire source diagram into a PNG ``tEXt`` chunk keyed ``mxfile``. Nobody in this
repo looked for it, so every draw.io PNG was routed to the vision model to
re-derive, probabilistically and at token cost, a graph that was already sitting
in the file byte-for-byte. :class:`DrawioPngAdapter` reads the chunk for the graph
and still emits the pixels, so the reviewer gets both the exact answer and the
picture to check it against.

Node geometry is normalized against the union bounding box of the page's own
cells rather than against ``mxGraphModel/@pageWidth``: a diagram routinely spills
outside the nominal page, and normalizing against the page would put boxes
outside 0..1 — the exact defect ``vision.py`` had to build a whole convention
detector to survive.
"""

from __future__ import annotations

import base64
import urllib.parse
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import CorruptSource
from ..hints import kind_hint, protocol_hint, strip_html
from ..models import BBox, GraphEdge, GraphNode, NormalizedPage, PageRef, StructuredGraph
from ..raster import MAX_EDGE
from .base import Adapter

__all__ = ["DrawioAdapter", "DrawioPngAdapter", "parse_mxfile"]

EXTRACTOR = "ingest.adapters.drawio@1.0"


def _decode_diagram(node: ET.Element) -> ET.Element | None:
    """Return the ``<mxGraphModel>`` of one tab, compressed or not."""
    inner = node.find("mxGraphModel")
    if inner is not None:
        return inner
    payload = (node.text or "").strip()
    if not payload:
        return None
    try:
        raw = base64.b64decode(payload)
        xml = zlib.decompress(raw, -15).decode("utf-8")
        return ET.fromstring(urllib.parse.unquote(xml))
    except Exception:  # noqa: BLE001 - one unreadable tab must not lose the others
        return None


def _geometry(cell: ET.Element) -> tuple[float, float, float, float] | None:
    geo = cell.find("mxGeometry")
    if geo is None:
        return None
    try:
        x = float(geo.get("x") or 0.0)
        y = float(geo.get("y") or 0.0)
        w = float(geo.get("width") or 0.0)
        h = float(geo.get("height") or 0.0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _normalize_boxes(raw: dict[str, tuple[float, float, float, float]]) -> dict[str, BBox]:
    """Map absolute mxGeometry into 0..1 against the drawing's own extent."""
    if not raw:
        return {}
    min_x = min(v[0] for v in raw.values())
    min_y = min(v[1] for v in raw.values())
    max_x = max(v[0] + v[2] for v in raw.values())
    max_y = max(v[1] + v[3] for v in raw.values())
    span_x = max_x - min_x or 1.0
    span_y = max_y - min_y or 1.0
    return {
        cid: BBox(
            x=round((x - min_x) / span_x, 5),
            y=round((y - min_y) / span_y, 5),
            w=round(w / span_x, 5),
            h=round(h / span_y, 5),
        )
        for cid, (x, y, w, h) in raw.items()
    }


def parse_mxfile(xml_text: str, *, source: str = "drawio") -> StructuredGraph:
    """Parse a full ``<mxfile>`` document (or a bare ``<mxGraphModel>``)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CorruptSource(
            "ingest_drawio_unparseable",
            "That draw.io file is not valid XML",
            f"{exc}",
            remedy=(
                "Open the file in diagrams.net (File > Open from > Device). If it opens "
                "there, use File > Save as > .drawio to write a clean copy and upload "
                "that. If it does not open, restore it from the tool's revision history."
            ),
        ) from exc

    diagrams = root.findall(".//diagram") if root.tag != "mxGraphModel" else []
    graph = StructuredGraph(format=source, extractor=EXTRACTOR)
    page_models: list[tuple[str, ET.Element]] = []

    if root.tag == "mxGraphModel":
        page_models.append(("Page-1", root))
    else:
        for index, node in enumerate(diagrams, start=1):
            model = _decode_diagram(node)
            name = node.get("name") or f"Page-{index}"
            if model is None:
                graph.warnings.append(
                    f"Tab '{name}' could not be decompressed and was skipped. "
                    "Re-save it from diagrams.net with File > Save as > .drawio "
                    "(uncompressed) if its contents matter."
                )
                continue
            page_models.append((name, model))

    if not page_models:
        raise CorruptSource(
            "ingest_drawio_no_pages",
            "That draw.io file has no readable page",
            "No <diagram> tab in the file could be decoded.",
            remedy=(
                "Open it in diagrams.net and use File > Save as > .drawio. If the file "
                "came from an email or chat client it may have been re-encoded in "
                "transit; ask for it again as an attachment, zipped."
            ),
        )

    graph.pages = len(page_models)
    graph.page_names = [name for name, _ in page_models]

    for page_index, (page_name, model) in enumerate(page_models, start=1):
        model_root = model.find("root")
        if model_root is None:
            continue
        cells = model_root.findall("mxCell") + model_root.findall(".//object")
        labels: dict[str, str] = {}
        raw_boxes: dict[str, tuple[float, float, float, float]] = {}
        pending_nodes: list[tuple[str, str, str]] = []  # (id, label, style)
        pending_edges: list[tuple[str, str, str, str]] = []  # (id, src, tgt, label)

        for cell in cells:
            inner = cell.find("mxCell") if cell.tag == "object" else cell
            if inner is None:
                continue
            cid = cell.get("id") or inner.get("id")
            if not cid:
                continue
            style = inner.get("style") or ""
            label = strip_html(cell.get("label") or inner.get("value") or "")
            if inner.get("edge") == "1":
                pending_edges.append(
                    (cid, inner.get("source") or "", inner.get("target") or "", label)
                )
                labels[cid] = label
                continue
            if inner.get("vertex") != "1":
                continue  # the two structural root cells, id 0 and 1
            geo = _geometry(inner)
            if geo:
                raw_boxes[cid] = geo
            labels[cid] = label
            pending_nodes.append((cid, label, style))

        boxes = _normalize_boxes(raw_boxes)
        for cid, label, style in pending_nodes:
            graph.nodes.append(
                GraphNode(
                    id=cid,
                    label=label,
                    kind_hint=kind_hint(style, label),
                    page=page_index,
                    bbox=boxes.get(cid),
                    evidence=f"{page_name}:mxCell[@id='{cid}']",
                    attrs={"style": style[:400]} if style else {},
                )
            )
        for eid, src, tgt, label in pending_edges:
            if not src or not tgt:
                graph.warnings.append(
                    f"Edge '{eid}' on tab '{page_name}' is not attached at both ends "
                    f"(source={src or 'none'}, target={tgt or 'none'}) and was kept as a "
                    "dangling edge. In diagrams.net, drag each endpoint onto the shape "
                    "until the outline turns green."
                )
            graph.edges.append(
                GraphEdge(
                    id=eid,
                    source=src,
                    target=tgt,
                    label=label,
                    protocol_hint=protocol_hint(label),
                    page=page_index,
                    evidence=f"{page_name}:mxCell[@id='{eid}']",
                    attrs={
                        "source_label": labels.get(src, ""),
                        "target_label": labels.get(tgt, ""),
                    },
                )
            )
    return graph


class DrawioAdapter(Adapter):
    id = "drawio"
    label = "draw.io diagram"
    produces_structure = True

    def pages(self, path: Path) -> list[PageRef]:
        graph = self.extract(path)
        if graph is None:
            return super().pages(path)
        return [
            PageRef(
                index=i,
                name=name,
                content="vector",
                text_chars=sum(len(n.label) for n in graph.nodes if n.page == i),
                likely_diagram=any(n.page == i for n in graph.nodes),
            )
            for i, name in enumerate(graph.page_names or ["Page-1"], start=1)
        ]

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        text = path.read_text(encoding="utf-8", errors="replace")
        graph = parse_mxfile(text, source="drawio")
        return _filter_pages(graph, pages)


class DrawioPngAdapter(Adapter):
    """A PNG that still carries its draw.io source: exact graph *and* pixels."""

    id = "drawio_png"
    label = "draw.io PNG export"
    produces_structure = True
    produces_raster = True

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph | None:
        from ..detect import _png_embedded_drawio  # local import: avoids a cycle

        xml_text = _png_embedded_drawio(path.read_bytes())
        if not xml_text:
            return None
        graph = parse_mxfile(xml_text, source="drawio-png")
        graph.warnings.append(
            "The graph was read from the XML embedded in this PNG, not from its "
            "pixels. It is exact and cost no inference."
        )
        return _filter_pages(graph, pages)

    def rasterize(
        self, path: Path, out_dir: Path, pages: list[int] | None = None,
        *, max_edge: int = MAX_EDGE,
    ) -> list[NormalizedPage]:
        from .image import ImageAdapter

        return ImageAdapter().rasterize(path, out_dir, pages, max_edge=max_edge)


def _filter_pages(graph: StructuredGraph, pages: list[int] | None) -> StructuredGraph:
    """Keep only the chosen tabs. ``None`` means every tab."""
    if not pages:
        return graph
    keep = set(pages)
    graph.nodes = [n for n in graph.nodes if n.page in keep]
    graph.edges = [e for e in graph.edges if e.page in keep]
    return graph
