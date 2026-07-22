"""Universal diagram ingestion: detect by content, prefer structure, refuse loudly.

Three rules govern everything in this package.

**1. Content decides the type, never the extension.** ``mimetypes.guess_type`` —
what this repo used before — is pure string matching on the suffix, so a PDF
renamed ``sketch.png`` was accepted as an image and handed to a decoder that
could not read it. :func:`~app.ingest.detect.detect` runs a three-level cascade
(binary signature, ZIP container manifest, text sniff) and refuses a file whose
bytes contradict its name.

**2. A structured source always beats vision.** Vision costs tokens and imports
hallucination; a ``.drawio``, a ``.vsdx`` or a ``.bpmn`` states its nodes and edges
with ids that can be cited. Adapters declare which they produce, and
``IngestSummary.vision_required`` is computed from that declaration rather than
from a habit.

**3. Every format we cannot read gets a way out.** ``formats.py`` carries a
``remedy`` on every refusal — the two clicks that turn a ``.vsd`` into a ``.vsdx``,
a ``.pptx`` slide into a PDF, an Astah model into an image. "Unsupported file
type" with no next step is not an outcome this package produces.

Public API::

    from app.ingest import detect, inspect_file, normalize, IngestError

    report = inspect_file("napkin.heic")     # what is it, how many pages, cost?
    result = normalize("deck.pdf", out, pages=[3])   # structured.json + vision/*.png

Everything else is an implementation detail. Note that this subpackage imports
nothing from ``app`` — that is what lets ``app/models.py`` embed
:class:`~app.ingest.models.IngestSummary` without an import cycle, and it also
means the package is usable from a script or a test with no FastAPI in sight.
"""

from __future__ import annotations

from .detect import Detection, detect, detect_path, sniff
from .errors import CorruptSource, IngestError, MissingDependency, UnsupportedFormat
from .formats import FORMATS, FormatSpec, accepted_extensions, by_extension, spec_for
from .models import (
    BBox,
    Capability,
    GraphEdge,
    GraphNode,
    IngestReport,
    IngestSummary,
    NormalizedPage,
    NormalizeResult,
    PageRef,
    StructuredGraph,
)
from .normalize import inspect_file, normalize, summary_for
from .raster import MAX_EDGE

__all__ = [
    # detection
    "Detection", "detect", "detect_path", "sniff",
    # errors
    "IngestError", "UnsupportedFormat", "CorruptSource", "MissingDependency",
    # registry
    "FORMATS", "FormatSpec", "spec_for", "by_extension", "accepted_extensions",
    # models
    "BBox", "Capability", "GraphNode", "GraphEdge", "StructuredGraph", "PageRef",
    "IngestSummary", "IngestReport", "NormalizedPage", "NormalizeResult",
    # pipeline
    "inspect_file", "normalize", "summary_for", "MAX_EDGE",
]
