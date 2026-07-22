"""Transient vs permanent pre-flight failures, and the single retry they earn.

The failure this whole file exists for was measured in production, run
``20260722-1526-e2e``, with 98.79 of the account budget still unspent:

    stage 1  intake   exit 0, 132 s, 1466 NDJSON lines
    stage 2  analyst  exit 1,  12 s, ZERO NDJSON lines
    stage 3  critic   exit 0,  81 s,  980 NDJSON lines

Stage 2's entire stderr was::

    YOLO mode is enabled. All tool calls will be automatically approved.
    Failed to fetch team user budget - HTTP 401:  - {"message":"API Key
    verification failed: Authz service returned status 504 for API Key
    validation","error":"unauthorized"}

The same API key worked in stage 1 twelve seconds earlier and in stage 3
afterwards, so the key was never invalid: IBM's authorization service was
unavailable for a moment and Bob reported that as a 401. A few seconds of
upstream downtime cost the run the analyst's reasoning, which fell back to the
deterministic AIR when repeating the stage would have been enough.

Two claims are tested here, and both halves matter:

* the classifier calls that chain **transient** and calls a real rejection —
  an invalid key, an unaccepted licence — **permanent**, because retrying a
  rejection spends quota and delays the diagnosis;
* the runner repeats such a stage **once, and only once**, never after any
  output has been produced, and never after a stall — and says so on the
  timeline.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.bobproc import ProcResult
from app.errors import classify_preflight_failure
from app.pipeline import (
    MAX_STAGE_ATTEMPTS,
    RETRY_BACKOFF_S,
    should_retry_stage,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "20260722-1526-e2e"

#: Verbatim, unfiltered, from the stage-2 log of the run above. Reproduced as a
#: literal on purpose: a paraphrase would test the paraphrase.
OBSERVED_504_STDERR = (
    "YOLO mode is enabled. All tool calls will be automatically approved.\n"
    "Failed to fetch team user budget - HTTP 401:  - "
    '{"message":"API Key verification failed: Authz service returned status 504 '
    'for API Key validation","error":"unauthorized"}\n'
)


# --------------------------------------------------------------------------- #
# the classifier
# --------------------------------------------------------------------------- #


def test_the_observed_504_wrapped_in_a_401_is_transient():
    """The case that motivated all of this.

    The 401 is the shape of the answer; the 504 inside it is the cause. Reading
    only the outer status is what made a few seconds of IBM downtime cost a
    whole stage of reasoning.
    """
    verdict = classify_preflight_failure(OBSERVED_504_STDERR)
    assert verdict.transient is True
    assert verdict.marker == "HTTP 504"
    # The sentence reaches the timeline, so it has to name the evidence.
    assert "504" in verdict.reason


def test_a_real_401_with_no_5xx_inside_it_is_permanent():
    """An invalid key is a rejection. Repeating it fails identically."""
    stderr = (
        "Failed to fetch team user budget - HTTP 401:  - "
        '{"message":"API Key verification failed: provided API key could not be '
        'found","error":"unauthorized"}\n'
    )
    verdict = classify_preflight_failure(stderr)
    assert verdict.transient is False


def test_an_unaccepted_licence_is_permanent():
    """No amount of repetition accepts a licence."""
    stderr = (
        "You must accept the license agreement before using Bob. "
        "Run `bob --show-license` to review the terms.\n"
    )
    verdict = classify_preflight_failure(stderr)
    assert verdict.transient is False
    assert verdict.marker == "licence not accepted"


def test_a_network_timeout_is_transient():
    stderr = "FetchError: request to https://api.eu-de.ibm.com/v1/chat failed, reason: connect ETIMEDOUT 10.0.0.1:443\n"
    verdict = classify_preflight_failure(stderr)
    assert verdict.transient is True
    assert verdict.marker == "ETIMEDOUT"


@pytest.mark.parametrize(
    "stderr,expected",
    [
        # Every server-side status the task names, whatever wraps it.
        ("HTTP 502 Bad Gateway", True),
        ("Authz service returned status 503 for API Key validation", True),
        ("getaddrinfo EAI_AGAIN iam.cloud.ibm.com", True),
        ("Error: socket hang up", True),
        ("read ECONNRESET", True),
        ("Error: connection reset by peer", True),
        # Rejections. None of these gets a second attempt.
        ('error: Invalid values:\n  Argument: chat-mode, Given: "arch-analyst"', False),
        ("HTTP 403: your quota for this account has been exhausted", False),
        ("Error: no API key was provided", False),
        ("HTTP 401: unauthorized", False),
        # In doubt, permanent: an unrecognized message is not retried.
        ("Segmentation fault", False),
        ("", False),
    ],
)
def test_the_classification_table(stderr, expected):
    assert classify_preflight_failure(stderr).transient is expected


def test_the_classifier_is_pure():
    """Text in, verdict out. Called twice on the same input it says the same.

    This is what makes the production chain above assertable at all, and what
    keeps the retry rule reviewable without a Bob install.
    """
    first = classify_preflight_failure(OBSERVED_504_STDERR)
    second = classify_preflight_failure(OBSERVED_504_STDERR)
    assert first == second


# --------------------------------------------------------------------------- #
# the retry policy, as a pure decision
# --------------------------------------------------------------------------- #


def _result(**overrides) -> ProcResult:
    """A failed pre-flight ProcResult: exit 1, fast, nothing on stdout."""
    base = dict(
        exit_code=1,
        duration_ms=12_000,
        stdout_bytes=0,
        stdout_lines=0,
        stderr_text=OBSERVED_504_STDERR,
        used_pty=False,
        timed_out=False,
        empty_stdout=True,
    )
    base.update(overrides)
    return ProcResult(**base)


def test_the_first_attempt_of_the_observed_failure_is_retried():
    decision = should_retry_stage(result=_result(), attempt=1)
    assert decision.retry is True
    assert decision.marker == "HTTP 504"


def test_the_second_attempt_is_never_retried():
    """One retry. Not a loop. Not a back-off curve to tune."""
    assert MAX_STAGE_ATTEMPTS == 2
    assert should_retry_stage(result=_result(), attempt=2).retry is False


def test_a_stage_that_produced_output_is_never_retried():
    """Inference was billed and the artifact may be half-written."""
    decision = should_retry_stage(
        result=_result(stdout_lines=1, stdout_bytes=120, empty_stdout=False),
        attempt=1,
    )
    assert decision.retry is False
    assert "already emitted" in decision.reason


def test_a_stall_is_never_retried():
    """The watchdog killed it for going silent, which repeating cannot fix.

    The observed cause of a stall is an exhausted account budget: the backend
    stops answering with no error and no exit. Retrying pays for that twice.
    """
    decision = should_retry_stage(
        result=_result(timed_out=True, duration_ms=180_000, exit_code=-9),
        attempt=1,
    )
    assert decision.retry is False
    assert "silent" in decision.reason


def test_a_cancelled_stage_is_never_retried():
    assert should_retry_stage(result=_result(), attempt=1, cancelled=True).retry is False
    assert should_retry_stage(result=_result(cancelled=True), attempt=1).retry is False


def test_exit_zero_is_never_retried():
    """Exit 0 with a missing artifact is a different failure entirely."""
    decision = should_retry_stage(result=_result(exit_code=0), attempt=1)
    assert decision.retry is False


def test_a_permanent_failure_is_not_retried_even_on_the_first_attempt():
    decision = should_retry_stage(
        result=_result(stderr_text="You must accept the license agreement"), attempt=1
    )
    assert decision.retry is False


def test_the_pause_is_short_enough_to_be_worth_taking():
    """A 504 from a key validator clears in seconds or does not clear at all.

    The window is asserted because the value is a judgement, and a future edit
    that turns it into a minute would quietly convert one lost stage into a run
    that looks hung.
    """
    assert 2.0 <= RETRY_BACKOFF_S <= 5.0


# --------------------------------------------------------------------------- #
# the runner, through a real store and a real event log
# --------------------------------------------------------------------------- #


def _runner_and_store(tmp_path: Path):
    """A PipelineRunner whose whole world is ``tmp_path``.

    ``project_root`` stays the real repository because the constructor reads
    the critic's gate strings off disk from it; ``bob_cwd`` and ``runs_root``
    are redirected into the temporary tree. ``health`` and ``vision`` are None
    on purpose: the stage path must not reach for either.
    """
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
        stages.append(stage)

    store.create(
        RunState(
            run_id=RUN_ID,
            mode="pipeline",
            status="running",
            slug="e2e",
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


def _analyst_spec():
    from app.pipeline import PIPELINE_STAGES

    return next(s for s in PIPELINE_STAGES if s.id == "analyst")


def _drive(tmp_path, monkeypatch, results: list[ProcResult]) -> tuple:
    """Run the analyst stage against a scripted sequence of ProcResults.

    Returns ``(stage_state, store, calls)`` where ``calls`` counts how many
    subprocesses the runner asked for. The back-off is collapsed to keep the
    suite fast; its real value is asserted separately above.
    """
    import app.pipeline as pipeline

    runner, store, _settings = _runner_and_store(tmp_path)
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_S", 0.01)

    calls = {"n": 0}

    async def fake_run_bob(argv, **kwargs):
        index = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        # The driver hands stderr to the runner as it arrives; the retry event
        # is expected to quote it, so the fake has to produce it too.
        chunk = results[index].stderr_text
        if chunk:
            await kwargs["on_stderr"](chunk)
        return results[index]

    monkeypatch.setattr(pipeline, "run_bob", fake_run_bob)

    stage_state = asyncio.run(
        runner._run_bob_stage(RUN_ID, _analyst_spec(), asyncio.Event())
    )
    return stage_state, store, calls["n"]


def _events(store, type_: str) -> list:
    return [e for e in store.eventlog(RUN_ID).read() if e.type == type_]


def test_the_runner_retries_the_observed_failure_exactly_once(tmp_path, monkeypatch):
    """Two attempts, both failing: the stage fails, and it fails honestly.

    A single sequence covers the whole contract, because the parts are one
    claim: retry once, never twice, and leave evidence of both attempts.
    """
    stage_state, store, calls = _drive(
        tmp_path, monkeypatch, [_result(), _result()]
    )

    assert calls == 2, "the transient pre-flight failure must be attempted twice"
    assert stage_state.status == "failed", "both attempts failed; nothing is masked"
    assert stage_state.attempts == 2

    # The timeline says a retry happened and why.
    retries = _events(store, "run.stage.retry")
    assert len(retries) == 1
    data = retries[0].data
    assert data["stage"] == "analyst"
    assert data["attempt"] == 1 and data["next_attempt"] == 2
    assert data["max_attempts"] == MAX_STAGE_ATTEMPTS
    assert data["classification"] == "transient"
    assert data["marker"] == "HTTP 504"
    assert "504" in data["reason"]
    # The stderr that motivated it travels with the event: a reader must not
    # have to correlate two rows to see what the upstream service said.
    assert "Authz service returned status 504" in data["stderr_tail"]

    # Both subprocesses are on the timeline, tagged by attempt, and the stage
    # finished exactly once carrying the total.
    exits = _events(store, "proc.exit")
    assert [e.data["attempt"] for e in exits] == [1, 2]
    finished = _events(store, "run.stage.finished")
    assert len(finished) == 1
    assert finished[0].data["attempts"] == 2
    assert finished[0].data["status"] == "failed"

    # And the count survives a reload, because it is in run.json too.
    on_disk = next(s for s in store.load(RUN_ID).stages if s.id == "analyst")
    assert on_disk.attempts == 2


def test_a_retry_that_succeeds_still_shows_that_it_took_two_attempts(
    tmp_path, monkeypatch
):
    """The point of the feature — and the point of not hiding it.

    The analyst's contracted artifact is written by the second attempt, so the
    stage succeeds. It still reports ``attempts == 2``: a stage that needed two
    tries is an operational fact about the upstream service, not a detail to
    round away.
    """
    import app.pipeline as pipeline
    from app.pipeline import artifact_path_for

    runner, store, settings = _runner_and_store(tmp_path)
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_S", 0.01)

    air_path = artifact_path_for(_analyst_spec(), RUN_ID, settings.bob_cwd)
    calls = {"n": 0}

    async def fake_run_bob(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            await kwargs["on_stderr"](OBSERVED_504_STDERR)
            return _result()
        # The second attempt behaves like a stage that did its job.
        air_path.parent.mkdir(parents=True, exist_ok=True)
        air_path.write_text(json.dumps({"air_version": "1.0"}), encoding="utf-8")
        return _result(
            exit_code=0,
            stdout_lines=980,
            stdout_bytes=120_000,
            stderr_text="",
            empty_stdout=False,
            duration_ms=81_000,
        )

    monkeypatch.setattr(pipeline, "run_bob", fake_run_bob)

    stage_state = asyncio.run(
        runner._run_bob_stage(RUN_ID, _analyst_spec(), asyncio.Event())
    )

    assert calls["n"] == 2
    assert stage_state.status == "succeeded"
    assert stage_state.attempts == 2
    assert len(_events(store, "run.stage.retry")) == 1


def test_a_stage_that_produced_output_is_not_repeated_by_the_runner(
    tmp_path, monkeypatch
):
    """Re-running it would pay for inference twice and could clobber its file."""
    stage_state, store, calls = _drive(
        tmp_path,
        monkeypatch,
        [_result(stdout_lines=42, stdout_bytes=9000, empty_stdout=False)],
    )
    assert calls == 1
    assert stage_state.status == "failed"
    assert stage_state.attempts == 1
    assert _events(store, "run.stage.retry") == []


def test_a_stalled_stage_is_not_repeated_by_the_runner(tmp_path, monkeypatch):
    """The stall watchdog's kill is not a pre-flight failure.

    This is the case that already has a designed answer — the deterministic AIR
    fallback — and repeating it first would spend the budget that made it stall.
    """
    stage_state, store, calls = _drive(
        tmp_path,
        monkeypatch,
        [_result(timed_out=True, exit_code=-9, duration_ms=180_000, stderr_text="")],
    )
    assert calls == 1
    assert stage_state.attempts == 1
    assert _events(store, "run.stage.retry") == []
    assert stage_state.error is not None
    assert stage_state.error.code == "stage_timeout"


def test_a_permanent_failure_is_not_repeated_by_the_runner(tmp_path, monkeypatch):
    """An unaccepted licence fails once, immediately, with its own remedy."""
    stage_state, store, calls = _drive(
        tmp_path,
        monkeypatch,
        [_result(stderr_text="You must accept the license agreement first.\n")],
    )
    assert calls == 1
    assert _events(store, "run.stage.retry") == []
    assert stage_state.error is not None
    assert stage_state.error.code == "bob_license_not_accepted"


def test_the_analyst_fallback_only_runs_after_the_retry_has_also_failed(
    tmp_path, monkeypatch
):
    """Ordering, stated as a test because it is the whole point of the change.

    The deterministic AIR is the answer of last resort. If it were reached
    before the retry, a transient 504 would still cost the run its reasoning —
    which is exactly what happened in 20260722-1526-e2e.

    The retry lives inside ``_run_bob_stage`` and the fallback is applied by
    ``_execute`` on the StageState it returns, so the order is structural: the
    stage cannot come back until both attempts are spent. Asserted here against
    the event log, which is the artifact a reviewer actually reads.
    """
    stage_state, store, calls = _drive(tmp_path, monkeypatch, [_result(), _result()])
    assert calls == 2

    types = [e.type for e in store.eventlog(RUN_ID).read()]
    assert "run.stage.retry" in types
    # No fallback yet: _run_bob_stage never applies it, and it will only be
    # considered now that the stage has come back failed.
    assert "run.stage.fallback" not in types

    runner, store2, settings = _runner_and_store(tmp_path / "second")
    extraction = settings.bob_cwd / f".arch/intake/{RUN_ID}/extraction.json"
    extraction.parent.mkdir(parents=True, exist_ok=True)
    extraction.write_text("{}", encoding="utf-8")
    applied = asyncio.run(
        runner._apply_air_fallback(RUN_ID, _analyst_spec(), stage_state)
    )
    assert applied is True
    fallback = _events(store2, "run.stage.fallback")
    assert len(fallback) == 1
    # The fallback carries the failure it replaced, so the 504 is not erased by
    # the degraded artifact that followed it.
    assert fallback[0].data["original_error"]["code"] == stage_state.error.code


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #


def test_the_browser_is_subscribed_to_the_retry_event():
    """``EVENT_TYPES`` in sse.js is the delivery list, not documentation.

    ``app/sse.py`` writes an ``event: <type>`` line on every frame, and a named
    SSE frame is dispatched only to a listener registered for that exact name.
    An event the server emits and that array omits never reaches the live UI —
    the retry would then be invisible until a reload, which is precisely the
    kind of quiet that this change exists to remove.
    """
    sse_js = (
        Path(__file__).resolve().parents[1] / "static" / "js" / "sse.js"
    ).read_text(encoding="utf-8")
    vocabulary = sse_js.split("EVENT_TYPES = [", 1)[1].split("];", 1)[0]
    assert "'run.stage.retry'" in vocabulary
