"""PDF — the hybrid case, and the one that can bankrupt a run.

Why pypdfium2 and not PyMuPDF
-----------------------------
PyMuPDF is AGPL-or-commercial (Artifex). AGPL is viral over a network service,
and arch2code is precisely a web application served over a network. Publishing
the code makes AGPL fine; shipping this as a closed hosted product does not, and
that decision is not this module's to make. ``pypdfium2`` is Apache-2.0/BSD-3,
already present on the target interpreter, and does everything needed here:
per-page size, text with rectangles, object enumeration and rendering at an
arbitrary scale. What it does not have is PyMuPDF's ``get_drawings()`` — vector
path extraction — so this adapter reads **text and geometry**, not shapes.

What "hybrid" honestly means here
---------------------------------
A vector PDF yields exact label text with exact positions, and no edges: a line
in a PDF is a stroked path, not a relationship. So the structural half is real
but *partial*, and it is reported that way — ``structure_available`` is true,
``structure_edges`` is zero, and ``vision_required`` stays true. The value is not
that it replaces vision; it is that every component name is exact and
independently checkable, which is the half of a vision answer most likely to be
subtly wrong.

The cost trap
-------------
A 30-page PDF is 30 images and 30 vision calls, and one trivial Bob stage in this
project already measured 37,154 tokens. So: this adapter never picks pages by
itself. It reports what each page contains, flags the ones that look like
diagrams, and :class:`~app.ingest.models.IngestSummary` carries
``requires_page_selection`` up to the API so a human answers the question.
:data:`MAX_PAGES_PER_CALL` is the hard stop behind that.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import CorruptSource, MissingDependency, UnsupportedFormat
from ..models import BBox, GraphNode, NormalizedPage, PageRef, StructuredGraph
from ..raster import MAX_EDGE, save_normalized
from .base import Adapter

__all__ = ["PdfAdapter", "MAX_PAGES_PER_CALL", "RENDER_DPI"]

EXTRACTOR = "ingest.adapters.pdf@1.0"

#: Rasterize at this DPI, then resample down to ``MAX_EDGE`` with LANCZOS.
#: Never 72 (pdfium's default), which is unreadable for 8pt labels; 200 keeps small
#: text legible after the downscale, and supersampling + LANCZOS beats rendering
#: straight at the target size.
RENDER_DPI = 200.0

#: The most pages one call will rasterize. Not a performance guard — a cost guard.
MAX_PAGES_PER_CALL = 20

#: A page with fewer characters than this is treated as a scan rather than a
#: vector export. Chosen low on purpose: a page number and a footer alone clear 20.
_TEXT_FLOOR = 20


def _open(path: Path):
    try:
        import pypdfium2  # Apache-2.0 / BSD-3, no system library
    except ImportError as exc:
        raise MissingDependency(
            "ingest_pdf_reader_missing",
            "PDFs cannot be read on this install",
            "pypdfium2 is not installed.",
            remedy=(
                "`<ARCH2CODE_PYTHON> -m pip install pypdfium2` (it ships self-contained "
                "wheels; poppler-utils is deliberately NOT required). Until then, export "
                "the diagram page as PNG from your PDF viewer and upload that."
            ),
        ) from exc
    try:
        return pypdfium2.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001 - pdfium raises its own family
        raise CorruptSource(
            "ingest_pdf_unreadable",
            "That PDF could not be opened",
            f"{path.name}: {type(exc).__name__}: {exc}",
            remedy=(
                "The file is truncated or password-protected. If it is encrypted, open "
                "it in a viewer, print it to a new PDF without a password and upload "
                "that. If it is truncated, download it again."
            ),
        ) from exc


class PdfAdapter(Adapter):
    id = "pdf"
    label = "PDF"
    produces_structure = True
    produces_raster = True

    # -- discovery -----------------------------------------------------------

    def pages(self, path: Path) -> list[PageRef]:
        doc = _open(path)
        refs: list[PageRef] = []
        try:
            for index in range(len(doc)):
                page = doc[index]
                width, height = page.get_size()
                text = ""
                try:
                    textpage = page.get_textpage()
                    text = textpage.get_text_bounded() or ""
                    textpage.close()
                except Exception:  # noqa: BLE001 - a page with no text object
                    text = ""
                paths, images = self._count_objects(page)
                chars = len(text.strip())
                if chars >= _TEXT_FLOOR and paths:
                    content = "vector"
                elif chars >= _TEXT_FLOOR:
                    content = "mixed" if images else "vector"
                elif images:
                    content = "raster"
                else:
                    content = "unknown"
                refs.append(
                    PageRef(
                        index=index + 1,
                        name=f"page {index + 1}",
                        width=round(width, 2),
                        height=round(height, 2),
                        content=content,
                        text_chars=chars,
                        # A page with both text and drawn paths is what an exported
                        # architecture diagram looks like. A pure-text page is prose;
                        # a pure-image page is a scan and cannot be told apart from a
                        # photo of a cat without spending a token, so it also counts.
                        likely_diagram=bool(paths >= 4 or (images and chars < _TEXT_FLOOR)),
                    )
                )
                page.close()
        finally:
            doc.close()
        if refs and not any(r.likely_diagram for r in refs):
            refs[0].likely_diagram = True
        return refs

    @staticmethod
    def _count_objects(page) -> tuple[int, int]:
        """(vector path objects, image objects) on one page."""
        paths = images = 0
        try:
            for obj in page.get_objects():
                kind = getattr(obj, "type", None)
                # pdfium object types: 1=text 2=path 3=image 4=shading 5=form
                if kind == 2:
                    paths += 1
                elif kind == 3:
                    images += 1
        except Exception:  # noqa: BLE001 - object enumeration is best-effort
            return 0, 0
        return paths, images

    # -- structure -----------------------------------------------------------

    def extract(self, path: Path, pages: list[int] | None = None) -> StructuredGraph | None:
        """Exact label text with exact geometry. No edges — see the module docstring."""
        doc = _open(path)
        graph = StructuredGraph(format="pdf", extractor=EXTRACTOR, pages=len(doc))
        try:
            wanted = sorted(set(pages)) if pages else list(range(1, len(doc) + 1))
            graph.page_names = [f"page {i}" for i in range(1, len(doc) + 1)]
            for page_no in wanted:
                if page_no < 1 or page_no > len(doc):
                    continue
                page = doc[page_no - 1]
                width, height = page.get_size()
                try:
                    textpage = page.get_textpage()
                except Exception:  # noqa: BLE001
                    page.close()
                    continue
                try:
                    count = textpage.count_rects()
                except Exception:  # noqa: BLE001
                    count = 0
                for rect_index in range(count):
                    try:
                        left, bottom, right, top = textpage.get_rect(rect_index)
                        span = (textpage.get_text_bounded(left, bottom, right, top) or "").strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not span:
                        continue
                    graph.nodes.append(
                        GraphNode(
                            id=f"p{page_no}-t{rect_index}",
                            label=" ".join(span.split()),
                            kind_hint="text_span",
                            page=page_no,
                            # PDF's origin is bottom-left; the app's is top-left.
                            bbox=BBox(
                                x=round(left / width, 5) if width else 0.0,
                                y=round((height - top) / height, 5) if height else 0.0,
                                w=round((right - left) / width, 5) if width else 0.0,
                                h=round((top - bottom) / height, 5) if height else 0.0,
                            ),
                            evidence=f"page {page_no}, text rect {rect_index}",
                        )
                    )
                textpage.close()
                page.close()
        finally:
            doc.close()

        if not graph.nodes:
            return None
        graph.warnings.append(
            "PDF text extraction gives exact labels and exact positions, but no "
            "connections: a line in a PDF is a stroked path, not a relationship. Use "
            "these labels to check the vision result's component names — they are the "
            "part a model most often gets subtly wrong."
        )
        return graph

    # -- pixels --------------------------------------------------------------

    def rasterize(
        self, path: Path, out_dir: Path, pages: list[int] | None = None,
        *, max_edge: int = MAX_EDGE,
    ) -> list[NormalizedPage]:
        doc = _open(path)
        try:
            total = len(doc)
            wanted = sorted(set(pages)) if pages else list(range(1, total + 1))
            wanted = [p for p in wanted if 1 <= p <= total]
            if not wanted:
                raise UnsupportedFormat(
                    "ingest_pdf_no_such_page",
                    "None of the requested pages exist",
                    f"{path.name} has {total} page(s); {pages!r} was requested.",
                    remedy=f"Pick a page between 1 and {total}.",
                    status=422,
                )
            if len(wanted) > MAX_PAGES_PER_CALL:
                raise UnsupportedFormat(
                    "ingest_pdf_too_many_pages",
                    "That is more PDF pages than one run may rasterize",
                    f"{len(wanted)} pages were requested; the ceiling is "
                    f"{MAX_PAGES_PER_CALL}. Each page becomes one vision call, and a "
                    "single trivial stage in this pipeline already costs tens of "
                    "thousands of tokens.",
                    remedy=(
                        "Select the pages that actually hold the architecture — the "
                        "upload response lists every page with its character count and "
                        "flags the ones that look like diagrams — or split the PDF and "
                        "run the parts separately."
                    ),
                    status=422,
                    page_count=total,
                    requested=len(wanted),
                )
            out_dir.mkdir(parents=True, exist_ok=True)
            results: list[NormalizedPage] = []
            for page_no in wanted:
                page = doc[page_no - 1]
                scale = self._render_scale(page.get_size(), max_edge)
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                dest = out_dir / f"{page_no:03d}.png"
                meta = save_normalized(image, dest, max_edge=max_edge)
                image.close()
                page.close()
                results.append(
                    NormalizedPage(
                        index=page_no,
                        path=str(dest),
                        width=meta["normalized"]["width"],
                        height=meta["normalized"]["height"],
                        scale=meta["scale"],
                        origin="render",
                        render_dpi=round(scale * 72.0, 1),
                    )
                )
            return results
        finally:
            doc.close()

    @staticmethod
    def _render_scale(size: tuple[float, float], max_edge: int) -> float:
        """Render big enough to supersample, not so big that a poster eats the heap.

        Target: ``RENDER_DPI``, clamped so the rendered bitmap's longest edge lands
        between ``max_edge`` (never render below the final size — that is upscaling
        blur) and ``2 * max_edge`` (beyond 2x, LANCZOS gains nothing and the bitmap
        for an A0 poster at 200 DPI is 600 MB).
        """
        longest_pt = max(size) or 612.0
        scale = RENDER_DPI / 72.0
        rendered = longest_pt * scale
        if rendered > 2 * max_edge:
            scale = (2 * max_edge) / longest_pt
        elif rendered < max_edge:
            scale = min(max_edge / longest_pt, 8.0)
        return max(scale, 0.1)
