"""Artifact routing: decides whether an upload goes down the deterministic path,
the vision path, both, or neither.

Two routers live here, and the difference between them is the point of this file.

:func:`route_content` is the real one. It reads the file's **bytes** through
``app.ingest`` and routes on what the file actually is. Use it whenever the bytes
are available, which — since ``UploadStore.save`` writes them before deciding —
is every path that matters.

:func:`route_artifact` is the legacy extension-only table. It is a MIRROR of
``route()`` in ``.bob/skills/diagram-intake/scripts/capture_diagram.py`` and of the
guard inside ``mcp/arch_vision/server.py::_encode_image``, and it is kept for two
reasons: the helper script still routes that way, and a filename with no bytes
behind it (an early 415 before a 25 MB body is read) has nothing else to go on.
**It is not authoritative.** A suffix is attacker-controlled text; before
``app.ingest`` existed, a PDF renamed ``sketch.png`` was routed to the vision path
and failed several frames later inside Pillow, and ``mimetypes.guess_type`` — pure
string matching on the suffix — would have said ``image/png`` right along with it.

Two known, deliberate asymmetries between the extension tables:

* ``capture_diagram.py`` accepts ``.heic/.heif/.bmp/.tif/.tiff`` as vision input,
  while the MCP server only accepts ``image/png|jpeg|webp|gif`` as *model* input.
  That is not a contradiction: capture normalizes every vision artifact to PNG
  before the model ever sees it. :func:`needs_normalization` names the extensions
  for which skipping capture is fatal rather than merely wasteful.
* ``.gif`` is accepted by the MCP server but is not in capture's vision set, so a
  ``.gif`` routes to ``unknown`` in the legacy table, exactly as capture would
  route it. Content routing accepts it and says so.
"""

from __future__ import annotations

from pathlib import Path

from .errors import AppError
from .ingest import (
    Detection,
    IngestError,
    IngestReport,
    accepted_extensions,
    by_extension,
    detect,
    detect_path,
    inspect_file,
)
from .ingest.errors import UnsupportedFormat
from .models import Routing

__all__ = [
    "VISION_EXT", "DETERMINISTIC_EXT", "PDF_EXT", "MODEL_READY_EXT", "KIND_BY_EXT",
    "MCP_SOURCE_KINDS", "route_artifact", "route_content", "route_detection",
    "sibling_structured", "is_vision_capable", "needs_normalization",
    "accepted_upload_extensions", "reject_by_extension", "as_app_error",
]

# --------------------------------------------------------------------------- #
# Legacy extension tables — kept identical to capture_diagram.py's module-level
# sets. Not authoritative; see the module docstring.
# --------------------------------------------------------------------------- #
VISION_EXT: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
)
DETERMINISTIC_EXT: frozenset[str] = frozenset(
    {
        ".drawio",
        ".xml",
        ".puml",
        ".plantuml",
        ".mmd",
        ".mermaid",
        ".md",
        ".json",
        ".yaml",
        ".yml",
    }
)
PDF_EXT: frozenset[str] = frozenset({".pdf"})

#: Extensions the watsonx chat endpoint accepts directly, via
#: ``mcp/arch_vision/server.py::ALLOWED_IMAGE_TYPES``.
MODEL_READY_EXT: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

KIND_BY_EXT: dict[str, str] = {
    **{ext: "screenshot" for ext in VISION_EXT},
    ".drawio": "drawio",
    ".xml": "drawio",
    ".puml": "plantuml",
    ".plantuml": "plantuml",
    ".mmd": "mermaid",
    ".mermaid": "mermaid",
    ".md": "prose",
    ".json": "prose",
    ".yaml": "prose",
    ".yml": "prose",
    ".pdf": "pdf",
}

#: The four ``source_kind`` values the MCP extraction tool accepts. A routing
#: ``source_kind`` of ``drawio``/``prose``/... is NOT one of these — it describes
#: the artifact, not the vision prompt variant.
MCP_SOURCE_KINDS: frozenset[str] = frozenset({"napkin", "whiteboard", "screenshot", "pdf"})

