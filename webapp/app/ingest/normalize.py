"""The pre-stage: everything a file has to survive before the pipeline sees it.

This is pure Python and runs **before** the first ``bob`` subprocess. It is
deliberately not a new Bob chat mode: pushing format decoding into a mode means
paying for inference to do deterministic parsing, which is the exact error
``parse_drawio.py`` exists to prevent.

    upload -> detect (3 levels) -> +- structural -> structured.json
                                   |                {nodes, edges, evidence}
                                   +- hybrid ------> structured.json (partial)
                                   |                 + vision/NNN.png
                                   +- raster ------> vision/NNN.png
                                   +- refuse ------> error with a remedy

Two functions, and the split matters:

* :func:`inspect_file` is cheap and read-only. It answers "what is this, how many
  pages, is structure available, will this cost tokens?" and it is what the upload
  endpoint calls. It never rasterizes and never spends more than one parse.
* :func:`normalize` does the work and writes files. It is called once the page
  question has been answered — by a human, not by a default.

The golden rule is enforced in one place, :func:`_vision_required`: if a format
yields a complete graph, ``vision_required`` is false and the pipeline has no
business spending a token on it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import adapters
from .detect import Detection, detect_path
from .errors import IngestError, UnsupportedFormat
from .formats import FormatSpec
from .models import (
    IngestReport,
    IngestSummary,
    NormalizedPage,
    NormalizeResult,
    PageRef,
    StructuredGraph,
)
from .raster import MAX_EDGE

__all__ = ["inspect_file", "normalize", "summary_for"]


def _vision_required(spec: FormatSpec, graph: StructuredGraph | None) -> bool:
    """Would this file still need the vision model after the structural read?

    ``raster``            yes. There is nothing else in the file.
    ``hybrid``/partial    only if the structural half came back without edges. A
                          draw.io PNG carries a complete graph and costs nothing;
                          a vector PDF or a plain SVG carries exact labels and no
                          relationships, so the arrows still have to be read from
                          the picture.
    everything else       no. A ``.drawio`` read through the vision model is a
                          forbidden move in this harness.
    """
    if spec.capability == "raster":
        return True
    if spec.capability == "hybrid" or spec.partial_structure:
        return not (graph and graph.edges)
    return False


def summary_for(
    detection: Detection,
    pages: list[PageRef],
    graph: StructuredGraph | None,
    *,
    extra_notes: list[str] | None = None,
) -> IngestSummary:
    spec = detection.spec
    suggested = [p.index for p in pages if p.likely_diagram] or ([1] if pages else [])
    notes = list(detection.notes)
    if spec.note:
        notes.append(spec.note)
    notes.extend(extra_notes or [])
    if graph is not None:
        notes.extend(graph.warnings)

    page_count = max(1, len(pages))
    vision_required = _vision_required(spec, graph)
    # Asking "which page?" is only worth a click when the answer costs something.
    # A 40-page PDF on the vision path is 40 inference calls, so the human decides.
    # A .drawio with three tabs was fully parsed for free before this line ran, so
    # blocking on a question there would be ceremony, not care.
    requires_page_selection = page_count > 1 and vision_required
    return IngestSummary(
        format_id=spec.id,
        format_label=spec.label,
        family=spec.family,
        capability=spec.capability,
        mime=spec.mime,
        detected_by=detection.level,
        extension=detection.extension,
        extension_agrees=detection.extension_agrees,
        page_count=page_count,
        requires_page_selection=requires_page_selection,
        suggested_pages=suggested,
        structure_available=bool(graph and not graph.is_empty),
        structure_nodes=len(graph.nodes) if graph else 0,
        structure_edges=len(graph.edges) if graph else 0,
        vision_required=vision_required,
        notes=notes,
        remedy=spec.remedy,
    )


def inspect_file(path: Path | str, *, detection: Detection | None = None) -> IngestReport:
    """Identify a file and describe it, without writing anything.

    :raises IngestError: for a refused format, a suffix/content contradiction, a
        corrupt file or a missing decoder — each with a specific remedy.
    """
    file_path = Path(path)
    detection = detection or detect_path(file_path)
    adapter = adapters.get(detection.spec)

    pages = adapter.pages(file_path)
    graph: StructuredGraph | None = None
    notes: list[str] = []

    # Extracting structure is cheap for a single page and for the formats whose
    # whole content IS the graph. For a 40-page PDF it is not, and doing it before
    # anyone has said which page matters is work thrown away.
    if adapter.produces_structure and (
        len(pages) <= 1 or detection.spec.capability == "structure"
    ):
        graph = adapter.extract(file_path)
    elif adapter.produces_structure:
        readable = [p.index for p in pages if p.text_chars > 0]
        if readable:
            notes.append(
                f"{len(readable)} of {len(pages)} pages carry machine-readable text, so "
                "exact labels can be extracted from them once a page is chosen. Pages "
                f"with text: {', '.join(str(i) for i in readable[:12])}"
                + (" ..." if len(readable) > 12 else "")
            )
        notes.append(
            "Structure was not extracted yet: this file has more than one page and "
            "parsing every page before anyone has said which one holds the "
            "architecture is work — and, on the vision path, money — thrown away."
        )

    summary = summary_for(detection, pages, graph, extra_notes=notes)
    if summary.requires_page_selection:
        summary.notes.append(
            f"This file has {summary.page_count} pages. Choose which one(s) to read: "
            "each page sent to the vision model is a separate inference call. "
            f"Suggested: {', '.join(str(i) for i in summary.suggested_pages)}."
        )
    elif summary.page_count > 1:
        summary.notes.append(
            f"All {summary.page_count} pages/tabs were read: this format is parsed "
            "exactly and costs nothing per page, so there is nothing to choose."
        )
    return IngestReport(summary=summary, pages=pages, graph=graph)


def normalize(
    path: Path | str,
    out_dir: Path | str,
    *,
    pages: list[int] | None = None,
    max_edge: int = MAX_EDGE,
    detection: Detection | None = None,
) -> NormalizeResult:
    """Produce the invariant output pair for one file.

    Writes, under ``out_dir``:

    * ``structured.json`` — the :class:`~app.ingest.models.StructuredGraph`, when
      one could be extracted;
    * ``vision/NNN.png``  — normalized, EXIF-corrected, ``max_edge``-capped PNGs,
      only when the vision path is actually needed;
    * ``ingest.json``     — this result, so the run is reproducible from disk.

    :param pages: 1-based page numbers. ``None`` means "everything", which is only
        safe for single-page files — the caller is expected to have asked a human
        first, using ``IngestSummary.requires_page_selection``.
    """
    file_path = Path(path)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)

    detection = detection or detect_path(file_path)
    adapter = adapters.get(detection.spec)
    page_refs = adapter.pages(file_path)
    warnings: list[str] = []

    graph: StructuredGraph | None = None
    if adapter.produces_structure:
        try:
            graph = adapter.extract(file_path, pages)
        except UnsupportedFormat as exc:
            # e.g. an SVG whose text was outlined. If pixels are available the run
            # can still go on; if not, the refusal is the whole answer.
            if not adapter.produces_raster:
                raise
            warnings.append(f"{exc.title}: {exc.remedy}")

    structured_path: str | None = None
    if graph is not None and not graph.is_empty:
        structured_path = str(root / "structured.json")
        Path(structured_path).write_text(
            graph.model_dump_json(indent=2), encoding="utf-8"
        )

    summary = summary_for(detection, page_refs, graph, extra_notes=warnings)
    normalized: list[NormalizedPage] = []
    if summary.vision_required:
        if adapter.produces_raster:
            normalized = adapter.rasterize(
                file_path, root / "vision", pages, max_edge=max_edge
            )
        elif structured_path is None:
            raise UnsupportedFormat(
                "ingest_nothing_extracted",
                f"Nothing could be read from that {detection.spec.label}",
                "No graph was extracted and this format cannot be turned into an "
                "image here, so there is nothing for either path to work on.",
                remedy=(
                    "Export the drawing as PNG or PDF from the tool that made it and "
                    "upload that — it will go down the vision path."
                ),
            )
        else:
            warnings.append(
                f"A {detection.spec.label} yields no image here, so the structural "
                "read is all there is. Upload a PNG or PDF export alongside it if a "
                "human needs to check the result against the drawing."
            )

    result = NormalizeResult(
        summary=summary,
        structured_path=structured_path,
        pages=normalized,
        warnings=warnings,
    )
    (root / "ingest.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
