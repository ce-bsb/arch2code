"""Ingest tests: every adapter against a file the test itself generates.

The bar these tests hold is not "the function returns something". It is:

* the type is decided from **content**, and a file whose bytes contradict its
  name is refused (the latent bug this package was written to close);
* a structural format produces the actual nodes and edges, with evidence;
* a refusal always carries a remedy a person can act on;
* HEIC decodes, which it did not before ``pillow-heif`` was a dependency;
* a multi-page PDF asks which page instead of silently choosing, because each
  page it chooses wrong is a paid vision call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ingest_samples as samples  # noqa: E402

from app.ingest import (  # noqa: E402
    FORMATS,
    IngestError,
    UnsupportedFormat,
    accepted_extensions,
    detect_path,
    inspect_file,
    normalize,
    sniff,
)
from app.ingest.adapters import ADAPTERS  # noqa: E402


# --------------------------------------------------------------------------- #
# registry invariants
# --------------------------------------------------------------------------- #
def test_every_refusal_carries_a_remedy():
    """A format we cannot read must always say how to produce one we can."""
    missing = [s.id for s in FORMATS.values() if s.capability == "refuse" and not s.remedy]
    assert missing == [], f"refusals with no way out: {missing}"


def test_every_readable_format_has_a_registered_adapter():
    missing = [
        s.id
        for s in FORMATS.values()
        if s.capability != "refuse" and s.adapter not in ADAPTERS
    ]
    assert missing == [], f"formats pointing at a missing adapter: {missing}"


def test_accepted_extensions_exclude_refusals():
    assert ".pptx" not in accepted_extensions()
    assert ".vsd" not in accepted_extensions()
    for ext in (".png", ".pdf", ".drawio", ".svg", ".mmd", ".puml", ".vsdx", ".heic"):
        assert ext in accepted_extensions(), ext


# --------------------------------------------------------------------------- #
# detection cascade
# --------------------------------------------------------------------------- #
def test_level1_signature_beats_extension(tmp_path: Path):
    """A PDF renamed .png must be refused, not handed to the image decoder.

    This is the exact latent bug: ``mimetypes.guess_type`` and ``Path.suffix``
    are both pure string matching, so this file used to be routed to vision.
    """
    samples.write_pdf(tmp_path / "real.pdf")
    disguised = tmp_path / "sketch.png"
    disguised.write_bytes((tmp_path / "real.pdf").read_bytes())

    with pytest.raises(UnsupportedFormat) as caught:
        detect_path(disguised)
    assert caught.value.code == "ingest_extension_mismatch"
    assert ".pdf" in caught.value.remedy


def test_raster_peers_disagreeing_is_a_note_not_a_refusal(tmp_path: Path):
    """A PNG named .jpg decodes identically; refusing it would fail a demo."""
    samples.write_png(tmp_path / "x.png", size=(400, 300))
    mislabelled = tmp_path / "x.jpg"
    mislabelled.write_bytes((tmp_path / "x.png").read_bytes())

    detection = detect_path(mislabelled)
    assert detection.format_id == "png"
    assert detection.extension_agrees is False
    assert any("mislabelled" in note for note in detection.notes)


def test_level2_container_separates_identical_zip_signatures(tmp_path: Path):
    """.vsdx, .pptx and .docx all start with 50 4B 03 04; the manifest decides."""
    import io
    import zipfile

    def make(path: Path, content_type: str) -> Path:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                f'<Types><Override ContentType="{content_type}"/></Types>',
            )
        path.write_bytes(buffer.getvalue())
        return path

    pptx = make(tmp_path / "deck.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml")
    docx = make(tmp_path / "doc.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml")
    assert sniff(pptx) == "pptx"
    assert sniff(docx) == "docx"
    assert detect_path(pptx).level == "container"


def test_level3_text_refines_rather_than_contradicts(tmp_path: Path):
    """An <mxfile> inside a .xml is an upgrade to draw.io, not a conflict."""
    path = samples.write_drawio(tmp_path / "diagram.xml")
    detection = detect_path(path)
    assert detection.format_id == "drawio"
    assert detection.level == "text"
    assert detection.extension_agrees is True


def test_empty_file_is_refused_with_a_remedy(tmp_path: Path):
    path = tmp_path / "nothing.png"
    path.write_bytes(b"")
    with pytest.raises(IngestError) as caught:
        detect_path(path)
    assert caught.value.code == "ingest_empty_file"
    assert caught.value.remedy


def test_unknown_binary_never_falls_back_to_vision(tmp_path: Path):
    path = tmp_path / "mystery.bin"
    path.write_bytes(bytes(range(8)) * 64)
    with pytest.raises(IngestError) as caught:
        inspect_file(path)
    assert "export" in caught.value.remedy.lower()


@pytest.mark.parametrize(
    "name, blob, expected",
    [
        ("legacy.vsd", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, "visio-legacy"),
    ],
)
def test_custom_signatures(tmp_path: Path, name: str, blob: bytes, expected: str):
    path = tmp_path / name
    path.write_bytes(blob)
    assert sniff(path) == expected
    with pytest.raises(IngestError) as caught:
        inspect_file(path)
    assert ".vsdx" in caught.value.remedy


# --------------------------------------------------------------------------- #
# structural adapters
# --------------------------------------------------------------------------- #
def test_drawio_compressed_payload(tmp_path: Path):
    """The default draw.io encoding: base64 -> raw deflate(-15) -> URL-decode."""
    path = samples.write_drawio(tmp_path / "d.drawio", compressed=True)
    report = inspect_file(path)
    graph = report.graph

    assert report.summary.format_id == "drawio"
    assert report.summary.vision_required is False
    labels = {n.label for n in graph.nodes}
    assert labels == {"Checkout API", "Orders DB", "Kafka"}
    assert {n.kind_hint for n in graph.nodes} == {"service", "database", "queue"}
    edges = {(e.source, e.target, e.protocol_hint) for e in graph.edges}
    assert ("n1", "n2", "http") in edges
    # The Portuguese alternative in the protocol table is load-bearing.
    assert ("n1", "n3", "kafka") in edges
    # Evidence has to be re-findable in the source file.
    assert all("mxCell[@id=" in n.evidence for n in graph.nodes)
    # Geometry normalized into 0..1 against the drawing's own extent.
    for node in graph.nodes:
        assert node.bbox is not None
        assert 0.0 <= node.bbox.x <= 1.0 and 0.0 <= node.bbox.y <= 1.0


def test_free_multipage_formats_do_not_ask_a_pointless_question(tmp_path: Path):
    """A two-tab .drawio is fully parsed before anyone could answer, so no question.

    The page question exists because a page on the vision path costs an inference
    call. Asking it where pages are free is ceremony, and it would put a modal
    between the user and a run that was already complete.
    """
    path = samples.write_drawio(tmp_path / "d.drawio")
    summary = inspect_file(path).summary
    assert summary.page_count == 2
    assert summary.requires_page_selection is False
    assert any("nothing to choose" in note for note in summary.notes)


def test_drawio_png_is_read_without_vision(tmp_path: Path):
    """The free structural win: draw.io PNGs carry their own source in a tEXt chunk."""
    path = samples.write_drawio_png(tmp_path / "export.png")
    report = inspect_file(path)
    assert report.summary.format_id == "drawio-png"
    assert report.summary.capability == "hybrid"
    assert report.summary.structure_edges == 2
    assert report.summary.vision_required is False, "a complete graph must not cost tokens"


def test_svg_labels_and_the_honest_missing_edges(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "d.svg", samples.SVG_SOURCE)
    report = inspect_file(path)
    labels = {n.label for n in report.graph.nodes}
    assert {"Checkout API", "Orders DB", "POST /orders"} <= labels
    assert report.graph.edges == []
    # Labels without relationships is a partial read, and it says so.
    assert report.summary.vision_required is True
    assert any("relationship" in w for w in report.graph.warnings)


def test_svg_with_outlined_text_is_refused_with_instructions(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "outlined.svg", samples.SVG_OUTLINED)
    with pytest.raises(UnsupportedFormat) as caught:
        inspect_file(path)
    assert caught.value.code == "ingest_svg_no_text"
    assert "outline" in caught.value.remedy.lower()


def test_mermaid_flowchart(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "d.mmd", samples.MERMAID_SOURCE)
    graph = inspect_file(path).graph
    by_id = {n.id: n.label for n in graph.nodes}
    assert by_id["api"] == "Checkout API"
    assert by_id["db"] == "Orders DB"
    assert by_id["kafka"] == "Kafka"
    edges = {(e.source, e.target, e.label) for e in graph.edges}
    assert ("api", "db", "POST /orders") in edges     # |label| form
    assert ("api", "kafka", "") in edges              # dotted arrow
    assert ("kafka", "worker", "consumes") in edges   # -- label --> form
    assert all(e.evidence.startswith("line ") for e in graph.edges)


def test_plantuml_declarations_and_arrow_direction(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "d.puml", samples.PLANTUML_SOURCE)
    graph = inspect_file(path).graph
    by_id = {n.id: n.label for n in graph.nodes}
    assert by_id["api"] == "Checkout API"
    assert by_id["db"] == "Orders DB"
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("Customer", "api") in edges
    assert ("api", "db") in edges
    # `bus <-- worker` points from worker to bus, not the other way round.
    assert ("worker", "bus") in edges
    assert {e.protocol_hint for e in graph.edges} >= {"https", "sql", "kafka"}


def test_bpmn_sequence_flows_and_layout(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "p.bpmn", samples.BPMN_SOURCE)
    graph = inspect_file(path).graph
    assert {n.id for n in graph.nodes} == {"Start_1", "Task_1", "Gate_1", "End_1"}
    assert ("Start_1", "Task_1") in {(e.source, e.target) for e in graph.edges}
    gateway = next(n for n in graph.nodes if n.id == "Gate_1")
    assert gateway.kind_hint == "decision"
    assert gateway.bbox is not None, "BPMNShape/dc:Bounds should give coordinates"


def test_archimate_open_exchange(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "m.archimate", samples.ARCHIMATE_SOURCE)
    graph = inspect_file(path).graph
    assert {n.label for n in graph.nodes} == {"Checkout API", "Orders", "Customer"}
    assert ("e1", "e2") in {(e.source, e.target) for e in graph.edges}
    assert next(n for n in graph.nodes if n.id == "e1").kind_hint == "service"


def test_staruml_skips_views_and_reads_relationships(tmp_path: Path):
    path = samples.write_text_sample(tmp_path / "m.mdj", samples.STARUML_SOURCE)
    graph = inspect_file(path).graph
    labels = {n.label for n in graph.nodes}
    assert {"CheckoutAPI", "OrdersDB"} <= labels
    assert "Main" not in labels, "a diagram is the picture of the model, not a node"
    assert ("C1", "C2") in {(e.source, e.target) for e in graph.edges}


def test_sparx_qea_reads_the_sqlite_repository(tmp_path: Path):
    path = samples.write_qea(tmp_path / "model.qea")
    report = inspect_file(path)
    assert report.summary.format_id == "sparx-qea"
    assert report.summary.detected_by == "signature"
    assert {n.label for n in report.graph.nodes} == {"Checkout API", "Orders DB"}
    assert ("1", "2") in {(e.source, e.target) for e in report.graph.edges}
    assert all(n.bbox is not None for n in report.graph.nodes)


def test_vsdx_reads_real_connectors(tmp_path: Path):
    path = samples.write_vsdx(tmp_path / "drawing.vsdx")
    report = inspect_file(path)
    assert report.summary.format_id == "vsdx"
    assert report.summary.detected_by == "container"
    assert report.summary.structure_edges >= 1, "a glued connector is an edge"
    assert report.summary.vision_required is False


# --------------------------------------------------------------------------- #
# raster and hybrid
# --------------------------------------------------------------------------- #
def test_png_is_normalized_to_the_edge_budget(tmp_path: Path):
    source = samples.write_png(tmp_path / "big.png", size=(2400, 1200))
    result = normalize(source, tmp_path / "out")
    assert result.structured_path is None
    assert len(result.pages) == 1
    page = result.pages[0]
    assert max(page.width, page.height) == 1568
    assert Path(page.path).exists()
    assert Path(page.path).suffix == ".png"


def test_heic_decodes(tmp_path: Path):
    """Confirmed latent bug: .heic was advertised with no decoder installed."""
    source = samples.write_heic(tmp_path / "whiteboard.heic")
    report = inspect_file(source)
    assert report.summary.format_id == "heic"
    result = normalize(source, tmp_path / "out")
    assert len(result.pages) == 1
    assert max(result.pages[0].width, result.pages[0].height) == 1568


def test_multipage_tiff_asks_which_frame(tmp_path: Path):
    source = samples.write_multipage_tiff(tmp_path / "scan.tif")
    report = inspect_file(source)
    assert report.summary.page_count == 3
    assert report.summary.requires_page_selection is True
    assert report.summary.suggested_pages == [1]


def test_pdf_reports_pages_and_never_chooses_one(tmp_path: Path):
    source = samples.write_pdf(tmp_path / "deck.pdf", pages=2)
    report = inspect_file(source)
    assert report.summary.format_id == "pdf"
    assert report.summary.page_count == 2
    assert report.summary.requires_page_selection is True
    assert report.pages[0].likely_diagram is True
    assert report.pages[0].text_chars > 0
    assert report.pages[1].likely_diagram is False, "an appendix page is not the diagram"
    assert report.graph is None, "no page was chosen, so no page was parsed"


def test_pdf_page_selection_produces_both_halves(tmp_path: Path):
    source = samples.write_pdf(tmp_path / "deck.pdf", pages=2)
    result = normalize(source, tmp_path / "out", pages=[1])

    assert len(result.pages) == 1 and result.pages[0].index == 1
    assert result.pages[0].origin == "render"
    assert result.pages[0].render_dpi and result.pages[0].render_dpi >= 72

    graph = json.loads(Path(result.structured_path).read_text(encoding="utf-8"))
    labels = {n["label"] for n in graph["nodes"]}
    assert {"Checkout API", "Orders DB"} <= labels
    assert graph["edges"] == [], "a stroked path in a PDF is not a relationship"
    assert result.summary.vision_required is True


def test_pdf_page_ceiling_is_a_cost_guard(tmp_path: Path):
    from app.ingest.adapters.pdf import MAX_PAGES_PER_CALL

    source = samples.write_pdf(tmp_path / "long.pdf", pages=MAX_PAGES_PER_CALL + 2)
    with pytest.raises(IngestError) as caught:
        normalize(source, tmp_path / "out")
    assert caught.value.code == "ingest_pdf_too_many_pages"
    assert "select" in caught.value.remedy.lower()


def test_normalize_writes_the_documented_layout(tmp_path: Path):
    source = samples.write_drawio(tmp_path / "d.drawio")
    out = tmp_path / "out"
    result = normalize(source, out)
    assert (out / "structured.json").exists()
    assert (out / "ingest.json").exists()
    assert not (out / "vision").exists(), "a complete graph must not rasterize anything"
    assert result.summary.structure_edges == 2
