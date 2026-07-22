"""Wire and disk shapes for ingest.

Two contracts live here and neither may drift:

1. **:class:`IngestSummary`** is embedded in ``UploadRef`` and therefore is part of
   the upload API response. The UI reads ``requires_page_selection`` to decide
   whether to ask a question before a run can start, and ``structure_available``
   to decide whether to grey out the vision button.
2. **:class:`StructuredGraph`** is what every structural adapter returns and what
   ``structured.json`` contains. It is intentionally the same shape for a
   ``.drawio``, a ``.vsdx`` and a ``.mmd``: the analyst stage downstream must not
   have to know which parser produced it.

This module imports nothing from ``app`` and nothing heavier than pydantic, on
purpose — see ``errors.py`` for why.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Capability",
    "DetectionLevel",
    "BBox",
    "GraphNode",
    "GraphEdge",
    "StructuredGraph",
    "PageRef",
    "IngestSummary",
    "IngestReport",
    "NormalizedPage",
    "NormalizeResult",
]

#: What an adapter produces.
#:
#: ``structure`` nodes and edges, no pixels needed — the cheapest and the only
#:               path that cannot hallucinate.
#: ``raster``    pixels only; the vision model is the sole reader.
#: ``hybrid``    both are available from the same bytes (a vector PDF, a drawio
#:               PNG). Structure is still tried first; the raster is the fallback
#:               and the human-visible evidence.
#: ``refuse``    recognized and not readable here. Carries a remedy, never silence.
Capability = Literal["structure", "raster", "hybrid", "refuse"]

#: Which of the three cascade levels identified the file. ``extension`` means the
#: content was inconclusive and the suffix broke the tie; ``none`` means nothing
#: matched at all.
DetectionLevel = Literal["signature", "container", "text", "extension", "none"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BBox(_Model):
    """Normalized 0..1 box, origin top-left — the same convention ``vision.py`` emits.

    Structural adapters know the page size, so they can normalize exactly. When a
    format gives no geometry at all the field is simply absent rather than zero:
    a box at (0,0,0,0) draws a dot on the overlay and looks like a real answer.
    """

    x: float
    y: float
    w: float
    h: float


class GraphNode(_Model):
    id: str
    label: str = ""
    #: A *hint*, never a decision. Naming a thing a "database" is the analyst's
    #: job with a human in the loop; a parser only sees a cylinder.
    kind_hint: str = "unknown"
    page: int = 1
    bbox: BBox | None = None
    #: Where this came from, verbatim and re-findable in the source file: an
    #: ``mxCell`` id, a ``<bpmn:task id>``, a VSDX shape id, a line number.
    evidence: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(_Model):
    id: str
    source: str
    target: str
    label: str = ""
    directed: bool = True
    #: A hint at the protocol, derived from the label. Same caveat as ``kind_hint``.
    protocol_hint: str = "unknown"
    page: int = 1
    evidence: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)


class StructuredGraph(_Model):
    """The deterministic extraction. Zero tokens, zero hallucination, full traceability."""

    format: str
    #: ``<module>@<version>`` of the parser, so a bad extraction is attributable.
    extractor: str
    pages: int = 1
    page_names: list[str] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    #: Things the parser could see but not resolve. Rendered, never swallowed.
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.nodes and not self.edges


class PageRef(_Model):
    """One page of a multi-page source, with enough facts for a human to choose.

    ``text_chars`` is the discriminator that matters: a vector PDF page exported
    from a modelling tool has thousands of characters of real text, a scanned page
    has zero. The UI shows it so the user is not guessing which of 30 pages holds
    the architecture.
    """

    index: int  # 1-based, as printed on the page
    name: str = ""
    width: float = 0.0
    height: float = 0.0
    #: ``vector`` | ``raster`` | ``mixed`` | ``unknown``
    content: str = "unknown"
    text_chars: int = 0
    #: True when this page looks like the one carrying the diagram.
    likely_diagram: bool = False


class IngestSummary(_Model):
    """The part of the ingest verdict that travels back on the upload response."""

    format_id: str
    format_label: str
    family: str
    capability: Capability
    mime: str
    detected_by: DetectionLevel
    #: The suffix the user's file arrived with, lowercased, ``""`` when it had none.
    extension: str = ""
    #: False when the bytes contradict the suffix (a PDF named ``.png``). The upload
    #: is refused in that case; the flag exists so the message can be specific.
    extension_agrees: bool = True

    page_count: int = 1
    #: True when the source has more than one page and nobody has said which one.
    #: The API returns this so the UI can ASK. Choosing silently is how a 30-page
    #: PDF becomes 30 vision calls and a four-figure token bill.
    requires_page_selection: bool = False
    #: What the app would suggest, 1-based. Never applied without confirmation.
    suggested_pages: list[int] = Field(default_factory=list)

    structure_available: bool = False
    structure_nodes: int = 0
    structure_edges: int = 0
    #: True when pixels are the only way in — i.e. tokens will be spent.
    vision_required: bool = False

    notes: list[str] = Field(default_factory=list)
    #: Present only for ``capability == "refuse"``.
    remedy: str | None = None


class IngestReport(_Model):
    """Everything ingest learned about one file. Superset of :class:`IngestSummary`."""

    summary: IngestSummary
    pages: list[PageRef] = Field(default_factory=list)
    graph: StructuredGraph | None = None


class NormalizedPage(_Model):
    """One PNG the vision path may read."""

    index: int
    path: str
    width: int
    height: int
    scale: float = 1.0
    #: ``copy`` (already a PNG within budget) | ``convert`` | ``render`` (rasterized)
    origin: str = "convert"
    render_dpi: float | None = None


class NormalizeResult(_Model):
    """Output contract of :func:`app.ingest.normalize.normalize`.

    Always the same pair, whatever went in: a ``structured.json`` when structure
    was extractable, and a list of normalized PNGs for the vision path. Everything
    downstream keeps seeing exactly what it sees today.
    """

    summary: IngestSummary
    structured_path: str | None = None
    pages: list[NormalizedPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
