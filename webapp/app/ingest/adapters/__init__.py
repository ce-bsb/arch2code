"""Adapter registry: format id -> the object that reads it.

Lookups go through :func:`get`, which is the single place where "this format has
no adapter" turns into a refusal carrying the format's own remedy. A ``KeyError``
here would surface as a 500 with a traceback, which is exactly the blank-panel
failure the project forbids.

Instances are cheap and stateless, so they are built once at import. The heavy
libraries (Pillow, pypdfium2, vsdx, pillow-heif) are imported *inside* the
methods that need them, so importing this package costs nothing and a missing
optional library fails at the moment it is used, with a message naming the pip
command — not at startup, taking the whole app down with it.
"""

from __future__ import annotations

from ..errors import UnsupportedFormat
from ..formats import FormatSpec
from .base import Adapter
from .drawio import DrawioAdapter, DrawioPngAdapter
from .image import ImageAdapter
from .modelfiles import SparxQeaAdapter, StarUmlAdapter
from .pdf import PdfAdapter
from .svg import SvgAdapter
from .textual import MermaidAdapter, PlantUmlAdapter, ProseAdapter
from .vsdx import VsdxAdapter
from .xmlgraph import ArchimateAdapter, BpmnAdapter

__all__ = ["ADAPTERS", "get", "Adapter"]

ADAPTERS: dict[str, Adapter] = {
    adapter.id: adapter
    for adapter in (
        ImageAdapter(),
        PdfAdapter(),
        DrawioAdapter(),
        DrawioPngAdapter(),
        SvgAdapter(),
        PlantUmlAdapter(),
        MermaidAdapter(),
        ProseAdapter(),
        VsdxAdapter(),
        BpmnAdapter(),
        ArchimateAdapter(),
        StarUmlAdapter(),
        SparxQeaAdapter(),
    )
}


def get(spec: FormatSpec) -> Adapter:
    """The adapter for a format, or a refusal that explains the way out."""
    if spec.capability == "refuse" or spec.adapter is None:
        raise UnsupportedFormat(
            f"ingest_unsupported_{spec.id.replace('-', '_')}",
            f"{spec.label} cannot be read here",
            f"No adapter in this build reads {spec.label} ({spec.mime}).",
            remedy=spec.remedy
            or (
                "Export the drawing as .png, .pdf, .svg, .drawio or .vsdx and upload "
                "that instead."
            ),
        )
    adapter = ADAPTERS.get(spec.adapter)
    if adapter is None:  # pragma: no cover - a registry typo, caught by the tests
        raise UnsupportedFormat(
            "ingest_adapter_missing",
            f"{spec.label} is declared but not wired up",
            f"formats.py points {spec.id} at adapter '{spec.adapter}', which is not "
            "in the registry.",
            remedy=(
                "This is a defect in webapp/app/ingest/adapters/__init__.py. Report it "
                "with the format id above; in the meantime upload a .png, .pdf or "
                ".svg export of the same drawing."
            ),
            status=500,
        )
    return adapter
