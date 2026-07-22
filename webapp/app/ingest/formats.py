"""The format registry: one row per format this app has an opinion about.

Every row declares, as data rather than as code:

* what the format **is** (id, label, family, mime, extensions);
* whether reading it yields **structure**, **pixels**, **both**, or nothing;
* which adapter implements it, or — for a refusal — **exactly how the user
  converts their file into something we do read**.

The refusal rows are the important half. A format we cannot open is not an
absence of a row; it is a row whose ``remedy`` is a sentence a person can act on.
"Unsupported file type" with no next step is the failure mode this table exists
to make impossible, and ``tests/test_ingest_formats.py`` fails the build if a
``refuse`` row ships without a remedy.

Ordering note: the table is a dict keyed by format id, and lookup by extension is
built from it once at import. When two formats claim the same extension (``.xml``
is drawio, BPMN, ArchiMate and plain XML all at once) the extension index keeps
the first declared and detection resolves the ambiguity from content — the
extension is only ever a tie-breaker.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Capability

__all__ = ["FormatSpec", "FORMATS", "by_extension", "spec_for", "UNKNOWN", "accepted_extensions"]


@dataclass(frozen=True)
class FormatSpec:
    id: str
    label: str
    #: Coarse grouping used by the UI to pick an icon and a phrase.
    family: str
    capability: Capability
    mime: str = "application/octet-stream"
    extensions: tuple[str, ...] = ()
    #: Key into ``adapters.ADAPTERS``. ``None`` for refusals.
    adapter: str | None = None
    #: Mandatory when ``capability == "refuse"``.
    remedy: str | None = None
    #: True when the format can carry more than one page/tab/sheet.
    multipage: bool = False
    #: True when the structural read yields labels but cannot yield relationships.
    #: SVG and PDF are both like this: they store drawing instructions, so the text
    #: is exact and "which line joins which box" is nowhere in the file. Such a
    #: format still needs the vision path for its edges, and
    #: :func:`app.ingest.normalize._vision_required` reads this flag to say so
    #: instead of silently reporting a three-node, zero-edge graph as complete.
    partial_structure: bool = False
    #: Free-text note surfaced on the upload so the user knows what they will get.
    note: str = ""


def _f(**kw) -> FormatSpec:
    return FormatSpec(**kw)


#: Every format, keyed by id.
FORMATS: dict[str, FormatSpec] = {
    # ---------------------------------------------------------------- raster
    "png": _f(
        id="png", label="PNG image", family="image", capability="raster",
        mime="image/png", extensions=(".png",), adapter="image",
    ),
    "jpeg": _f(
        id="jpeg", label="JPEG image", family="image", capability="raster",
        mime="image/jpeg", extensions=(".jpg", ".jpeg"), adapter="image",
    ),
    "webp": _f(
        id="webp", label="WebP image", family="image", capability="raster",
        mime="image/webp", extensions=(".webp",), adapter="image",
    ),
    "gif": _f(
        id="gif", label="GIF image", family="image", capability="raster",
        mime="image/gif", extensions=(".gif",), adapter="image",
        note="Only the first frame is read; an animated GIF loses every later frame.",
    ),
    "bmp": _f(
        id="bmp", label="BMP image", family="image", capability="raster",
        mime="image/bmp", extensions=(".bmp",), adapter="image",
    ),
    "tiff": _f(
        id="tiff", label="TIFF image", family="image", capability="raster",
        mime="image/tiff", extensions=(".tif", ".tiff"), adapter="image",
        multipage=True,
        note="A multi-page TIFF is treated as multiple pages, like a PDF.",
    ),
    "heic": _f(
        id="heic", label="HEIC/HEIF photo (iPhone)", family="image", capability="raster",
        mime="image/heic", extensions=(".heic", ".heif"), adapter="image",
        note="Decoded with pillow-heif and converted to PNG. The watsonx endpoint "
             "never sees HEIC — it does not accept it.",
    ),
    # ------------------------------------------------------ raster + structure
    "drawio-png": _f(
        id="drawio-png", label="PNG exported from draw.io (diagram embedded)",
        family="image", capability="hybrid", mime="image/png",
        extensions=(".png",), adapter="drawio_png",
        note="This PNG carries the original draw.io XML in a tEXt chunk. The graph "
             "is read exactly from that chunk, for free — no vision call needed.",
    ),
    # ------------------------------------------------------------------- pdf
    "pdf": _f(
        id="pdf", label="PDF document", family="document", capability="hybrid",
        mime="application/pdf", extensions=(".pdf",), adapter="pdf", multipage=True,
        partial_structure=True,
        note="Text and page geometry are read directly; pages are rasterized only "
             "when a page carries no usable text.",
    ),
    # ------------------------------------------------------------ xml graphs
    "drawio": _f(
        id="drawio", label="draw.io / diagrams.net", family="diagram",
        capability="structure", mime="application/xml",
        extensions=(".drawio", ".xml"), adapter="drawio", multipage=True,
    ),
    "bpmn": _f(
        id="bpmn", label="BPMN 2.0 process model", family="diagram",
        capability="structure", mime="application/xml",
        extensions=(".bpmn", ".bpmn2", ".xml"), adapter="bpmn",
    ),
    "archimate": _f(
        id="archimate", label="ArchiMate model", family="diagram",
        capability="structure", mime="application/xml",
        extensions=(".archimate", ".xml"), adapter="archimate",
        note="Both dialects are read: Archi's native EMF file and the Open Group "
             "Model Exchange format.",
    ),
    "svg": _f(
        id="svg", label="SVG drawing", family="vector", capability="structure",
        mime="image/svg+xml", extensions=(".svg",), adapter="svg",
        partial_structure=True,
        note="Labels come from <text> elements. An SVG whose text was converted to "
             "outlines carries no text at all and will be refused with instructions.",
    ),
    # ------------------------------------------------------------ text graphs
    "plantuml": _f(
        id="plantuml", label="PlantUML source", family="text-diagram",
        capability="structure", mime="text/plain",
        extensions=(".puml", ".plantuml", ".iuml", ".pu"), adapter="plantuml",
    ),
    "mermaid": _f(
        id="mermaid", label="Mermaid source", family="text-diagram",
        capability="structure", mime="text/plain",
        extensions=(".mmd", ".mermaid"), adapter="mermaid",
    ),
    # ------------------------------------------------------------------ office
    "vsdx": _f(
        id="vsdx", label="Visio drawing (2013+)", family="office",
        capability="structure", mime="application/vnd.ms-visio.drawing",
        extensions=(".vsdx", ".vsdm"), adapter="vsdx", multipage=True,
        note="Shapes and real connectors are read from the package; no Visio and no "
             "LibreOffice are involved.",
    ),
    # -------------------------------------------------------------- databases
    "staruml": _f(
        id="staruml", label="StarUML model", family="model", capability="structure",
        mime="application/json", extensions=(".mdj", ".mfj"), adapter="staruml",
    ),
    "sparx-qea": _f(
        id="sparx-qea", label="Sparx Enterprise Architect 16+ repository",
        family="model", capability="structure", mime="application/vnd.sqlite3",
        extensions=(".qea", ".qeax"), adapter="sparx_qea",
        note="Read straight out of the SQLite repository: t_object is the node "
             "table, t_connector is the edge table.",
    ),
    # ------------------------------------------------------------- refusals
    "pptx": _f(
        id="pptx", label="PowerPoint presentation", family="office", capability="refuse",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        extensions=(".pptx", ".pptm"),
        remedy="Open the deck, select the slide with the architecture, and use "
               "File > Export > PDF (or right-click the diagram > Save as Picture > PNG). "
               "Upload that. Exporting one slide also stops the other 40 slides from "
               "becoming 40 vision calls.",
    ),
    "docx": _f(
        id="docx", label="Word document", family="office", capability="refuse",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        extensions=(".docx", ".docm"),
        remedy="Right-click the diagram inside the document and choose "
               "'Save as Picture' (PNG), or export the whole file as PDF with "
               "File > Export. Upload that instead.",
    ),
    "xlsx": _f(
        id="xlsx", label="Excel workbook", family="office", capability="refuse",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extensions=(".xlsx", ".xlsm"),
        remedy="A spreadsheet is not an architecture drawing. If the diagram is a "
               "picture on a sheet, right-click it and save it as PNG; if the "
               "architecture is tabular data, export it as CSV and describe it in "
               "the run hint instead.",
    ),
    "visio-legacy": _f(
        id="visio-legacy", label="Visio binary drawing (2003-2010)", family="office",
        capability="refuse", mime="application/vnd.visio", extensions=(".vsd", ".vdx"),
        remedy="This is the pre-2013 binary Visio format; reading it needs "
               "LibreOffice, which this build deliberately does not ship. Open it in "
               "Visio (or LibreOffice Draw) and use File > Save As > .vsdx — that "
               "format is read exactly, with the real connectors.",
    ),
    "opendocument-graphics": _f(
        id="opendocument-graphics", label="OpenDocument drawing/presentation",
        family="office", capability="refuse", mime="application/vnd.oasis.opendocument.graphics",
        extensions=(".odg", ".odp"),
        remedy="From LibreOffice use File > Export as PDF, or File > Export > SVG. "
               "Both of those are read here.",
    ),
    "miro-rtb": _f(
        id="miro-rtb", label="Miro board archive", family="whiteboard",
        capability="refuse", mime="application/zip", extensions=(".rtb",),
        remedy="Miro's board archive has no published schema. From the board, use "
               "Export > Save as image (PNG) or Export > PDF, and upload that. If the "
               "board is large, export the single frame that holds the architecture — "
               "a full board rasterizes to text no model can read.",
    ),
    "sparx-eap": _f(
        id="sparx-eap", label="Sparx Enterprise Architect repository (Access/JET)",
        family="model", capability="refuse", mime="application/x-msaccess",
        extensions=(".eap", ".eapx"),
        remedy="This is the old Access-backed EA repository, which needs the mdbtools "
               "system binary this build does not install. In Enterprise Architect use "
               "File > Save Project As and pick the .qea (SQLite) format — that is read "
               "here natively. Publishing the diagram as XMI, SVG or PDF also works.",
    ),
    "astah": _f(
        id="astah", label="Astah model", family="model", capability="refuse",
        mime="application/octet-stream", extensions=(".asta", ".jude", ".juth"),
        remedy="Astah's file format is closed and its converter is a commercial Java "
               "CLI. From Astah use File > Export > Image (PNG/SVG) or File > Export > "
               "XMI, and upload that.",
    ),
    "zip-unknown": _f(
        id="zip-unknown", label="ZIP archive (unrecognized contents)", family="container",
        capability="refuse", mime="application/zip", extensions=(".zip",),
        remedy="This is a ZIP whose contents match no diagram format known here. "
               "Unzip it and upload the individual drawing — .drawio, .svg, .vsdx, "
               ".pdf or an image all work.",
    ),
    "prose": _f(
        id="prose", label="Text/Markdown/JSON/YAML description", family="text",
        capability="structure", mime="text/plain",
        extensions=(".md", ".markdown", ".txt", ".json", ".yaml", ".yml"),
        adapter="prose",
        note="Read verbatim as prose. No graph is extracted here — the analyst stage "
             "reads the text itself, which is exact and costs no vision call.",
    ),
}

#: The catch-all. Not in FORMATS: it is what detection returns when nothing matched.
UNKNOWN = FormatSpec(
    id="unknown",
    label="Unrecognized file",
    family="unknown",
    capability="refuse",
    mime="application/octet-stream",
    extensions=(),
    remedy=(
        "Nothing in this file's first bytes matches a diagram format. Export the "
        "drawing as one of: .png .jpg .webp .heic (photo or screenshot), .pdf, "
        ".svg, .drawio, .vsdx, .bpmn, .archimate, .puml or .mmd. If you believe the "
        "file is already one of those, it may be truncated — re-export it and check "
        "the size is not zero."
    ),
)


def _build_extension_index() -> dict[str, FormatSpec]:
    index: dict[str, FormatSpec] = {}
    for spec in FORMATS.values():
        for ext in spec.extensions:
            index.setdefault(ext, spec)
    return index


#: Extension -> first format that claims it. A tie-breaker only, never a verdict.
_BY_EXT: dict[str, FormatSpec] = _build_extension_index()

#: Extension -> every format that claims it, so "does the suffix contradict the
#: bytes?" can be answered without false positives on ``.xml``.
_ALL_BY_EXT: dict[str, tuple[str, ...]] = {}
for _spec in FORMATS.values():
    for _ext in _spec.extensions:
        _ALL_BY_EXT[_ext] = _ALL_BY_EXT.get(_ext, ()) + (_spec.id,)


def by_extension(ext: str) -> FormatSpec | None:
    """The format a suffix suggests, or ``None``. Never authoritative on its own."""
    return _BY_EXT.get((ext or "").lower())


def extension_claims(ext: str) -> tuple[str, ...]:
    """Every format id that legitimately uses this suffix."""
    return _ALL_BY_EXT.get((ext or "").lower(), ())


def spec_for(format_id: str) -> FormatSpec:
    """Lookup by id, falling back to :data:`UNKNOWN` rather than raising."""
    return FORMATS.get(format_id, UNKNOWN)


def accepted_extensions() -> tuple[str, ...]:
    """Every suffix that leads to a working adapter, sorted — for the API and the
    file picker's ``accept`` attribute.

    Refusals are excluded on purpose: offering ``.pptx`` in the picker and then
    rejecting it is worse than not offering it, and the rejection message already
    explains the conversion when someone drags one in anyway.
    """
    out: set[str] = set()
    for spec in FORMATS.values():
        if spec.capability != "refuse":
            out.update(spec.extensions)
    return tuple(sorted(out))
