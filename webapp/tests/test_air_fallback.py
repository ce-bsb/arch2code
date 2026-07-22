"""The deterministic AIR fallback, and the promise that it never passes for real.

Stage 2 is the one stage whose output is partly reconstructible without a model:
everything it *derives* from ``extraction.json`` is a transform, and everything
it *adds* — assumptions with a declared impact, falsifiable hypotheses — is not.
``app.air_fallback`` builds the first half and refuses the second.

The tests are organized around the two claims that matter:

**It is a real AIR.** Every produced document validates against
``.bob/skills/air-normalizer/air.schema.json`` — the actual file on disk, not a
copy — including for extractions whose ids, kinds and protocols are outside the
AIR vocabulary entirely. If the schema changes, these fail.

**It is visibly not a reasoned AIR.** ``assumptions`` is empty, ``meta.extractor``
says the analyst did not run, and a blocking unknown says no assumption was
declared or impact-assessed. The consequence is asserted end to end by running
``validate_air.py --gate`` as a subprocess and requiring exit 1. That rejection
is the designed outcome, not a defect: the run reaches the human gate carrying an
honest account of what was and was not done.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.air_fallback import (
    AIR_VERSION,
    FALLBACK_EXTRACTOR,
    FALLBACK_UNKNOWN_ID,
    LOW_CONFIDENCE_BELOW,
    build_fallback_air,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / ".bob" / "skills" / "air-normalizer" / "air.schema.json"
VALIDATOR = PROJECT_ROOT / ".bob" / "skills" / "air-normalizer" / "scripts" / "validate_air.py"

#: The run the fallback was designed against. It is not in the repository (runs
#: are local artifacts), so the suite falls back to the checked-in fixture rather
#: than skipping: the contract has to hold for both.
TARGET_EXTRACTION = PROJECT_ROOT / ".arch" / "intake" / "20260722-1343-image-24" / "extraction.json"
FIXTURE_EXTRACTION = Path(__file__).resolve().parent / "fixtures" / "extraction_sample.json"


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


def _observed_extraction() -> tuple[str, dict[str, Any]]:
    """The real extraction if it is on this machine, else the fixture."""
    path = TARGET_EXTRACTION if TARGET_EXTRACTION.exists() else FIXTURE_EXTRACTION
    run_id = path.parent.name if path is TARGET_EXTRACTION else "20260721-1700-smoke"
    return run_id, json.loads(path.read_text(encoding="utf-8"))


#: A second input on purpose: every vocabulary here is foreign to the AIR.
#: ``comp-1`` is not a legal AIR id, ``client``/``plugin`` are not component
#: kinds, ``REST``/``POST`` are not protocols, ``ghost`` points at a component
#: that was never extracted, and the unknown uses ``description`` +
#: ``related_elements`` instead of ``question``. Both shapes exist in
#: ``.arch/intake/`` today.
FOREIGN_EXTRACTION: dict[str, Any] = {
    "meta": {
        "run_id": "20260722-0528-modeb2",
        "source_artifact": ".arch/intake/inbox/20260722-0528-modeb2/exemplo-rascunho.png",
        "source_kind": "napkin",
        "extraction_path": "vision",
        "extracted_at": "2026-07-22T05:30:00Z",
        "extractor": "manual_visual_analysis",
    },
    "components": [
        {
            "id": "comp-1",
            "label": "App Mobile",
            "type": "client",
            "confidence": 0.95,
            "evidence": {"kind": "bbox", "value": [67, 85, 250, 130], "label_text": "App Mobile"},
        },
        {
            "id": "comp-2",
            "label": "API Gateway",
            "type": "gateway",
            "confidence": 0.95,
            "evidence": {"kind": "bbox", "value": [467, 85, 250, 130]},
        },
        {
            "id": "comp-3",
            "label": "Notificacao",
            "type": "service",
            "evidence": {"description": "rounded box at the bottom"},
        },
    ],
    "connections": [
        {
            "id": "conn-1",
            "from": "comp-1",
            "to": "comp-2",
            "protocol": "REST",
            "sync": "sync",
            "confidence": 0.9,
            "evidence": {"kind": "bbox", "value": [317, 115, 150, 50], "label_text": "REST"},
        },
        {
            "id": "conn-2",
            "from": "comp-2",
            "to": "comp-3",
            "protocol": "POST",
            "sync": "whenever",
            "confidence": 0.45,
            "evidence": {"kind": "bbox", "value": [717, 115, 153, 50]},
        },
        {
            "id": "ghost",
            "from": "comp-3",
            "to": "comp-99",
            "protocol": "unknown",
            "sync": "unknown",
            "confidence": 0.3,
            "evidence": {"kind": "bbox", "value": [700, 215, 300, 410], "label_text": "?"},
        },
    ],
    "boundaries": [
        {
            "id": "zone-a",
            "name": "Mobile edge",
            "kind": "perimeter",
            "contains": ["comp-1", "comp-2", "comp-99"],
            "confidence": 0.8,
        }
    ],
    "unknowns": [
        {
            "id": "unknown-1",
            "description": "Arrow to Notificacao has no arrowhead",
            "category": "connection",
            "blocking": True,
            "related_elements": ["conn-2"],
        }
    ],
    "_quality": {"avg_confidence": 0.72},
}


def _build(run_id: str, extraction: dict[str, Any]) -> dict[str, Any]:
    return build_fallback_air(
        extraction,
        run_id=run_id,
        reason="Stage analyst stopped producing output",
    )


CASES = {
    "observed": _observed_extraction(),
    "foreign_vocabulary": ("20260722-0528-modeb2", FOREIGN_EXTRACTION),
    # The degenerate input: the intake wrote a file and read nothing off the
    # drawing. The fallback still has to produce a valid document.
    "empty": ("20260722-1343-image-24", {}),
}


@pytest.fixture(params=sorted(CASES), ids=sorted(CASES))
def air(request) -> dict[str, Any]:
    run_id, extraction = CASES[request.param]
    return _build(run_id, extraction)


# --------------------------------------------------------------------------- #
# it is a real AIR
# --------------------------------------------------------------------------- #


def test_the_schema_is_where_we_think_it_is():
    """A moved schema must fail loudly here, not silently weaken every test below."""
    assert SCHEMA_PATH.exists(), SCHEMA_PATH
    assert VALIDATOR.exists(), VALIDATOR


def test_fallback_air_validates_against_the_schema_on_disk(air):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(air),
        key=lambda e: list(e.path),
    )
    assert not errors, "\n".join(
        f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}" for e in errors
    )
    assert air["air_version"] == AIR_VERSION


def test_ids_are_rewritten_into_the_alphabet_the_schema_allows():
    """``comp-1`` and ``C1`` are real extractor ids and neither is a legal AIR id."""
    air = _build("20260722-0528-modeb2", FOREIGN_EXTRACTION)
    ids = [c["id"] for c in air["components"]]
    assert ids == ["comp_1", "comp_2", "comp_3"]
    # The edges must have been remapped with them, or the AIR is a set of
    # dangling references that reads as valid.
    assert (air["connections"][0]["from"], air["connections"][0]["to"]) == ("comp_1", "comp_2")


def test_unknown_vocabulary_becomes_unknown_and_keeps_the_literal():
    """Mapping ``client`` to ``ui`` or ``REST`` to ``http`` would be a guess."""
    air = _build("20260722-0528-modeb2", FOREIGN_EXTRACTION)
    mobile = air["components"][0]
    assert mobile["kind"] == "unknown"
    assert "'client'" in mobile["note"]

    rest = air["connections"][0]
    assert rest["protocol"] == "unknown"
    assert "'REST'" in rest["note"]
    # The label read off the drawing is preserved verbatim, so nothing is lost.
    assert rest["label"] == "REST"

    weird_sync = air["connections"][1]
    assert weird_sync["sync"] == "unknown"
    assert "'whenever'" in weird_sync["note"]


def test_a_dangling_edge_is_dropped_and_asked_about_by_name():
    """Inventing ``comp-99`` to keep the arrow would be exactly the fabrication."""
    air = _build("20260722-0528-modeb2", FOREIGN_EXTRACTION)
    assert [c["id"] for c in air["connections"]] == ["conn_1", "conn_2"]
    questions = " ".join(u["question"] for u in air["unknowns"])
    assert "'ghost'" in questions and "comp-99" in questions
    # And the boundary cannot keep a member that no longer exists either.
    assert air["boundaries"][0]["contains"] == ["comp_1", "comp_2"]


def test_low_confidence_connections_each_get_their_own_question():
    air = _build("20260722-0528-modeb2", FOREIGN_EXTRACTION)
    low = [c for c in air["connections"] if c["confidence"] < LOW_CONFIDENCE_BELOW]
    assert low, "the fixture must contain at least one low-confidence edge"
    for conn in low:
        assert any(u.get("about") == conn["id"] for u in air["unknowns"])


def test_the_transform_is_deterministic():
    """Same extraction in, byte-identical AIR out — except for the clock."""
    from datetime import datetime, timezone

    clock = datetime(2026, 7, 22, 13, 43, tzinfo=timezone.utc)
    first = build_fallback_air(FOREIGN_EXTRACTION, run_id="20260722-1343-image-24", now=clock)
    second = build_fallback_air(FOREIGN_EXTRACTION, run_id="20260722-1343-image-24", now=clock)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --------------------------------------------------------------------------- #
# it is visibly not a reasoned AIR
# --------------------------------------------------------------------------- #


def test_assumptions_is_empty(air):
    """The single most important assertion in this file.

    An assumption carries an ``impact`` — what breaks in the generated code if it
    is wrong. A transform cannot know that. Emitting a plausible one would make
    stage 4 write code from a sentence nobody stands behind.
    """
    assert air["assumptions"] == []


def test_meta_extractor_says_the_analyst_did_not_run(air):
    extractor = air["meta"]["extractor"]
    assert extractor == FALLBACK_EXTRACTOR
    assert "did not run" in extractor
    assert air["meta"]["human_reviewed_by"] is None


def test_the_blocking_unknown_exists_and_is_open(air):
    blocking = [u for u in air["unknowns"] if u["blocking"] and not u.get("answer")]
    assert blocking, "a fallback AIR with no blocking unknown would sail through the gate"
    headline = [u for u in blocking if u["id"] == FALLBACK_UNKNOWN_ID]
    assert len(headline) == 1
    question = headline[0]["question"]
    assert "arch-analyst" in question
    assert "NO ASSUMPTION WAS DECLARED" in question
    assert "IMPACT" in question


def test_the_blocking_unknown_keeps_its_id_against_a_squatting_extraction():
    """Its id is the handle the critic and these tests reach for. It is reserved.

    Without the reservation an extraction entry carrying the same id would take
    it first and push the real one to ``..._2``, leaving a blocking unknown that
    nothing can look up by name.
    """
    squatter = {
        "unknowns": [
            {"id": FALLBACK_UNKNOWN_ID, "question": "a squatting entry", "blocking": False}
        ]
    }
    air = _build("20260722-1343-image-24", squatter)
    ids = [u["id"] for u in air["unknowns"]]
    assert ids.count(FALLBACK_UNKNOWN_ID) == 1
    blocking = [u for u in air["unknowns"] if u["blocking"]]
    assert [u["id"] for u in blocking] == [FALLBACK_UNKNOWN_ID]


def test_the_experiment_plan_is_minimal_and_about_provenance(air):
    plan = air["experiment_plan"]
    # The schema requires at least one hypothesis, so "none" is not expressible;
    # the one offered claims nothing about the architecture.
    assert len(plan["hypotheses"]) == 1
    assert plan["stack"] == {}
    assert plan["out_of_scope"]
    statement = plan["hypotheses"][0]["statement"].lower()
    assert "observed" in statement and "none was added by reasoning" in statement


def test_overall_confidence_is_derived_and_never_a_constant():
    """It is one of the two numbers the gate thresholds on. It must be measured."""
    assert _build("20260722-0528-modeb2", FOREIGN_EXTRACTION)["meta"]["overall_confidence"] == 0.72
    # No number anywhere in the extraction: the mean of what was extracted.
    no_quality = {"components": [{"id": "a", "name": "A", "kind": "service", "confidence": 0.5}]}
    assert _build("20260722-1343-image-24", no_quality)["meta"]["overall_confidence"] == 0.5
    # Nothing extracted at all: 0.0, which is itself below the gate threshold.
    assert _build("20260722-1343-image-24", {})["meta"]["overall_confidence"] == 0.0


# --------------------------------------------------------------------------- #
# the gate rejects it — end to end, through the real script
# --------------------------------------------------------------------------- #


def _run_validator(air: dict[str, Any], tmp_path: Path, *, gate: bool) -> subprocess.CompletedProcess:
    path = tmp_path / "air.json"
    path.write_text(json.dumps(air, indent=2, ensure_ascii=False), encoding="utf-8")
    argv = [sys.executable, str(VALIDATOR), str(path)] + (["--gate"] if gate else [])
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def test_validate_air_accepts_the_structure(air, tmp_path):
    """Without --gate the document must be clean: shape and semantics both.

    This is what separates "the fallback is honest" from "the fallback is
    broken". A structural error here would mean the transform itself is wrong.
    """
    pytest.importorskip("jsonschema")
    result = _run_validator(air, tmp_path, gate=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_validate_air_gate_rejects_the_fallback(air, tmp_path):
    """The designed outcome: the run reaches the gate and the gate says no.

    If this ever passes, the fallback has started claiming work nobody did.
    """
    pytest.importorskip("jsonschema")
    result = _run_validator(air, tmp_path, gate=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INVALID (gate applied)" in result.stdout
    assert f"open blocking unknown '{FALLBACK_UNKNOWN_ID}'" in result.stdout


# --------------------------------------------------------------------------- #
# when the runner is allowed to use it
# --------------------------------------------------------------------------- #


def test_the_fallback_refuses_when_there_is_nothing_to_transform(tmp_path):
    """No extraction.json means the run fails exactly as it did before."""
    from app.pipeline import PipelineRunner

    reason = PipelineRunner._fallback_refusal(
        tmp_path / "extraction.json", tmp_path / "air.json"
    )
    assert reason is not None and "does not exist" in reason


def test_the_fallback_refuses_to_overwrite_an_air_the_analyst_wrote(tmp_path):
    """A derived AIR must never replace a reasoned one.

    The analyst can write a complete AIR and then be killed by the stall
    watchdog on the way out. Clobbering that file would destroy the only
    artifact in the run that had a model's reasoning behind it.
    """
    from app.pipeline import PipelineRunner

    extraction = tmp_path / "extraction.json"
    extraction.write_text("{}", encoding="utf-8")
    air_path = tmp_path / "air.json"
    air_path.write_text('{"air_version": "1.0"}', encoding="utf-8")

    reason = PipelineRunner._fallback_refusal(extraction, air_path)
    assert reason is not None and "already exists" in reason

    air_path.unlink()
    assert PipelineRunner._fallback_refusal(extraction, air_path) is None


def test_no_stage_other_than_the_analyst_gets_a_fallback():
    """Nothing else in the pipeline has a deterministic substitute.

    Called unbound with a sentinel ``self``: the guard has to return before it
    touches any runner state, which is what this asserts. A regression that
    moved the check later would raise AttributeError here instead of quietly
    fabricating a scaffold.
    """
    import asyncio

    from app.models import StageState
    from app.pipeline import PIPELINE_STAGES, PipelineRunner

    sentinel = object()
    for spec in PIPELINE_STAGES:
        if spec.id == "analyst":
            continue
        failed = StageState(id=spec.id, index=spec.index, title=spec.title, status="failed")
        applied = asyncio.run(
            PipelineRunner._apply_air_fallback(
                sentinel, "20260722-1343-image-24", spec, failed
            )
        )
        assert applied is False, spec.id


# --------------------------------------------------------------------------- #
# the runner actually applying it — through a real store and a real event log
# --------------------------------------------------------------------------- #


RUN_ID = "20260722-1343-image-24"


def _analyst_spec():
    from app.pipeline import PIPELINE_STAGES

    return next(s for s in PIPELINE_STAGES if s.id == "analyst")


def _stalled_error():
    """The error the stall watchdog records when the Bob backend goes quiet."""
    from app.models import ErrorBody

    return ErrorBody(
        code="stage_stalled",
        title="Stage analyst stopped producing output",
        detail="No output for 180s.",
        remedy="Check the Bob account budget.",
    )


def _runner_and_store(tmp_path):
    """A PipelineRunner whose whole world is ``tmp_path``, with a dead analyst.

    ``project_root`` stays the real repository because the constructor reads the
    critic's gate strings off disk from it; everything the fallback touches —
    ``bob_cwd`` (where ``.arch/`` lives) and ``runs_root`` (where run.json and
    events.jsonl live) — is redirected into the temporary tree. ``health`` and
    ``vision`` are None on purpose: if a future edit makes the fallback path
    reach for either, this test fails with AttributeError rather than passing.

    The analyst stage is seeded as ``failed`` **on disk**, because that is the
    state the runner is in when ``_apply_air_fallback`` is called: ``_run_stage``
    has already persisted the failure. It matters for the assertions below —
    the fallback deliberately patches only ``artifacts`` and ``error`` and never
    touches ``status``, so "still failed afterwards" is only a real claim if it
    was failed to begin with.
    """
    from dataclasses import replace
    from datetime import datetime, timezone

    from app.config import load_settings
    from app.eventlog import EventLogRegistry
    from app.models import RunState, Routing, StageState, UploadRef
    from app.pipeline import PipelineRunner, stages_for
    from app.store import RunStore

    settings = replace(
        load_settings({"ARCH2CODE_PROJECT_ROOT": str(PROJECT_ROOT)}),
        bob_cwd=tmp_path / "project",
        runs_root=tmp_path / "runs",
    )
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    store = RunStore(settings, EventLogRegistry(settings.runs_root))

    now = datetime.now(timezone.utc)
    upload = UploadRef(
        upload_id="u1",
        filename="sketch.png",
        content_type="image/png",
        bytes=1,
        sha256="0" * 64,
        stored_path=str(tmp_path / "sketch.png"),
        routing=Routing(
            extraction_path="vision", source_kind="napkin", recommended_tool="arch_vision"
        ),
        created_at=now,
    )
    stages = []
    for spec in stages_for("pipeline"):
        stage = StageState(id=spec.id, index=spec.index, title=spec.title, slug=spec.slug)
        if spec.id == "intake":
            stage.status = "succeeded"
        elif spec.id == "analyst":
            stage.status = "failed"
            stage.error = _stalled_error()
        stages.append(stage)

    store.create(
        RunState(
            run_id=RUN_ID,
            mode="pipeline",
            status="running",
            slug="image-24",
            created_at=now,
            updated_at=now,
            upload=upload,
            source_kind="napkin",
            project_root=str(settings.project_root),
            bob_cwd=str(settings.bob_cwd),
            stages=stages,
        )
    )
    return PipelineRunner(settings, store, None, None), store, settings


def _failed_analyst():
    """What ``_run_stage`` hands back to the loop. Mirrors the seeded run.json."""
    from app.models import StageState

    spec = _analyst_spec()
    return StageState(
        id=spec.id,
        index=spec.index,
        title=spec.title,
        status="failed",
        error=_stalled_error(),
    )


def test_the_runner_writes_the_derived_air_and_says_so(tmp_path):
    """The whole point: the run may continue, and nothing pretends it went well.

    Asserted together because they are one claim. A version that wrote the file
    but marked the stage succeeded, or that marked it degraded but wrote
    nothing, would satisfy half of this and be wrong.
    """
    import asyncio

    pytest.importorskip("jsonschema")
    runner, store, settings = _runner_and_store(tmp_path)
    spec = _analyst_spec()

    extraction_path = settings.bob_cwd / ".arch" / "intake" / RUN_ID / "extraction.json"
    extraction_path.parent.mkdir(parents=True, exist_ok=True)
    extraction_path.write_text(json.dumps(FOREIGN_EXTRACTION), encoding="utf-8")

    applied = asyncio.run(runner._apply_air_fallback(RUN_ID, spec, _failed_analyst()))
    assert applied is True, "the run must be allowed to reach the critic"

    # 1. the artifact exists, at the path the stage was contracted to write.
    from app.pipeline import artifact_path_for

    air_path = artifact_path_for(spec, RUN_ID, settings.bob_cwd)
    air = json.loads(air_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema = pytest.importorskip("jsonschema")
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(air))
    assert air["assumptions"] == []
    assert air["meta"]["extractor"] == FALLBACK_EXTRACTOR

    # 2. the stage is degraded, not recovered. `failed` is the honest status and
    #    the error code is what the UI keys the "fallback" label off after a
    #    reload, when the client-side event flag is gone.
    stage = next(s for s in store.load(RUN_ID).stages if s.id == "analyst")
    assert stage.status == "failed"
    assert stage.error is not None and stage.error.code == "analyst_fallback_applied"
    assert "derived, not reasoned" in stage.error.title

    # 3. the timeline says it out loud.
    events = store.eventlog(RUN_ID).read()
    fallback = [e for e in events if e.type == "run.stage.fallback"]
    assert len(fallback) == 1
    data = fallback[0].data
    assert data["degraded"] is True and data["label"] == "fallback"
    assert data["assumptions"] == 0
    assert data["expect_gate"] == "blocked"
    assert data["blocking_unknown_id"] == FALLBACK_UNKNOWN_ID
    # The original failure is not erased by the one that replaced it.
    assert data["original_error"]["code"] == "stage_stalled"

    # 4. and the gate rejects what was written.
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(air_path), "--gate"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_a_missing_extraction_leaves_the_run_failing_and_explains_why(tmp_path):
    """No input, no fallback — and the refusal is on the timeline, not silent."""
    import asyncio

    runner, store, settings = _runner_and_store(tmp_path)
    spec = _analyst_spec()

    applied = asyncio.run(runner._apply_air_fallback(RUN_ID, spec, _failed_analyst()))
    assert applied is False

    assert artifact_missing(settings, spec)
    events = store.eventlog(RUN_ID).read()
    refusals = [e for e in events if e.type == "run.stage.fallback_unavailable"]
    assert len(refusals) == 1
    assert "does not exist" in refusals[0].data["reason"]
    # The stage keeps the failure it actually had; nothing was substituted.
    stage = next(s for s in store.load(RUN_ID).stages if s.id == "analyst")
    assert stage.error is not None and stage.error.code == "stage_stalled"


def artifact_missing(settings, spec) -> bool:
    from app.pipeline import artifact_path_for

    path = artifact_path_for(spec, RUN_ID, settings.bob_cwd)
    return path is not None and not path.exists()


def test_the_browser_is_subscribed_to_the_fallback_events():
    """``EVENT_TYPES`` in sse.js is the delivery list, not documentation.

    ``app/sse.py`` writes an ``event: <type>`` line on every frame, and a NAMED
    SSE frame is dispatched only to a listener registered for that exact name —
    the ``'message'`` listener beside the loop catches unnamed frames and nothing
    else. An event the server emits and that array omits therefore never reaches
    the live UI at all; it only appears after a reload, which reads run.json.
    That is precisely how a degraded stage would look green until refreshed.
    """
    sse_js = (
        Path(__file__).resolve().parents[1] / "static" / "js" / "sse.js"
    ).read_text(encoding="utf-8")
    vocabulary = sse_js.split("EVENT_TYPES = [", 1)[1].split("];", 1)[0]
    for emitted in ("run.stage.fallback", "run.stage.fallback_unavailable"):
        assert f"'{emitted}'" in vocabulary, emitted