#: Detected format id -> the ``source_kind`` label the rest of the app shows. This
#: is descriptive metadata for the UI and the analyst, not the MCP prompt variant.
_SOURCE_KIND_BY_FORMAT: dict[str, str] = {
    "png": "screenshot", "jpeg": "screenshot", "webp": "screenshot",
    "gif": "screenshot", "bmp": "screenshot", "tiff": "screenshot",
    "heic": "screenshot",
    "pdf": "pdf",
    "drawio": "drawio", "drawio-png": "drawio",
    "plantuml": "plantuml", "mermaid": "mermaid",
    "svg": "svg", "vsdx": "visio", "bpmn": "bpmn", "archimate": "archimate",
    "staruml": "uml", "sparx-qea": "uml",
    "prose": "prose",
}

#: Detected format id -> the tool that should read it. ``recommended_tool`` is
#: rendered verbatim in the UI, so it names something a human can actually run.
_TOOL_BY_FORMAT: dict[str, str] = {
    "drawio": "parse_drawio.py (or app.ingest, which reads every tab and its geometry)",
    "drawio-png": "app.ingest — the draw.io XML is embedded in this PNG, no vision needed",
    "svg": "app.ingest (SVG <text> gives exact labels; the edges still need vision)",
    "vsdx": "app.ingest (Visio shapes and glued connectors)",
    "bpmn": "app.ingest (sequenceFlow sourceRef/targetRef IS the edge)",
    "archimate": "app.ingest (Open Group Exchange and Archi native)",
    "staruml": "app.ingest (JSON walk over ownedElements)",
    "sparx-qea": "app.ingest (t_object + t_connector via sqlite3)",
    "plantuml": "read_file — the arrows are already text",
    "mermaid": "read_file — the arrows are already text",
    "prose": "read_file",
    "pdf": "app.ingest (text and geometry first; vision only for the arrows)",
}

_VISION_TOOL = "arch_vision_extract_architecture (via use_mcp_tool)"


# --------------------------------------------------------------------------- #
# Content routing — the authoritative path
# --------------------------------------------------------------------------- #
def route_detection(detection: Detection) -> Routing:
    """Turn a content detection into the ``Routing`` the rest of the app speaks.

    The mapping is the project's golden rule expressed as a table: a format that
    yields nodes and edges is ``deterministic`` and never spends a token; a format
    with only pixels is ``vision``; a format with both, or with labels but no
    relationships, is ``hybrid``; a format we refuse is ``unknown``, which is a
    question for the human rather than a silent fallback. Sending an unrecognized
    file through the vision model is how you pay for an inference that returns an
    apology.
    """
    spec = detection.spec
    source_kind = _SOURCE_KIND_BY_FORMAT.get(spec.id, spec.family)
    if spec.capability == "refuse":
        return Routing(
            extraction_path="unknown",
            source_kind=source_kind or "prose",
            recommended_tool="ask the human",
        )
    if spec.capability == "raster":
        return Routing(
            extraction_path="vision",
            source_kind=source_kind or "screenshot",
            recommended_tool=_VISION_TOOL,
        )
    if spec.capability == "hybrid" or spec.partial_structure:
        return Routing(
            extraction_path="hybrid",
            source_kind=source_kind or "pdf",
            recommended_tool=_TOOL_BY_FORMAT.get(
                spec.id, "app.ingest, then vision for the arrows"
            ),
        )
    return Routing(
        extraction_path="deterministic",
        source_kind=source_kind or "prose",
        recommended_tool=_TOOL_BY_FORMAT.get(spec.id, "app.ingest"),
    )


def route_content(
    path: Path, *, data: bytes | None = None, filename: str | None = None
) -> tuple[Routing, IngestReport]:
    """Route by content and describe the file in one pass.

    :param path: the stored file. The adapters always read from disk.
    :param data: the bytes, when the caller already has them — saves one read on
        the detection pass and nothing else.
    :param filename: the *user's* filename, when ``path`` was renamed on the way
        in. Detection uses it only as a tie-breaker and to report a mismatch.
    :raises IngestError: refused format, suffix/content contradiction, corrupt
        file, or a decoder this build does not have. Every one carries a remedy.
    """
    name = filename or path.name
    detection = detect(data, name) if data is not None else detect_path(path)
    report = inspect_file(path, detection=detection)
    return route_detection(detection), report


