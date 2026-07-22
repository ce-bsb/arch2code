"""Gate parsing, including the historical verdicts that lack the gate string.

The stage-3 gate is a string on the last non-empty line of ``verdict.md``. Three
outcomes exist and the third one is the important one:

* ``approved`` -- the line is exactly ``VERDICT: APPROVED``
* ``blocked``  -- the line is exactly ``VERDICT: BLOCKED``
* ``absent``   -- there is no gate line at all

``absent`` is not a theoretical case. Neither of the two historical runs in
``.arch/`` contains the gate string: both expressed the decision in prose and
stage 4 ran anyway, meaning the gate was satisfied by a person rather than by
the mechanism. These tests exist to guarantee that case never silently becomes
an approval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline import (
    GATE_APPROVED,
    GATE_BLOCKED,
    PIPELINE_STAGES,
    VISION_STAGES,
    artifact_path_for,
    load_gate_strings,
    parse_gate,
    stage_by_id,
    stages_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# the gate string itself
# --------------------------------------------------------------------------- #


def test_gate_strings_are_read_from_the_critic_rule_files():
    """The literal must come from disk, not from a constant in the code.

    The harness was migrated from Portuguese to English and the gate string
    changed with it. Reading the rule file is what makes a future translation a
    visible failure instead of a silent one.
    """
    approved, blocked = load_gate_strings(PROJECT_ROOT)
    assert approved == "VERDICT: APPROVED"
    assert blocked == "VERDICT: BLOCKED"


def test_gate_strings_fall_back_to_the_constants_when_the_rules_are_missing(tmp_path):
    approved, blocked = load_gate_strings(tmp_path)
    assert (approved, blocked) == (GATE_APPROVED, GATE_BLOCKED)


def test_the_rule_file_on_disk_still_documents_both_literals():
    rubric = (PROJECT_ROOT / ".bob" / "rules-arch-critic" / "01-review-rubric.md").read_text(
        encoding="utf-8"
    )
    assert GATE_APPROVED in rubric
    assert GATE_BLOCKED in rubric


# --------------------------------------------------------------------------- #
# parse_gate
# --------------------------------------------------------------------------- #


def test_exact_approved_line_is_approved():
    reading = parse_gate("# review\n\nfindings\n\nVERDICT: APPROVED\n")
    assert reading.verdict == "approved"
    assert reading.gate_line == "VERDICT: APPROVED"
    assert reading.matched == GATE_APPROVED


def test_exact_blocked_line_is_blocked():
    reading = parse_gate("# review\n\nVERDICT: BLOCKED")
    assert reading.verdict == "blocked"
    assert reading.matched == GATE_BLOCKED


def test_trailing_blank_lines_do_not_hide_the_gate_line():
    reading = parse_gate("VERDICT: APPROVED\n\n   \n\t\n")
    assert reading.verdict == "approved"


def test_markdown_decoration_around_the_gate_line_is_tolerated():
    assert parse_gate("**VERDICT: APPROVED**").verdict == "approved"
    assert parse_gate("`VERDICT: BLOCKED`").verdict == "blocked"


def test_a_stray_closing_sentence_after_the_verdict_still_resolves():
    """A model that appends one more line must not turn the run into 'absent'.

    The secondary bottom-up scan catches it, and the recorded gate_line makes
    the deviation visible to the human rather than hiding it.
    """
    text = "findings\n\nVERDICT: BLOCKED\n\nPlease revise the AIR and try again.\n"
    reading = parse_gate(text)
    assert reading.verdict == "blocked"
    assert reading.gate_line == "VERDICT: BLOCKED"


def test_the_last_verdict_wins_when_several_appear():
    text = "Earlier draft said VERDICT: BLOCKED\n\nVERDICT: APPROVED\n"
    assert parse_gate(text).verdict == "approved"


def test_prose_approval_is_never_inferred():
    """The whole point of the gate: an argument is not a decision."""
    text = (
        "# AIR review\n"
        "Everything checks out, the AIR is consistent and I would approve it.\n"
        "No blocking findings were identified. Recommendation: proceed to scaffolding.\n"
    )
    reading = parse_gate(text)
    assert reading.verdict == "absent"
    assert reading.gate_line is None
    assert reading.matched is None


def test_empty_and_whitespace_verdict_files_are_absent():
    assert parse_gate("").verdict == "absent"
    assert parse_gate("\n\n   \n").verdict == "absent"


def test_lowercase_verdict_line_is_matched_by_the_secondary_pass():
    reading = parse_gate("summary\n\nverdict: approved\n")
    assert reading.verdict == "approved"
    assert reading.matched == GATE_APPROVED


def test_a_translated_gate_string_can_be_supplied_explicitly():
    reading = parse_gate(
        "VEREDITO: APROVADO", approved="VEREDITO: APROVADO", blocked="VEREDITO: BLOQUEADO"
    )
    assert reading.verdict == "approved"


def test_excerpt_is_kept_for_the_gate_card():
    reading = parse_gate("line one\nline two\nVERDICT: APPROVED\n")
    assert "line one" in reading.excerpt


def test_a_very_long_verdict_is_excerpted_from_the_end():
    text = ("filler\n" * 5000) + "VERDICT: APPROVED\n"
    reading = parse_gate(text)
    assert reading.verdict == "approved"
    assert reading.excerpt.startswith("…")
    assert reading.excerpt.rstrip().endswith("VERDICT: APPROVED")


# --------------------------------------------------------------------------- #
# the historical verdicts on disk
# --------------------------------------------------------------------------- #

HISTORICAL = sorted((PROJECT_ROOT / ".arch" / "review").glob("*/verdict.md"))


@pytest.mark.skipif(not HISTORICAL, reason="no historical runs in this checkout")
@pytest.mark.parametrize("path", HISTORICAL, ids=lambda p: p.parent.name)
def test_historical_verdicts_are_read_from_the_line_and_never_from_prose(path: Path):
    """A verdict on disk is whatever its gate string says -- and nothing else.

    This test used to assert that every historical verdict reads ``absent``,
    because at the time neither run in ``.arch/`` carried the gate string. Two
    of them now do (``## VERDICT: APPROVED``), so the old assertion had become a
    statement about the data rather than about the parser, and it failed for the
    right reason. Following the instruction the original docstring left behind,
    the test was updated rather than ``parse_gate`` weakened.

    What is asserted now is the invariant that actually matters and that holds
    for any file that ever lands here: an approval is reported **only** when the
    literal gate string is present. A document that argues at length for
    approval and forgets the line still reads ``absent``.
    """
    text = path.read_text(encoding="utf-8")
    reading = parse_gate(text)

    has_approved = GATE_APPROVED.casefold() in text.casefold()
    has_blocked = GATE_BLOCKED.casefold() in text.casefold()

    if reading.verdict == "approved":
        assert has_approved, f"{path} reported approved without containing {GATE_APPROVED!r}"
        assert reading.gate_line is not None
        assert GATE_APPROVED.casefold() in reading.gate_line.casefold()
    elif reading.verdict == "blocked":
        assert has_blocked, f"{path} reported blocked without containing {GATE_BLOCKED!r}"
        assert reading.gate_line is not None
    else:
        assert reading.verdict == "absent"
        assert reading.gate_line is None
        assert not has_approved and not has_blocked, (
            f"{path} contains a gate string but parse_gate reported absent"
        )


@pytest.mark.skipif(not HISTORICAL, reason="no historical runs in this checkout")
def test_prose_only_verdicts_still_exist_on_disk_and_read_absent():
    """The ``absent`` case is not theoretical -- keep a real example of it.

    At least one historical verdict decided in prose with no machine-readable
    gate line, and stage 4 ran anyway. That is the defect the UI has to surface,
    so it is worth failing the build if the last such file disappears and the
    ``absent`` path stops being exercised against real data.
    """
    absent = [p for p in HISTORICAL if parse_gate(p.read_text(encoding="utf-8")).verdict == "absent"]
    assert absent, (
        "every verdict.md under .arch/review now carries a gate line; "
        "the 'decided in prose' case is no longer covered by real data"
    )


# --------------------------------------------------------------------------- #
# the stage table
# --------------------------------------------------------------------------- #


def test_pipeline_has_five_stages_in_order():
    assert [s.id for s in PIPELINE_STAGES] == [
        "intake",
        "analyst",
        "critic",
        "scaffold",
        "validator",
    ]
    assert [s.index for s in PIPELINE_STAGES] == [1, 2, 3, 4, 5]


def test_only_the_critic_is_a_gate():
    gates = [s.id for s in PIPELINE_STAGES if s.is_gate]
    assert gates == ["critic"]


def test_no_stage_pins_its_own_approval_mode():
    """This test used to assert the bug.

    It read `s.approval_mode == "yolo"` off PIPELINE_STAGES and required scaffold
    to be the only one — encoding a second, hard-coded copy of a policy that
    lives in bobcli.APPROVAL_BY_SLUG. Because the runner resolves
    `spec.approval_mode or approval_for_slug(spec.slug)`, the copy always won,
    and the analyst, critic and validator ran under auto_edit without
    execute_command while being told to run scripts.

    The specs now carry None and the policy has one home. What each stage
    resolves to is asserted in test_bobcli.py.
    """
    assert all(s.approval_mode is None for s in PIPELINE_STAGES)


def test_the_orchestrator_mode_is_never_a_stage():
    """The webapp is the orchestrator; arch2code is never spawned.

    The gate has to be a human decision in the UI, not a model deciding to
    switch_mode. The health probe still asserts the slug exists.
    """
    assert "arch2code" not in {s.slug for s in PIPELINE_STAGES}


def test_vision_stages_spawn_no_bob_and_spend_nothing():
    assert [s.id for s in VISION_STAGES] == ["capture", "extract"]
    assert all(s.slug is None for s in VISION_STAGES)
    assert all(s.approval_mode is None for s in VISION_STAGES)


def test_stages_for_selects_by_mode():
    assert stages_for("vision") == VISION_STAGES
    assert stages_for("pipeline") == PIPELINE_STAGES


def test_stage_by_id_round_trips_and_rejects_nonsense():
    for spec in (*PIPELINE_STAGES, *VISION_STAGES):
        assert stage_by_id(spec.id) is spec
    with pytest.raises(KeyError):
        stage_by_id("orchestrator")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stage_id,expected",
    [
        ("intake", ".arch/intake/RUN/extraction.json"),
        ("analyst", ".arch/air/RUN/air.json"),
        ("critic", ".arch/review/RUN/verdict.md"),
        ("scaffold", ".arch/build/RUN/manifest.json"),
        ("validator", ".arch/run/RUN/validation.md"),
    ],
)
def test_artifact_paths_match_the_documented_pipeline(stage_id, expected, tmp_path):
    path = artifact_path_for(stage_by_id(stage_id), "RUN", tmp_path)
    assert path == tmp_path / expected


def test_vision_stages_have_no_contracted_arch_artifact(tmp_path):
    for spec in VISION_STAGES:
        assert artifact_path_for(spec, "RUN", tmp_path) is None
