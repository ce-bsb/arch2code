"""Visio 2013+ (``.vsdx``) — real shapes and real connectors, no Visio required.

A ``.vsdx`` is a ZIP of XML, and it records connections explicitly: every
``<Connect FromSheet="7" FromCell="BeginX" ToSheet="1"/>`` says *connector 7
starts at shape 1*. That is an edge, stated by the tool, with an id that can be
looked up again. There is no inference anywhere in this adapter — which is the
whole argument for preferring a structured source over a picture of one.

The ``vsdx`` package is pure Python (its only dependency is Jinja2), so this
costs no system binary. LibreOffice is deliberately not installed: it would drag
in ~400 MB, needs a font package or it silently renders empty rectangles where
text should be, and is not concurrency-safe with a shared profile. The legacy
binary ``.vsd`` that would need it is a documented refusal in ``formats.py`` with
the two-click conversion in its remedy.

Geometry: Visio's origin is bottom-left and ``x``/``y`` are the shape's *centre*
(PinX/PinY), in inches. Both are converted to this app's convention — top-left
origin, 0..1, ``x``/``y`` at the top-left corner — so a Visio bbox and a vision
bbox can be drawn on the same overlay.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..errors import CorruptSource, MissingDependency
from ..hints import kind_hint, protocol_hint
from ..models import BBox, GraphEdge, GraphNode, PageRef, StructuredGraph
from .base import Adapter

__all__ = ["VsdxAdapter"]

EXTRACTOR = "ingest.adapters.vsdx@1.0"


def _open(path: Path):
    try:
        from vsdx import VisioFile
    except ImportError as exc:
        raise MissingDependency(
            "ingest_vsdx_reader_missing",
            "Visio files cannot be read on this install",
            "The `vsdx` package is not installed.",
            remedy=(
                "`<ARCH2CODE_PYTHON> -m pip install vsdx` (pure Python, no Visio and no "
                "LibreOffice needed). Until then, open the drawing in Visio and use "
                "File > Export > PDF, then upload the PDF."
            ),
        ) from exc
    try:
        return VisioFile(str(path))
    except Exception as exc:  # noqa: BLE001 - the package raises a wide family
        raise CorruptSource(
            "ingest_vsdx_unreadable",
            "That Visio file could not be opened",
            f"{path.name}: {type(exc).__name__}: {exc}",
            remedy=(
                "Open it in Visio and re-save with File > Save As > .vsdx. If it came "
                "from Lucidchart or another web tool, re-export it — some exporters "
                "write a package Visio itself opens but that is not valid OPC."
            ),
        ) from exc


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


class VsdxAdapter(Adapter):
    id = "vsdx"
    label = "Visio drawing"
    produces_structure = True

    def pages(self, path: Path) -> list[PageRef]:
        with _open(path) as vis:
            refs = []
            for index, page in enumerate(vis.pages, start=1):
                if getattr(page, "is_master_page", False):
                    continue
                shapes = list(page.all_shapes)
                refs.append(
                    PageRef(
                        index=index,
                        name=page.name or f"page {index}",
                        width=_number(page.width) or 0.0,
                        height=_number(page.height) or 0.0,
                        content="vector",
                        text_chars=sum(len((s.text or "").strip()) for s in shapes),
                        likely_diagram=bool(shapes),
                    )
                )
        return refs or [PageRef(index=1, likely_diagram=True)]

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph:
        keep = set(pages) if pages else None
        graph = StructuredGraph(format="vsdx", extractor=EXTRACTOR)
        with _open(path) as vis:
            page_list = [p for p in vis.pages if not getattr(p, "is_master_page", False)]
            graph.pages = len(page_list)
            graph.page_names = [p.name or f"page {i}" for i, p in enumerate(page_list, 1)]

            for page_index, page in enumerate(page_list, start=1):
                if keep is not None and page_index not in keep:
                    continue
                page_w = _number(page.width) or 0.0
                page_h = _number(page.height) or 0.0

                connects = list(getattr(page, "connects", []) or [])
                by_connector: dict[str, list] = defaultdict(list)
                for connect in connects:
                    if connect.from_id:
                        by_connector[str(connect.from_id)].append(connect)
                connector_ids = set(by_connector)

                labels: dict[str, str] = {}
                for shape in page.all_shapes:
                    shape_id = str(getattr(shape, "ID", "") or "")
                    if not shape_id:
                        continue
                    text = " ".join((shape.text or "").split())
                    labels[shape_id] = text
                    if shape_id in connector_ids:
                        continue  # the connector itself is an edge, not a node
                    master = ""
                    try:
                        master_page = shape.master_page
                        master = getattr(master_page, "name", "") or ""
                    except Exception:  # noqa: BLE001 - masters are optional
                        master = ""
                    graph.nodes.append(
                        GraphNode(
                            id=f"p{page_index}-{shape_id}",
                            label=text,
                            kind_hint=kind_hint(master, text),
                            page=page_index,
                            bbox=self._bbox(shape, page_w, page_h),
                            evidence=f"{page.name}:Shape[@ID='{shape_id}']",
                            attrs={"master": master} if master else {},
                        )
                    )

                for connector_id, group in by_connector.items():
                    begin = next(
                        (c.to_id for c in group if (c.from_rel or "").startswith("Begin")),
                        None,
                    )
                    end = next(
                        (c.to_id for c in group if (c.from_rel or "").startswith("End")),
                        None,
                    )
                    if begin is None and end is None:
                        continue
                    if begin is None or end is None:
                        graph.warnings.append(
                            f"Connector {connector_id} on page '{page.name}' is glued at "
                            "only one end, so its direction is a guess. In Visio, drag "
                            "the loose endpoint onto the shape until the green square "
                            "appears, then re-export."
                        )
                    label = labels.get(str(connector_id), "")
                    graph.edges.append(
                        GraphEdge(
                            id=f"p{page_index}-c{connector_id}",
                            source=f"p{page_index}-{begin}" if begin else "",
                            target=f"p{page_index}-{end}" if end else "",
                            label=label,
                            protocol_hint=protocol_hint(label),
                            page=page_index,
                            evidence=f"{page.name}:Connect[@FromSheet='{connector_id}']",
                            attrs={
                                "source_label": labels.get(str(begin), ""),
                                "target_label": labels.get(str(end), ""),
                            },
                        )
                    )

        if graph.nodes and not graph.edges:
            graph.warnings.append(
                "Shapes were found but no connector is glued to anything. Lines drawn "
                "next to shapes without snapping to a connection point are decoration "
                "as far as the file is concerned. Re-connect them in Visio, or upload a "
                "PDF/PNG export and let the vision path read the arrows from pixels."
            )
        return graph

    @staticmethod
    def _bbox(shape, page_w: float, page_h: float) -> BBox | None:
        if not page_w or not page_h:
            return None
        x = _number(getattr(shape, "x", None))
        y = _number(getattr(shape, "y", None))
        w = _number(getattr(shape, "width", None))
        h = _number(getattr(shape, "height", None))
        if None in (x, y, w, h) or not w or not h:
            return None
        # Visio: origin bottom-left, (x, y) is the centre. Ours: origin top-left,
        # (x, y) is the top-left corner.
        return BBox(
            x=round((x - w / 2) / page_w, 5),
            y=round((page_h - (y + h / 2)) / page_h, 5),
            w=round(w / page_w, 5),
            h=round(h / page_h, 5),
        )