def reject_by_extension(filename: str) -> None:
    """Cheap pre-flight before a body is read: refuse only what the suffix *proves*.

    This runs before the upload has been streamed, so there are no bytes to sniff.
    It therefore refuses one narrow class — a suffix that maps to a format already
    declared unreadable, such as ``.pptx`` — because streaming 25 MB of a file we
    will certainly reject wastes the user's time and the pod's memory. Everything
    else is allowed through to content detection, which is the only thing entitled
    to a verdict.

    :raises IngestError: carrying the format's own conversion instructions.
    """
    safe = Path(filename or "").name
    spec = by_extension(Path(safe).suffix.lower())
    if spec is not None and spec.capability == "refuse":
        raise UnsupportedFormat(
            f"upload_unsupported_{spec.id.replace('-', '_')}",
            f"{spec.label} files are not read here",
            f"'{safe}' is a {spec.label}, which this build does not open.",
            remedy=spec.remedy or "Export the drawing as .png, .pdf or .svg instead.",
            filename=safe,
        )


def accepted_upload_extensions() -> tuple[str, ...]:
    """Every suffix that leads to a working adapter — for the API and the picker."""
    return accepted_extensions()


def as_app_error(exc: IngestError) -> AppError:
    """Lift an ``IngestError`` into the app's error envelope, losing nothing.

    ``app.ingest`` deliberately imports nothing from ``app``, so it raises its own
    exception type; this is the one-line bridge. Code, title, detail, remedy,
    status and context all survive, which is what keeps the front end's single
    error path working for ingest failures too.
    """
    return AppError(
        exc.code,
        exc.title,
        exc.detail,
        remedy=exc.remedy,
        status=exc.status,
        **exc.context,
    )


# --------------------------------------------------------------------------- #
# Legacy extension routing
# --------------------------------------------------------------------------- #
def route_artifact(path: Path) -> Routing:
    """Extension-only routing. Mirrors ``capture_diagram.route()`` exactly.

    Prefer :func:`route_content` wherever the bytes exist. This remains for the
    helper script's table and for pre-flight decisions made from a filename alone.
    """
    ext = path.suffix.lower()
    if ext in DETERMINISTIC_EXT:
        return Routing(
            extraction_path="deterministic",
            source_kind=KIND_BY_EXT.get(ext, "prose"),
            recommended_tool=(
                "parse_drawio.py" if ext in {".drawio", ".xml"} else "read_file"
            ),
        )
    if ext in PDF_EXT:
        return Routing(
            extraction_path="hybrid",
            source_kind="pdf",
            recommended_tool="read_file (try text first; vision only if it is a pure image)",
        )
    if ext in VISION_EXT:
        return Routing(
            extraction_path="vision",
            source_kind="screenshot",
            recommended_tool=_VISION_TOOL,
        )
    return Routing(
        extraction_path="unknown",
        source_kind="prose",
        recommended_tool="ask the human",
    )


def sibling_structured(path: Path) -> list[Path]:
    """Return structured sources for the same drawing sitting next to it.

    If one exists, running vision is a waste: the structured file is exact, free
    and cannot hallucinate. The UI must surface this as a warning on the upload,
    not bury it.
    """
    if not path.parent.is_dir():
        return []
    try:
        candidates = sorted(path.parent.glob(f"{path.stem}.*"))
    except OSError:
        return []
    return [
        p
        for p in candidates
        if p != path and p.suffix.lower() in DETERMINISTIC_EXT and p.is_file()
    ]


def is_vision_capable(path: Path) -> bool:
    """True when the vision path is legal for this artifact."""
    return path.suffix.lower() in VISION_EXT


def needs_normalization(path: Path) -> bool:
    """True when the model cannot read these bytes without normalization first.

    A ``.heic`` from an iPhone or a multi-page ``.tiff`` is a vision artifact that
    the watsonx endpoint will reject with an unhelpful type error. Capture converts
    it to PNG; this flag exists so the UI can explain *why* capture is mandatory
    rather than optional for those files.
    """
    ext = path.suffix.lower()
    return ext in VISION_EXT and ext not in MODEL_READY_EXT
