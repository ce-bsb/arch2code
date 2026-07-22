"""SVG — three very different files wearing the same extension.

1. **draw.io's "Export as SVG" with *Include a copy of my diagram* ticked.** The
   root ``<svg>`` carries a ``content`` attribute holding the entire ``<mxfile>``.
   That is a complete, exact graph and it is checked for first — the same free win
   as the draw.io PNG.
2. **A normal vector export** (Lucid, Figma, Excalidraw, Mermaid CLI, Graphviz).
   ``<text>`` elements give exact labels with exact positions; nothing in SVG says
   which line connects which box, so there are labels and no edges. Reported as
   such, never dressed up as a full extraction.
3. **An SVG whose text was converted to outlines.** Every label is a ``<path>``.
   There is nothing to read, and no amount of parsing changes that — so this is a
   refusal with the one instruction that fixes it, not an empty result.

Positions are normalized against ``viewBox`` when present, and against the union
of the coordinates seen otherwise. Text width is *estimated* from the character
count and font size, because computing a real glyph advance would mean shipping a
font engine; the estimate is labelled in the node attrs so nobody mistakes it for
a measurement.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import CorruptSource, UnsupportedFormat
from ..hints import kind_hint, strip_html
from ..models import BBox, GraphNode, PageRef, StructuredGraph
from .base import Adapter

__all__ = ["SvgAdapter"]

EXTRACTOR = "ingest.adapters.svg@1.0"

_SVG_NS = "http://www.w3.org/2000/svg"
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
#: Mean glyph advance as a fraction of font size for a proportional sans face.
#: An estimate, and named as one wherever it is used.
_GLYPH_RATIO = 0.55


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _float(value: str | None, default: float = 0.0) -> float:
    if not value:
        return default
    match = _NUM_RE.search(value)
    return float(match.group()) if match else default


def _font_size(element: ET.Element) -> float:
    size = element.get("font-size")
    if not size:
        style = element.get("style") or ""
        match = re.search(r"font-size\s*:\s*([\d.]+)", style)
        size = match.group(1) if match else None
    return _float(size, 12.0) or 12.0


class SvgAdapter(Adapter):
    id = "svg"
    label = "SVG drawing"
    produces_structure = True

    def pages(self, path: Path) -> list[PageRef]:
        return [PageRef(index=1, name="", content="vector", likely_diagram=True)]

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise CorruptSource(
                "ingest_svg_unparseable",
                "That SVG is not valid XML",
                str(exc),
                remedy=(
                    "Re-export the drawing as SVG from the tool that made it. If it "
                    "came out of a copy-paste, the file is probably missing its "
                    "closing tag."
                ),
            ) from exc

        # Case 1: draw.io kept its own source inside the export.
        embedded = root.get("content") or ""
        if "<mxfile" in embedded or "<mxGraphModel" in embedded:
            from .drawio import parse_mxfile

            graph = parse_mxfile(strip_html(embedded) if "&lt;" in embedded else embedded,
                                 source="drawio-svg")
            graph.warnings.append(
                "This SVG was exported from draw.io with a copy of the diagram "
                "included, so the graph was read from that XML exactly — no geometry "
                "was guessed and no tokens were spent."
            )
            return graph

        # Case 2: read the labels.
        view_box = [_float(v) for v in (root.get("viewBox") or "").replace(",", " ").split()]
        if len(view_box) == 4 and view_box[2] and view_box[3]:
            origin_x, origin_y, span_x, span_y = view_box
        else:
            origin_x, origin_y = 0.0, 0.0
            span_x = _float(root.get("width"), 0.0)
            span_y = _float(root.get("height"), 0.0)

        graph = StructuredGraph(format="svg", extractor=EXTRACTOR)
        collected: list[tuple[str, float, float, float, float, str]] = []
        for index, element in enumerate(root.iter()):
            if _local(element.tag) != "text":
                continue
            label = " ".join("".join(element.itertext()).split())
            if not label:
                continue
            size = _font_size(element)
            x = _float(element.get("x"))
            y = _float(element.get("y"))
            if x == 0.0 and y == 0.0:
                child = next((c for c in element if _local(c.tag) == "tspan"), None)
                if child is not None:
                    x, y = _float(child.get("x")), _float(child.get("y"))
            width = len(label) * size * _GLYPH_RATIO
            anchor = element.get("text-anchor") or ""
            if anchor == "middle":
                x -= width / 2
            elif anchor == "end":
                x -= width
            collected.append((label, x, y - size, width, size, f"text[{index}]"))

        if not collected:
            raise UnsupportedFormat(
                "ingest_svg_no_text",
                "That SVG contains no readable text",
                f"{path.name} has no <text> element, so every label in it was "
                "converted to outlines (paths) when it was exported.",
                remedy=(
                    "Re-export with text kept as text: in draw.io untick 'Convert "
                    "shapes to sketch'/'Embed fonts'; in Figma or Illustrator do not "
                    "'Outline text'; in Inkscape use Path > Object to Path only if you "
                    "must. Alternatively export the drawing as PNG or PDF and upload "
                    "that — it goes down the vision path and is read from pixels."
                ),
            )

        if not span_x or not span_y:
            span_x = max(x + w for _, x, _, w, _, _ in collected) or 1.0
            span_y = max(y + h for _, _, y, _, h, _ in collected) or 1.0

        for order, (label, x, y, width, height, evidence) in enumerate(collected):
            graph.nodes.append(
                GraphNode(
                    id=f"svg-text-{order}",
                    label=label,
                    kind_hint=kind_hint(label, default="text_span"),
                    bbox=BBox(
                        x=round((x - origin_x) / span_x, 5),
                        y=round((y - origin_y) / span_y, 5),
                        w=round(width / span_x, 5),
                        h=round(height / span_y, 5),
                    ),
                    evidence=evidence,
                    attrs={"width_is_estimated": True},
                )
            )
        graph.warnings.append(
            "SVG stores drawing instructions, not relationships: these labels and "
            "their positions are exact, but which line joins which box is not "
            "recorded anywhere in the file. Edges still need either the source "
            "format (.drawio, .vsdx) or the vision path."
        )
        return graph
