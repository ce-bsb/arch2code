"""Bounding-box normalization: the geometry the overlay is drawn from.

The fixture is a real ``arch_vision_extract_architecture`` payload recorded from
llama-4-maverick on 2026-07-21, trimmed and with two failure modes injected (a
connection pointing at a component that does not exist, and a box outside 0..1).

The test that matters most here is :func:`test_detects_corner_convention`. The
prompt asks the model for ``[x, y, width, height]`` and the model answers with
``[x1, y1, x2, y2]``. Reading the schema instead of the numbers puts every box in
the wrong place, and an overlay that is confidently wrong is worse than no overlay
at all — it is the artefact a reviewer trusts to check the extraction against the
drawing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.vision import (  # noqa: E402
    compose_claim,
    detect_bbox_convention,
    enrich_extraction,
    normalize_bbox,
    normalize_bboxes,
    raw_bbox,
    summarize_quality,
)

FIXTURE = Path(__file__).parent / "fixtures" / "extraction_sample.json"


@pytest.fixture(scope="module")
def extraction() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Convention detection
# --------------------------------------------------------------------------- #
def test_detects_corner_convention(extraction: dict) -> None:
    detection = detect_bbox_convention(extraction)
    assert detection.convention == "xyxy"
    assert detection.xyxy_votes > detection.xywh_votes
    assert not detection.matches_schema
    assert "corner" in detection.as_dict()["note"]


def test_xywh_wins_by_default_and_on_a_tie() -> None:
    # Every box is readable both ways: nothing votes, so the documented shape holds.
    ambiguous = {
        "components": [
            {"id": "a", "evidence": {"kind": "bbox", "value": [0.1, 0.1, 0.2, 0.2]}},
            {"id": "b", "evidence": {"kind": "bbox", "value": [0.3, 0.3, 0.4, 0.4]}},
        ]
    }
    detection = detect_bbox_convention(ambiguous)
    assert detection.convention == "xywh"
    assert detection.ambiguous == 2


def test_empty_extraction_does_not_raise() -> None:
    assert detect_bbox_convention({}).convention == "xywh"
    assert normalize_bboxes({}) == {}
    assert enrich_extraction({})["components"] == []
    assert enrich_extraction(None)["connections"] == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Clamping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ([0.1, 0.2, 0.3, 0.4], {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}),
        (["0.1", "0.2", "0.3", "0.4"], {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}),
        ({"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
         {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}),
    ],
)
def test_valid_boxes_pass_through_unchanged(raw, expected) -> None:
    box, warning = normalize_bbox(raw)
    assert box is not None
    assert box.as_dict() == expected
    assert box.clamped is False
    assert warning is None


def test_out_of_range_is_clamped_not_dropped() -> None:
    box, warning = normalize_bbox([0.9, 0.9, 0.5, 0.5])
    assert box is not None
    # The element was seen; only its extent was wrong.
    assert box.as_dict() == {"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1}
    assert box.clamped is True
    assert warning and "clamped" in warning


def test_clamping_preserves_the_edge_that_was_right() -> None:
    # x is negative, so the LEFT edge is wrong and the right edge (x + w = 0.4) is
    # not. Clamping the origin while keeping the width would slide the whole box.
    box, _ = normalize_bbox([-0.1, 0.2, 0.5, 0.4])
    assert box is not None
    assert box.as_dict() == {"x": 0.0, "y": 0.2, "w": 0.4, "h": 0.4}


@pytest.mark.parametrize(
    "raw",
    [None, [1, 2, 3], "nonsense", [float("nan"), 0, 1, 1], [0, 0, 0, 0], [1.5, 1.5, 0.2, 0.2]],
)
def test_undrawable_boxes_are_dropped_without_raising(raw) -> None:
    box, _ = normalize_bbox(raw)
    assert box is None


def test_reversed_corners_are_the_same_rectangle() -> None:
    # A right-to-left arrow arrives with x2 < x1. Dropping it would erase a
    # connection the model read perfectly well.
    box, warning = normalize_bbox([0.833, 0.678, 0.754, 0.809], "xyxy")
    assert box is not None
    assert box.as_dict() == {"x": 0.754, "y": 0.678, "w": 0.079, "h": 0.131}
    assert warning and "reverse order" in warning


def test_no_spurious_warning_from_float_error() -> None:
    # 0.1 + 0.2 - 0.1 is not 0.2. A warning on a perfectly good box teaches the
    # user to ignore the warnings that matter.
    box, warning = normalize_bbox([0.1, 0.1, 0.2, 0.1])
    assert box is not None and box.clamped is False
    assert warning is None


# --------------------------------------------------------------------------- #
# Enrichment: what the front end actually receives
# --------------------------------------------------------------------------- #
def test_every_box_is_inside_the_image(extraction: dict) -> None:
    enriched = enrich_extraction(extraction)
    for key in ("components", "connections", "boundaries"):
        for entry in enriched[key]:
            box = entry["bbox"]
            if box is None:
                continue
            assert 0.0 <= box["x"] <= 1.0
            assert 0.0 <= box["y"] <= 1.0
            assert 0.0 < box["w"] <= 1.0
            assert 0.0 < box["h"] <= 1.0
            assert box["x"] + box["w"] <= 1.0 + 1e-9
            assert box["y"] + box["h"] <= 1.0 + 1e-9
            # Both shapes are offered: dict for the lists, array for the canvas.
            assert entry["bbox_array"] == [box["x"], box["y"], box["w"], box["h"]]


def test_known_component_lands_where_it_is_drawn(extraction: dict) -> None:
    # The "watsonx.orchestrate" caption sits in the top-left corner of the
    # recorded diagram. Under the schema's own reading it would be twice as tall.
    enriched = enrich_extraction(extraction)
    box = next(c for c in enriched["components"] if c["id"] == "watsonx_orchestrate")["bbox"]
    assert box["x"] == pytest.approx(0.017, abs=0.005)
    assert box["y"] == pytest.approx(0.022, abs=0.005)
    assert box["w"] == pytest.approx(0.189, abs=0.005)
    assert box["h"] == pytest.approx(0.069, abs=0.005)


def test_out_of_range_element_is_warned_and_still_listed(extraction: dict) -> None:
    enriched = enrich_extraction(extraction)
    ghost = next(c for c in enriched["connections"] if c["id"] == "ghost_edge")
    assert ghost["bbox_clamped"] is True
    assert ghost["bbox_warning"]
    assert any(w["id"] == "ghost_edge" for w in enriched["_bbox_warnings"])


def test_confidence_is_coerced_to_a_float_or_null() -> None:
    enriched = enrich_extraction(
        {
            "components": [
                {"id": "a", "confidence": "0.5"},
                {"id": "b", "confidence": "not a number"},
                {"id": "c", "confidence": 3},
            ]
        }
    )
    assert [c["confidence"] for c in enriched["components"]] == [0.5, None, 1.0]


def test_raw_bbox_reads_the_verified_evidence_shape() -> None:
    # server.py's prompt puts the numbers in evidence.value, not evidence.bbox.
    assert raw_bbox({"evidence": {"kind": "bbox", "value": [0.1, 0.2, 0.3, 0.4]}}) == (
        0.1, 0.2, 0.3, 0.4,
    )
    # Drift is tolerated rather than fatal.
    assert raw_bbox({"evidence": {"bbox": [0.1, 0.2, 0.3, 0.4]}}) is not None
    assert raw_bbox({"bbox": [0.1, 0.2, 0.3, 0.4]}) is not None
    assert raw_bbox({"evidence": "somewhere in the middle"}) is None
    assert raw_bbox({}) is None


# --------------------------------------------------------------------------- #
# Quality signals — the point of Mode A
# --------------------------------------------------------------------------- #
def test_quality_passes_the_server_through_and_derives_the_rest(extraction: dict) -> None:
    quality = summarize_quality(extraction)
    assert "ghost_edge" in quality.broken_refs
    assert "ghost_edge" in quality.connections_needing_verification
    assert quality.action_required
    # Derived client-side, so the UI can rank findings without recomputing.
    # watsonx_orchestrate is the enclosing platform caption: extracted as a
    # component, wired to nothing. That is exactly the sort of thing a reviewer
    # should be shown before it becomes a node in the AIR.
    assert "watsonx_orchestrate" in quality.orphan_components
    # rag IS referenced (external_sources -> rag), so it must not be an orphan.
    assert "rag" not in quality.orphan_components
    assert isinstance(quality.low_confidence_components, list)


def test_quality_of_an_empty_payload_is_empty_not_an_error() -> None:
    quality = summarize_quality({})
    assert quality.broken_refs == []
    assert quality.orphan_components == []
    assert quality.action_required is None


# --------------------------------------------------------------------------- #
# Claim composition
# --------------------------------------------------------------------------- #
def test_claim_uses_the_literal_label_from_the_drawing(extraction: dict) -> None:
    claim = compose_claim(extraction, "connection", "ai_chat_to_portfolio_agent")
    assert "AI CHAT" in claim
    assert "Portfolio Agent" in claim
    assert 10 <= len(claim) <= 500


def test_claim_never_asserts_an_unwritten_protocol(extraction: dict) -> None:
    # `protocol` is the model's classification, not text it read. Asking the
    # verifier whether "jdbc" is written on the arrow invites a "false" about a
    # word nobody ever claimed was drawn.
    for conn in extraction["connections"]:
        protocol = conn.get("protocol")
        if not protocol or protocol == "unknown":
            continue
        claim = compose_claim(extraction, "connection", conn["id"])
        label = (conn.get("evidence") or {}).get("label_text")
        if label != protocol:
            assert protocol not in claim


def test_claim_for_a_missing_target_is_a_404_not_a_crash(extraction: dict) -> None:
    from app.errors import NotFound

    with pytest.raises(NotFound):
        compose_claim(extraction, "component", "no_such_component")
