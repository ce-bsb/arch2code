"""The adapter contract.

One adapter per format family. Each declares, in code, the same thing the
registry declares in data: whether it yields **structure**, **pixels**, or both.
The declaration is not decoration — :func:`app.ingest.normalize.normalize` reads
it to decide whether a vision call is even allowed, and the project's golden rule
lives in that decision: *a structured source always beats vision, because vision
costs tokens and imports hallucination.*

Defaults are deliberately hostile. An adapter that forgets to implement
:meth:`rasterize` does not silently return an empty page list — it raises a
refusal that names the format and tells the user how to produce pixels
themselves. Silence is the one behaviour this package does not offer.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import UnsupportedFormat
from ..models import NormalizedPage, PageRef, StructuredGraph
from ..raster import MAX_EDGE

__all__ = ["Adapter"]


class Adapter:
    """Base class. Subclasses override what they can actually do."""

    #: Registry key, matching ``FormatSpec.adapter``.
    id: str = "base"
    #: Human name used in messages.
    label: str = "file"
    produces_structure: bool = False
    produces_raster: bool = False

    # -- discovery -----------------------------------------------------------

    def pages(self, path: Path) -> list[PageRef]:
        """Enumerate pages/tabs/sheets. Single-page formats keep the default."""
        return [PageRef(index=1, name="", likely_diagram=True)]

    # -- the two real capabilities ------------------------------------------

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph | None:
        """Return nodes and edges, or ``None`` when this format carries none."""
        return None

    def rasterize(
        self,
        path: Path,
        out_dir: Path,
        pages: list[int] | None = None,
        *,
        max_edge: int = MAX_EDGE,
    ) -> list[NormalizedPage]:
        """Write normalized PNGs into ``out_dir`` and return their references."""
        raise UnsupportedFormat(
            "ingest_no_raster_path",
            f"A {self.label} cannot be turned into an image here",
            f"The {self.id} adapter reads structure only; this build has no renderer "
            f"for {self.label}.",
            remedy=(
                "Open the file in the tool that made it and export it as PNG or PDF, "
                "then upload that alongside — or instead of — this file. The structural "
                "read is still the one used for the graph; the image is only there for "
                "a human to check against."
            ),
        )
