"""The arch2code stage table, the gate reader and the pipeline state machine.

Three decisions are encoded here and each one is load-bearing.

**The webapp is the orchestrator.** The ``arch2code`` orchestrator chat mode is
never spawned as a subprocess. :class:`PipelineRunner` drives ``arch-intake``,
``arch-analyst``, ``arch-critic``, ``arch-scaffold`` and ``arch-validator``
directly, because the stage-3 gate has to be a human decision in the UI rather
than a model deciding to ``switch_mode``. The health probe still asserts the
``arch2code`` slug exists: its absence proves the working directory is wrong or
``custom_modes.yaml`` is not loading, which is the same root cause that would
break the other five.

**The gate never parks a coroutine.** On reaching stage 3, ``_execute`` writes
``status=awaiting_input`` plus the parsed :class:`GateReading` to ``run.json``,
emits ``gate.evaluated`` and ``run.awaiting_input``, and *returns*. The task is
gone. :meth:`PipelineRunner.resume` writes ``gate/decision.json`` and enqueues a
fresh task from the resolved stage. A run can therefore sit at the gate across a
server restart and still be resumable, because the whole state is on disk.

**A stage succeeds only if the exit code is 0 AND its contracted artifact
exists.** ``arch-scaffold`` under an approval mode that excludes
``write_to_file`` exits 0 and writes nothing; trusting the exit code alone would
report that as a success and then fail confusingly in stage 5.

**A stage gets one retry, and only for an upstream outage that produced
nothing.** Run ``20260722-1526-e2e`` lost the analyst to a 504 from IBM's API
key validator, reported by Bob as a 401, twelve seconds after the same key had
worked in stage 1 and moments before it worked again in stage 3.
:func:`should_retry_stage` is where that judgement lives and every rule it
refuses on is written out there; the retry is announced with its own
``run.stage.retry`` event, because a stage that needed two attempts is an
operational fact and not a detail to round away.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import air_fallback
from . import artifacts as artifacts_mod
from . import projectdiff
from . import prompts as prompts_mod
from . import scripts as scripts_mod
from .bobcli import approval_for_slug, build_argv, redact_argv
from .bobproc import PTY_REMEDY, ProcResult, run_bob, should_suggest_pty
from .config import Settings, subprocess_env
from .errors import (
    AppError,
    Conflict,
    NotFound,
    PreflightVerdict,
    bob_preflight_error,
    classify_preflight_failure,
)
from .health import HealthCache
from .models import (
    ArtifactRef,
    ErrorBody,
    GateDecision,
    GateState,
    GateVerdict,
    RunMode,
    RunState,
    StageId,
    StageState,
    StageStats,
)
from .ndjson import MessageCoalescer, StreamEvent, extract_stats, to_event_payload
from .prompts import PromptContext
from .store import RunStore
from .vision import ArchVisionClient, VisionToolError, summarize_quality

__all__ = [
    "GATE_APPROVED",
    "GATE_BLOCKED",
    "MAX_STAGE_ATTEMPTS",
    "RETRY_BACKOFF_S",
    "RetryDecision",
    "should_retry_stage",
    "StageSpec",
    "PIPELINE_STAGES",
    "VISION_STAGES",
    "stages_for",
    "stage_by_id",
    "artifact_path_for",
    "GateReading",
    "parse_gate",
    "load_gate_strings",
    "PipelineRunner",
]

# --------------------------------------------------------------------------- #
# the gate string
# --------------------------------------------------------------------------- #

#: Verified in .bob/custom_modes.yaml (line 53, orchestrator gate rule; line 255,
#: arch-critic instruction) and .bob/rules-arch-critic/01-review-rubric.md (line
#: 52). These are the defaults; :func:`load_gate_strings` re-reads them from the
#: critic's own rule file at startup so a re-translated harness is detected
#: rather than silently mis-parsed.
GATE_APPROVED: str = "VERDICT: APPROVED"
GATE_BLOCKED: str = "VERDICT: BLOCKED"

_GATE_RULE_FILES: tuple[str, ...] = (
    ".bob/rules-arch-critic/01-review-rubric.md",
    ".bob/custom_modes.yaml",
)

# A gate line is "<LABEL>: <DECISION>" where both halves are word-ish. Anchored
# to a line so prose that merely mentions the words cannot match.
_GATE_LINE_RE = re.compile(r"^([A-Z][A-Z_ ]{2,20})\s*:\s*([A-Z][A-Z_ ]{2,20})$")

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "blocked"})

#: Excerpt of verdict.md carried in the gate event, so the UI can show the
#: decision in context without a second fetch.
_VERDICT_EXCERPT_CHARS = 4000


def load_gate_strings(project_root: Path) -> tuple[str, str]:
    """Read the approved/blocked gate literals out of the critic's rule files.

    The repository was migrated from Portuguese to English and the gate string
    changed with it. Hard-coding the literal would mean a future translation
    silently turns every verdict into ``absent``. So the rule file is the source
    of truth and the module constants are only the fallback.

    Returns ``(approved, blocked)``, falling back to :data:`GATE_APPROVED` /
    :data:`GATE_BLOCKED` for whichever one cannot be found. Never raises: a
    missing ``.bob/`` is the health probe's problem to report, not a reason for
    the pipeline module to fail to import.
    """
    approved: str | None = None
    blocked: str | None = None

    for rel_path in _GATE_RULE_FILES:
        path = project_root / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_line in text.splitlines():
            candidate = _strip_decoration(raw_line)
            match = _GATE_LINE_RE.match(candidate)
            if not match:
                continue
            label, decision = match.group(1).strip(), match.group(2).strip()
            normalized = f"{label}: {decision}"
            # The two decisions differ; whichever appears first for each label
            # wins, mirroring the order they are documented in.
            if approved is None and decision in _APPROVED_WORDS:
                approved = normalized
            elif blocked is None and decision in _BLOCKED_WORDS:
                blocked = normalized
        if approved and blocked:
            break

    return approved or GATE_APPROVED, blocked or GATE_BLOCKED


_APPROVED_WORDS = frozenset({"APPROVED", "APPROVE", "APROVADO", "OK", "PASS"})
_BLOCKED_WORDS = frozenset({"BLOCKED", "BLOCK", "BLOQUEADO", "REJECTED", "FAIL"})


def _strip_decoration(line: str) -> str:
    """Remove markdown decoration so a fenced or bolded gate line still matches."""
    text = line.strip()
    text = text.strip("`")
    text = re.sub(r"^[#>\-\*\s]+", "", text)
    text = re.sub(r"\*+$", "", text).strip()
    text = text.strip("`").strip()
    return text


# --------------------------------------------------------------------------- #
# stage table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StageSpec:
    """One stage of one mode.

    ``slug`` is the Bob chat mode; ``None`` means the stage runs in-process
    (the Mode A vision stages spawn no Bob and spend no Bobcoin).
    ``artifact_template`` is relative to the Bob working directory and is
    formatted with ``run_id``.
    """

    id: StageId
    index: int
    title: str
    slug: str | None
    approval_mode: str | None
    artifact_template: str | None
    is_gate: bool
    mode: RunMode

    @property
    def spawns_bob(self) -> bool:
        return self.slug is not None


PIPELINE_STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        id="intake",
        index=1,
        title="Intake — read the drawing",
        slug="arch-intake",
        approval_mode=None,
        artifact_template=".arch/intake/{run_id}/extraction.json",
        is_gate=False,
        mode="pipeline",
    ),
    StageSpec(
        id="analyst",
        index=2,
        title="Contextualization — build the AIR",
        slug="arch-analyst",
        approval_mode=None,
        artifact_template=".arch/air/{run_id}/air.json",
        is_gate=False,
        mode="pipeline",
    ),
    StageSpec(
        id="critic",
        index=3,
        title="Critique — adversarial gate",
        slug="arch-critic",
        approval_mode=None,
        artifact_template=".arch/review/{run_id}/verdict.md",
        is_gate=True,
        mode="pipeline",
    ),
    StageSpec(
        id="scaffold",
        index=4,
        title="Scaffolding — generate the code",
        slug="arch-scaffold",
        approval_mode=None,
        artifact_template=".arch/build/{run_id}/manifest.json",
        is_gate=False,
        mode="pipeline",
    ),
    StageSpec(
        id="validator",
        index=5,
        title="Validation — test the hypotheses",
        slug="arch-validator",
        approval_mode=None,
        artifact_template=".arch/run/{run_id}/validation.md",
        is_gate=False,
        mode="pipeline",
    ),
)

VISION_STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        id="capture",
        index=1,
        title="Capture — normalize the image",
        slug=None,
        approval_mode=None,
        artifact_template=None,
        is_gate=False,
        mode="vision",
    ),
    StageSpec(
        id="extract",
        index=2,
        title="Extract — read the architecture",
        slug=None,
        approval_mode=None,
        artifact_template=None,
        is_gate=False,
        mode="vision",
    ),
)


def stages_for(mode: RunMode) -> tuple[StageSpec, ...]:
    """The stage plan for a mode, in execution order."""
    return VISION_STAGES if mode == "vision" else PIPELINE_STAGES


def stage_by_id(stage_id: StageId) -> StageSpec:
    """Look up a stage spec by id.

    Raises:
        KeyError: for an unknown stage id.
    """
    for spec in (*PIPELINE_STAGES, *VISION_STAGES):
        if spec.id == stage_id:
            return spec
    raise KeyError(f"Unknown stage id: {stage_id!r}")


def artifact_path_for(spec: StageSpec, run_id: str, root: Path) -> Path | None:
    """Absolute path of the artifact a stage is contracted to write."""
    if not spec.artifact_template:
        return None
    return Path(root) / spec.artifact_template.format(run_id=run_id)


# --------------------------------------------------------------------------- #
# gate parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GateReading:
    """The machine's reading of ``verdict.md``.

    ``verdict="absent"`` is a first-class outcome, not an error and definitely
    not an approval: ``.arch/README.md`` records that neither historical run's
    ``verdict.md`` contains the gate string at all -- both expressed the
    decision in prose and stage 4 ran anyway, meaning the gate was satisfied by
    a person rather than by the mechanism. The UI must present ``absent`` as a
    defect of the run and force a human decision.
    """

    verdict: GateVerdict
    gate_line: str | None
    matched: str | None
    excerpt: str


def parse_gate(
    verdict_text: str,
    *,
    approved: str = GATE_APPROVED,
    blocked: str = GATE_BLOCKED,
) -> GateReading:
    """Read the gate decision out of ``verdict.md``.

    The contract is that the **last non-empty line** is exactly the gate string.
    That is checked first. A secondary, case-insensitive scan then looks for a
    ``VERDICT:`` line anywhere in the file, from the bottom up, to catch a model
    that appended a stray closing sentence after an otherwise correct verdict.

    Anything else is ``absent``. The verdict is never inferred from prose: a
    document that argues at length for approval and forgets the line has not
    approved anything.
    """
    text = verdict_text or ""
    excerpt = _excerpt(text)

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return GateReading(verdict="absent", gate_line=None, matched=None, excerpt=excerpt)

    last = _strip_decoration(lines[-1])
    if last == approved:
        return GateReading("approved", last, approved, excerpt)
    if last == blocked:
        return GateReading("blocked", last, blocked, excerpt)

    # Secondary pass, bottom up, case-insensitive.
    approved_ci, blocked_ci = approved.casefold(), blocked.casefold()
    for raw in reversed(lines):
        candidate = _strip_decoration(raw)
        folded = candidate.casefold()
        if approved_ci in folded:
            return GateReading("approved", candidate, approved, excerpt)
        if blocked_ci in folded:
            return GateReading("blocked", candidate, blocked, excerpt)

    return GateReading(verdict="absent", gate_line=None, matched=None, excerpt=excerpt)


def _excerpt(text: str) -> str:
    if len(text) <= _VERDICT_EXCERPT_CHARS:
        return text
    return "…\n" + text[-_VERDICT_EXCERPT_CHARS:]


# --------------------------------------------------------------------------- #
# the one retry
# --------------------------------------------------------------------------- #

#: Attempts allowed per stage. Two means exactly one retry — never a loop.
MAX_STAGE_ATTEMPTS: int = 2

#: Pause between the two attempts. Deliberately short: the failure this exists
#: for is a 504 from IBM's API-key validator, which clears within seconds or
#: does not clear at all, so a long back-off only delays the same answer while
#: holding the single pipeline slot.
RETRY_BACKOFF_S: float = 3.0


@dataclass(frozen=True)
class RetryDecision:
    """Whether to run a failed stage a second time, and the sentence saying why.

    ``reason`` is emitted verbatim on the timeline. A retry that cannot explain
    itself is indistinguishable from the run silently doing twice the work.
    """

    retry: bool
    reason: str
    marker: str | None = None
    verdict: PreflightVerdict | None = None


def should_retry_stage(
    *, result: ProcResult, attempt: int, cancelled: bool = False
) -> RetryDecision:
    """Decide whether a stage that just failed gets its one repeat.

    Every refusal below is a rule that cannot be relaxed without making the
    retry harmful rather than useful:

    * **at most one retry.** ``attempt >= MAX_STAGE_ATTEMPTS`` ends it. There is
      no loop and no exponential back-off to tune.
    * **never after cancellation.** The user asked for the process to stop.
    * **never on a stall.** ``timed_out`` means the watchdog killed a stage that
      stopped talking. By the time that fires, the stage has streamed minutes of
      reasoning and been billed for it; repeating from the start pays for all of
      it again, and the fallback AIR already keeps the run alive. A retry is for
      a fast pre-flight failure, and nothing else.
    * **never after exit 0.** Exit 0 with a missing artifact is a different
      failure (see ``_classify_stage_failure``); re-running it would spend a
      second inference on a stage that already ran to completion.
    * **never once output exists.** Any NDJSON line means inference was billed
      and the stage may have half-written its artifact. Repeating from the start
      would pay again and could overwrite what is already on disk.

    Only then does :func:`app.errors.classify_preflight_failure` get a say, and
    only a transient verdict retries.
    """
    if attempt >= MAX_STAGE_ATTEMPTS:
        return RetryDecision(
            retry=False,
            reason=(
                f"Attempt {attempt} of {MAX_STAGE_ATTEMPTS}: the stage has "
                "already been retried once and is not repeated again."
            ),
        )
    if cancelled or result.cancelled:
        return RetryDecision(
            retry=False, reason="The run was cancelled; nothing is retried."
        )
    if result.timed_out:
        return RetryDecision(
            retry=False,
            reason=(
                "The stage was killed for going silent, not for failing fast. "
                "A stall is not a pre-flight failure and repeating it spends "
                "the same budget again."
            ),
        )
    if result.exit_code == 0:
        return RetryDecision(
            retry=False,
            reason=(
                "The process exited 0, so whatever failed happened after Bob "
                "ran. Only a pre-flight failure is retried."
            ),
        )
    if result.stdout_lines > 0:
        return RetryDecision(
            retry=False,
            reason=(
                f"The stage already emitted {result.stdout_lines} NDJSON "
                "line(s), so inference was billed and its artifact may be "
                "partly written. Re-running would pay twice and could "
                "overwrite it."
            ),
        )

    verdict = classify_preflight_failure(result.stderr_text)
    if not verdict.transient:
        return RetryDecision(
            retry=False, reason=verdict.reason, marker=verdict.marker, verdict=verdict
        )
    return RetryDecision(
        retry=True, reason=verdict.reason, marker=verdict.marker, verdict=verdict
    )


# --------------------------------------------------------------------------- #
# the runner
# --------------------------------------------------------------------------- #


class PipelineRunner:
    """Drives runs: start, resume after the gate, cancel.

    One asyncio task per active run, one Bob subprocess at a time by policy.
    All coordination is in-process (a per-run ``asyncio.Lock`` inside
    :class:`~app.store.RunStore`), which is why the app must run with a single
    uvicorn worker -- a second worker would not see the first one's locks.
    """

    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        health: HealthCache,
        vision: ArchVisionClient,
    ) -> None:
        self._settings = settings
        self._store = store
        self._health = health
        self._vision = vision
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._gate_strings = load_gate_strings(settings.project_root)

    # -- public API -------------------------------------------------------- #

    @property
    def gate_strings(self) -> tuple[str, str]:
        """``(approved, blocked)`` as read from the critic's rule files."""
        return self._gate_strings

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def start(self, run_id: str) -> None:
        """Validate, reserve the concurrency slot and spawn the run task."""
        state = self._store.load(run_id)

        if state.status != "created":
            raise Conflict(
                code="run_not_startable",
                title="This run has already been started",
                detail=(
                    f"Run {run_id} is in state {state.status!r}; only a run in state "
                    "'created' can be started."
                ),
                remedy=(
                    "Create a new run for this upload, or resume this one at the gate "
                    "if it is waiting for a decision."
                ),
                run_id=run_id,
                status=state.status,
            )

        # A failing health probe blocks the mode it affects, and only that mode:
        # a broken Bob install still leaves the whole vision preview usable.
        self._health.assert_allows(state.mode)

        if state.mode == "pipeline":
            active = self._active_pipeline_run_ids()
            if len(active) >= max(1, self._settings.max_concurrent_pipeline_runs):
                raise Conflict(
                    code="pipeline_busy",
                    title="Another pipeline run is already active",
                    detail=(
                        "This tool runs one Bob subprocess at a time by policy. "
                        f"Active: {', '.join(active)}."
                    ),
                    remedy=(
                        "Wait for the active run to reach the gate or finish, or cancel "
                        "it with POST /api/runs/{id}/cancel."
                    ),
                    active_runs=active,
                )

        # The baseline for "what did this run write". Taken before the first
        # subprocess exists, because after that there is no way to tell the
        # scaffold's output from what was already on disk. See app/projectdiff.py
        # for why the manifest alone is not enough.
        self._record_baseline(run_id)

        await self._store.update(run_id, lambda s: _mark_running(s))
        self._spawn(run_id, from_index=0, resumed_from=None)

    async def resume(self, run_id: str, decision: GateDecision) -> None:
        """Apply the human gate decision and enqueue the next leg of the run."""
        state = self._store.load(run_id)
        if state.status != "awaiting_input":
            raise Conflict(
                code="run_not_awaiting_input",
                title="This run is not waiting for a gate decision",
                detail=f"Run {run_id} is in state {state.status!r}, not 'awaiting_input'.",
                remedy="Reload the run: the decision may already have been recorded.",
                run_id=run_id,
                status=state.status,
            )

        gate = state.gate or GateState(
            verdict="absent", gate_line=None, verdict_artifact_id=None
        )
        override = _is_override(gate.verdict, decision.decision)

        if override and not (decision.reason or "").strip():
            raise AppError(
                code="gate_override_needs_reason",
                title="An override has to be justified",
                detail=(
                    f"The machine read the verdict as {gate.verdict!r} and you chose "
                    f"{decision.decision!r}. That contradiction is recorded in the audit "
                    "trail, so it needs a reason."
                ),
                remedy="Re-submit the decision with a `reason` explaining the override.",
                status=400,
                verdict=gate.verdict,
                decision=decision.decision,
            )

        resume_from: StageId | None
        if decision.decision == "approve":
            resume_from = "scaffold"
        elif decision.decision == "send_back":
            resume_from = decision.resume_from or "analyst"
            if resume_from not in ("analyst", "critic"):
                raise AppError(
                    code="gate_bad_resume_stage",
                    title="A run can only be sent back to the analyst or the critic",
                    detail=f"resume_from={resume_from!r} is not a stage that can be re-run here.",
                    remedy="Use resume_from='analyst' (default) or 'critic'.",
                    status=400,
                )
        else:  # block
            resume_from = None

        decided_at = datetime.now(timezone.utc)
        self._write_gate_decision(
            run_id,
            gate=gate,
            decision=decision,
            override=override,
            resume_from=resume_from,
            decided_at=decided_at,
        )

        def _apply(s: RunState) -> None:
            if s.gate is not None:
                s.gate.decided = True
                s.gate.decision = decision.decision
                s.gate.override = override
                s.gate.reason = decision.reason
                s.gate.resume_from = resume_from
                s.gate.decided_at = decided_at
            s.updated_at = decided_at

        await self._store.update(run_id, _apply)

        if decision.decision == "block":
            await self._emit(
                run_id,
                "run.blocked",
                {
                    "stage": "critic",
                    "reason": decision.reason,
                    "gate_line": gate.gate_line,
                },
                stage="critic",
            )
            await self._terminate(run_id, "blocked")
            return

        await self._emit(
            run_id,
            "run.resumed",
            {
                "decision": decision.decision,
                "override": override,
                "reason": decision.reason,
                "resume_from": resume_from,
                "decided_at": decided_at.isoformat(),
            },
            stage="critic",
        )

        assert resume_from is not None
        spec = stage_by_id(resume_from)
        await self._store.update(run_id, lambda s: _mark_running(s, reset_from=spec.index))
        self._spawn(run_id, from_index=spec.index - 1, resumed_from=resume_from)

    async def cancel(self, run_id: str) -> None:
        """Signal cancellation, tear the subprocess down, mark the run cancelled.

        Safe in any state and a no-op on a terminal run.
        """
        state = self._store.load(run_id)
        if state.status in _TERMINAL_STATUSES:
            return

        event = self._cancels.get(run_id)
        if event is not None:
            event.set()

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            # Give the driver its own SIGTERM -> SIGKILL escalation window
            # before resorting to cancelling the task from outside.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_CANCEL_GRACE_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:  # noqa: BLE001 - the task's own failure path handles it
                pass

        await self._mark_cancelled(run_id, stage=None, signal=None)

    async def shutdown(self) -> None:
        """Cancel every task and close every event log, on server shutdown."""
        run_ids = list(self._tasks)
        for run_id in run_ids:
            event = self._cancels.get(run_id)
            if event is not None:
                event.set()
        for run_id in run_ids:
            task = self._tasks.get(run_id)
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for run_id in run_ids:
            with contextlib.suppress(Exception):
                self._store.eventlog(run_id).close()
        self._tasks.clear()
        self._cancels.clear()

    # -- the stage loop ---------------------------------------------------- #

    def _spawn(self, run_id: str, from_index: int, resumed_from: StageId | None) -> None:
        cancel = asyncio.Event()
        self._cancels[run_id] = cancel
        task = asyncio.create_task(
            self._execute(run_id, from_index, resumed_from=resumed_from),
            name=f"arch2code-run:{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._on_task_done(run_id, t))

    def _on_task_done(self, run_id: str, task: "asyncio.Task[None]") -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
            self._cancels.pop(run_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # The loop's own handlers cover every expected failure, so anything
            # arriving here is a bug in the runner. Never swallow it silently.
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self._report_crash(run_id, exc))
            )

    async def _report_crash(self, run_id: str, exc: BaseException) -> None:
        error = ErrorBody(
            code="runner_crashed",
            title="The pipeline runner itself failed",
            detail=f"{type(exc).__name__}: {exc}",
            remedy=(
                "This is a defect in the webapp, not in Bob. Check the uvicorn console "
                "for the traceback and report it with the run id."
            ),
            context={"run_id": run_id},
        )
        with contextlib.suppress(Exception):
            await self._fail(run_id, stage=None, error=error)

    async def _execute(
        self, run_id: str, from_index: int, *, resumed_from: StageId | None
    ) -> None:
        state = self._store.load(run_id)
        specs = stages_for(state.mode)
        cancel = self._cancels.get(run_id) or asyncio.Event()
        current: StageSpec | None = None

        await self._emit(
            run_id,
            "run.started",
            {
                "run_id": run_id,
                "mode": state.mode,
                "bob_cwd": str(self._settings.bob_cwd),
                "project_root": str(self._settings.project_root),
                "use_pty": self._use_pty(state),
                "resumed_from": resumed_from,
            },
        )

        try:
            for spec in specs[from_index:]:
                current = spec
                if cancel.is_set():
                    await self._mark_cancelled(run_id, stage=spec.id, signal=None)
                    return

                stage_state = await self._run_stage(run_id, spec, cancel)

                if stage_state.status != "succeeded":
                    if cancel.is_set():
                        await self._mark_cancelled(
                            run_id, stage=spec.id, signal="SIGTERM"
                        )
                        return
                    # Degraded continuation. Exactly one stage has an output
                    # that can be reconstructed without a model — the analyst's
                    # AIR, from the intake's extraction.json — and this is the
                    # only place that is allowed to happen. The stage stays
                    # `failed`, keeps its own error, and gains an event saying
                    # what was written instead. See app/air_fallback.py.
                    if not await self._apply_air_fallback(run_id, spec, stage_state):
                        await self._fail(
                            run_id,
                            stage=spec.id,
                            error=stage_state.error
                            or ErrorBody(
                                code="stage_failed",
                                title=f"Stage {spec.id} failed",
                                detail="The stage did not complete successfully.",
                                remedy=(
                                    f"Open the stage detail for {spec.id} to see the exact "
                                    "command line and the captured stderr."
                                ),
                            ),
                        )
                        return

                if spec.is_gate:
                    await self._park_at_gate(run_id, spec)
                    return

            await self._finish(run_id)

        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._mark_cancelled(
                    run_id, stage=current.id if current else None, signal="SIGKILL"
                )
            raise
        except AppError as exc:
            await self._fail(
                run_id, stage=current.id if current else None, error=exc.to_body()
            )
        except Exception as exc:  # noqa: BLE001 - no failure reaches the UI bare
            await self._fail(
                run_id,
                stage=current.id if current else None,
                error=ErrorBody(
                    code="runner_error",
                    title="The run stopped on an unexpected error",
                    detail=f"{type(exc).__name__}: {exc}",
                    remedy=(
                        "Check the uvicorn console for the traceback. The artifacts "
                        "written so far are still on disk."
                    ),
                    context={"stage": current.id if current else None},
                ),
            )

    async def _run_stage(
        self, run_id: str, spec: StageSpec, cancel: asyncio.Event
    ) -> StageState:
        if spec.spawns_bob:
            return await self._run_bob_stage(run_id, spec, cancel)
        return await self._run_vision_stage(run_id, spec, cancel)

    # -- Bob-backed stages ------------------------------------------------- #

    async def _run_bob_stage(
        self, run_id: str, spec: StageSpec, cancel: asyncio.Event
    ) -> StageState:
        state = self._store.load(run_id)
        settings = self._settings
        assert spec.slug is not None

        ctx = self._prompt_context(state)
        prompt = prompts_mod.build_prompt(spec, ctx)
        # bobcli.APPROVAL_BY_SLUG is the ONLY approval policy. Every
        # StageSpec carries approval_mode=None on purpose: this table used to
        # hard-code its own copy, the two drifted, and the copy won silently —
        # so a fix applied to the policy had no effect on any run. An override
        # here remains possible for a one-off, and is visible as a non-None.
        approval = spec.approval_mode or approval_for_slug(spec.slug)
        include_dirs = self._include_directories(run_id)

        argv = build_argv(
            settings,
            chat_mode=spec.slug,
            prompt=prompt,
            approval_mode=approval,
            output_format="stream-json",
            include_directories=include_dirs,
            max_coins=state.options.max_coins,
        )
        timeout_s = state.options.stage_timeout_s or settings.stage_timeout_s
        use_pty = self._use_pty(state)
        stage_dir = self._store.stage_dir(run_id, spec.id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        # Create the stage's artifact directory before Bob starts.
        #
        # Bob orients itself by listing the directory it has been told to write
        # into. Until the first write that directory does not exist, so the
        # listing fails with ENOENT, Bob retries somewhere else, and the run
        # collects three red `list_files` rows before doing anything useful.
        # Nothing is broken — but the first thing a new user sees is a column of
        # errors, and they conclude the product does not work.
        #
        # An empty directory answers the question Bob is actually asking, so the
        # error cannot occur. This is a real fix rather than hiding the rows: a
        # failed tool call belongs on screen, which is exactly why none of these
        # should be failing.
        artifact_dir = artifact_path_for(spec, run_id, settings.bob_cwd)
        if artifact_dir is not None:
            artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        stdout_sink = stage_dir / "stdout.ndjson"
        stderr_sink = stage_dir / "stderr.txt"
        env = subprocess_env(settings)

        _write_json(
            stage_dir / "argv.json",
            {
                "argv": redact_argv(argv),
                "argv_full": list(argv),
                "prompt": prompt,
                "cwd": str(settings.bob_cwd),
                "env_keys": sorted(env),
                "approval_mode": approval,
                "chat_mode": spec.slug,
                "use_pty": use_pty,
                "timeout_s": timeout_s,
                "max_attempts": MAX_STAGE_ATTEMPTS,
            },
        )

        started_at = datetime.now(timezone.utc)
        await self._set_stage(
            run_id,
            spec.id,
            status="running",
            started_at=started_at,
            approval_mode=approval,
            used_pty=use_pty,
        )
        await self._emit(
            run_id,
            "run.stage.started",
            {
                "stage": spec.id,
                "index": spec.index,
                "slug": spec.slug,
                "title": spec.title,
                "approval_mode": approval,
                "argv": redact_argv(argv),
                "cwd": str(settings.bob_cwd),
                "timeout_s": timeout_s,
                "strategy": "pty" if use_pty else "pipe",
                "max_attempts": MAX_STAGE_ATTEMPTS,
            },
            stage=spec.id,
        )

        stats_holder: dict[str, StageStats] = {}
        stderr_total = {"bytes": 0}
        # Bob streams the model's reasoning one token per line. Merging those
        # back into readable blocks HERE, before the event log, is what makes
        # the trail auditable: the file, the replay endpoint, the SSE stream and
        # every client get the same few dozen readable events instead of the
        # same few thousand fragments. The raw lines travel inside the merged
        # event, so nothing is lost to the "Everything" view.
        coalescer = MessageCoalescer()

        async def on_event(ev: StreamEvent) -> None:
            name, data = to_event_payload(ev)
            if ev.type == "result":
                stats_holder["stats"] = extract_stats(ev.payload)
            for out_name, out_data in coalescer.feed(name, data):
                await self._emit(run_id, out_name, out_data, stage=spec.id)

        async def on_stderr(chunk: str) -> None:
            stderr_total["bytes"] += len(chunk.encode("utf-8", errors="replace"))
            await self._emit(
                run_id,
                "bob.stderr",
                {
                    "stage": spec.id,
                    "chunk": chunk,
                    "cumulative_bytes": stderr_total["bytes"],
                },
                stage=spec.id,
            )

        # One retry, at most, and only for a fast pre-flight failure that
        # produced nothing. See should_retry_stage() for every rule this loop
        # obeys; the loop itself cannot run more than MAX_STAGE_ATTEMPTS times.
        attempt = 1
        while True:
            result = await run_bob(
                argv,
                cwd=settings.bob_cwd,
                env=env,
                on_event=on_event,
                on_stderr=on_stderr,
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
                use_pty=use_pty,
                timeout_s=timeout_s,
                cancel=cancel,
            )

            # Whatever the model was still saying when the process ended.
            # Without this the last block of every stage — often the conclusion
            # — would be left in the buffer and never written.
            for out_name, out_data in coalescer.flush():
                await self._emit(run_id, out_name, out_data, stage=spec.id)

            await self._emit(
                run_id,
                "proc.exit",
                {
                    "stage": spec.id,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "used_pty": result.used_pty,
                    "attempt": attempt,
                },
                stage=spec.id,
            )

            decision = should_retry_stage(
                result=result, attempt=attempt, cancelled=cancel.is_set()
            )
            if not decision.retry:
                break

            # Say it out loud. A stage that needed two attempts is an
            # operational fact about this run and about the upstream service;
            # hiding it behind a green stage would be the same lie as reporting
            # a fallback as a success.
            await self._emit(
                run_id,
                "run.stage.retry",
                {
                    "stage": spec.id,
                    "attempt": attempt,
                    "next_attempt": attempt + 1,
                    "max_attempts": MAX_STAGE_ATTEMPTS,
                    "classification": "transient",
                    "marker": decision.marker,
                    "reason": decision.reason,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "stdout_lines": result.stdout_lines,
                    "delay_s": RETRY_BACKOFF_S,
                    "stderr_tail": _tail(result.stderr_text, 2000),
                },
                stage=spec.id,
            )

            # Cancel-aware pause: waiting out the back-off would otherwise make
            # a cancel during the retry window feel like a hang.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(cancel.wait(), timeout=RETRY_BACKOFF_S)
            if cancel.is_set():
                break

            attempt += 1
            # Fresh per attempt: the first attempt's buffer has already been
            # flushed, and its stats belong to a process that no longer exists.
            coalescer = MessageCoalescer()
            stats_holder.clear()

        if should_suggest_pty(result):
            await self._emit(
                run_id,
                "bob.empty_output",
                {
                    "stage": spec.id,
                    "exit_code": 0,
                    "stderr_bytes": stderr_total["bytes"],
                    "used_pty": result.used_pty,
                    "remedy": PTY_REMEDY,
                },
                stage=spec.id,
            )

        found = await self._collect_artifacts(run_id, spec)
        expected_path = artifact_path_for(spec, run_id, settings.bob_cwd)
        artifact_ok = expected_path is None or expected_path.exists()

        if expected_path is not None and not artifact_ok:
            await self._emit(
                run_id,
                "artifact.missing",
                {
                    "stage": spec.id,
                    "expected_path": str(expected_path),
                    "remedy": _missing_artifact_remedy(spec, approval),
                },
                stage=spec.id,
            )

        error = self._classify_stage_failure(
            spec=spec,
            result=result,
            argv=argv,
            expected_path=expected_path,
            artifact_ok=artifact_ok,
            approval=approval,
            cancelled=cancel.is_set(),
        )
        status = "succeeded" if error is None else "failed"
        stats = stats_holder.get("stats")

        _write_json(
            stage_dir / "result.json",
            {
                "stage": spec.id,
                "status": status,
                "attempts": attempt,
                "max_attempts": MAX_STAGE_ATTEMPTS,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "stdout_bytes": result.stdout_bytes,
                "stdout_lines": result.stdout_lines,
                "empty_stdout": result.empty_stdout,
                "used_pty": result.used_pty,
                "timed_out": result.timed_out,
                "signal_sent": result.signal_sent,
                "stats": stats.model_dump(mode="json") if stats else None,
            },
        )

        finished_at = datetime.now(timezone.utc)
        stage_state = await self._set_stage(
            run_id,
            spec.id,
            status=status,
            finished_at=finished_at,
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
            used_pty=result.used_pty,
            empty_stdout=result.empty_stdout,
            stdout_lines=result.stdout_lines,
            stderr_tail=_tail(result.stderr_text),
            artifacts=found,
            stats=stats,
            error=error,
            attempts=attempt,
            add_totals=stats,
        )

        await self._emit(
            run_id,
            "run.stage.finished",
            {
                "stage": spec.id,
                "status": status,
                "attempts": attempt,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "stdout_lines": result.stdout_lines,
                "stdout_bytes": result.stdout_bytes,
                "empty_stdout": result.empty_stdout,
                "used_pty": result.used_pty,
                "stats": stats.model_dump(mode="json") if stats else None,
                "artifacts": [a.model_dump(mode="json") for a in found],
                "error": error.model_dump(mode="json") if error else None,
            },
            stage=spec.id,
        )
        return stage_state

    def _classify_stage_failure(
        self,
        *,
        spec: StageSpec,
        result: ProcResult,
        argv: Sequence[str],
        expected_path: Path | None,
        artifact_ok: bool,
        approval: str,
        cancelled: bool,
    ) -> ErrorBody | None:
        """Decide a stage's fate from the exit code and the artifact on disk.

        Nothing read out of the NDJSON participates in this decision. A
        ``type: "error"`` line is a symptom; the exit code is the verdict.
        """
        if cancelled or result.cancelled:
            return ErrorBody(
                code="cancelled",
                title="The stage was cancelled",
                detail=(
                    f"Stage {spec.id} was terminated on request "
                    f"({result.signal_sent or 'SIGTERM'})."
                ),
                remedy="Start a new run when you are ready.",
                context={"stage": spec.id},
            )

        if result.timed_out:
            # Name the likely cause first. A stage that stops emitting is almost
            # never a slow model: Bob streams reasoning deltas continuously while
            # it works. The failure that looks exactly like this is an exhausted
            # account budget — the backend stops answering with no error, no
            # stderr and no exit, and the stage sits until it is killed. Observed
            # with budget_spend at 93.74 of max_budget 100, where intake finished
            # and the analyst never emitted a second line.
            return ErrorBody(
                code="stage_timeout",
                title=f"Stage {spec.id} stopped producing output",
                detail=(
                    f"The subprocess ran for {result.duration_ms} ms without "
                    f"finishing and was terminated ({result.signal_sent or 'SIGKILL'}). "
                    f"It emitted {result.stdout_lines} line(s) of output."
                ),
                remedy=(
                    "Silence is not proof that Bob stopped working: it emits no "
                    "output at all while the model generates a tool call's "
                    "arguments, and for the analyst that argument is the whole "
                    "AIR (measured: 48-73 s of silence for a 12-20 kB artifact). "
                    "Re-running usually succeeds. If this stage stalls "
                    "repeatedly, raise ARCH2CODE_STAGE_TIMEOUT_S or the run's "
                    "options.stage_timeout_s rather than chasing the account "
                    "budget, which the result line of the previous stage already "
                    "reports as budget_spend against max_budget."
                ),
                context={
                    "stage": spec.id,
                    "duration_ms": result.duration_ms,
                    "stdout_lines": result.stdout_lines,
                },
            )

        if result.exit_code != 0:
            app_error = bob_preflight_error(result.exit_code, result.stderr_text, argv)
            return app_error.to_body()

        if expected_path is not None and not artifact_ok:
            return ErrorBody(
                code="artifact_missing",
                title=f"Stage {spec.id} exited 0 but wrote nothing",
                detail=(
                    f"The stage was contracted to produce {expected_path}, and that file "
                    "does not exist. Exit code 0 with no artifact is a silent failure, "
                    "so the stage is reported as failed."
                ),
                remedy=_missing_artifact_remedy(spec, approval),
                context={"stage": spec.id, "expected_path": str(expected_path)},
            )

        return None

    # -- degraded continuation: the derived AIR ----------------------------- #

    async def _apply_air_fallback(
        self, run_id: str, spec: StageSpec, stage_state: StageState
    ) -> bool:
        """Write a mechanically derived AIR so a dead analyst does not end the run.

        Returns True when the run may continue to stage 3.

        The failure this exists for leaves the run holding a perfectly good
        extraction.json and nothing downstream: the analyst goes quiet with no
        error, no stderr and no exit, and the stall watchdog kills it. The cause
        is not an exhausted account budget — that theory was measured and is
        false; see app.bobproc.STALL_BEFORE_FIRST_LINE_S for what the silence
        actually is. Everything stage 2 *derives* from the extraction is a pure
        transform; what it *adds* — assumptions with a declared impact,
        falsifiable hypotheses — is not, and is deliberately left empty by
        app.air_fallback.

        Three refusals, each of which returns False and lets the run fail as before:

        * any stage other than the analyst. Nothing else in the pipeline has a
          deterministic substitute, and inventing one would be fabricating output.
        * no extraction.json. There is then nothing to transform, and a fallback
          AIR would be pure invention.
        * an air.json already on disk. The analyst may have written a real AIR and
          then died; overwriting it with a degraded one would destroy the only
          reasoned artifact the run has.

        The stage is NOT marked succeeded. It keeps status "failed" and gains an
        error whose code is ``analyst_fallback_applied``, so the UI shows a
        degraded stage rather than a green one, and the AIR itself carries a
        blocking unknown that the stage-3 gate is expected to reject.
        """
        if spec.id != "analyst":
            return False

        settings = self._settings
        state = self._store.load(run_id)
        extraction_path = settings.bob_cwd / f".arch/intake/{run_id}/extraction.json"
        air_path = artifact_path_for(spec, run_id, settings.bob_cwd)
        if air_path is None:
            return False

        reason = self._fallback_refusal(extraction_path, air_path)
        if reason is not None:
            await self._emit(
                run_id,
                "run.stage.fallback_unavailable",
                {
                    "stage": spec.id,
                    "extraction_path": str(extraction_path),
                    "air_path": str(air_path),
                    "reason": reason,
                },
                stage=spec.id,
            )
            return False

        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
            if not isinstance(extraction, dict):
                raise ValueError("extraction.json is not a JSON object")
            air = air_fallback.build_fallback_air(
                extraction,
                run_id=run_id,
                source_kind=state.source_kind,
                source_artifact=str(self._source_path(state)),
                reason=(stage_state.error.title if stage_state.error else None),
            )
            _write_json(air_path, air)
        except Exception as exc:  # noqa: BLE001 - a broken fallback must not mask the real failure
            await self._emit(
                run_id,
                "run.stage.fallback_unavailable",
                {
                    "stage": spec.id,
                    "extraction_path": str(extraction_path),
                    "air_path": str(air_path),
                    "reason": f"{type(exc).__name__}: {exc}",
                },
                stage=spec.id,
            )
            return False

        error = ErrorBody(
            code="analyst_fallback_applied",
            title="Contextualization degraded — the AIR was derived, not reasoned",
            detail=(
                f"Stage {spec.id} did not produce an AIR "
                f"({stage_state.error.code if stage_state.error else 'unknown failure'}: "
                f"{stage_state.error.title if stage_state.error else 'no error recorded'}). "
                f"{air_path.name} was rebuilt mechanically from {extraction_path.name}: "
                f"{len(air['components'])} component(s), {len(air['connections'])} "
                f"connection(s) and {len(air['unknowns'])} unknown(s) were carried over. "
                "assumptions[] is EMPTY and no impact was assessed, because no model "
                "reasoned about this drawing. The run continues so the critic can judge "
                "what is there."
            ),
            remedy=(
                "Expect the stage-3 gate to BLOCK: the derived AIR carries an open "
                "blocking unknown saying contextualization never completed. Check the "
                "Bob account budget (budget_spend against max_budget on the intake "
                "stage's result line) and send the run back to the analyst once there "
                "is quota left."
            ),
            context={
                "stage": spec.id,
                "original_error_code": (
                    stage_state.error.code if stage_state.error else None
                ),
                "extraction_path": str(extraction_path),
                "air_path": str(air_path),
                "blocking_unknown_id": air_fallback.FALLBACK_UNKNOWN_ID,
            },
        )

        # Re-scan so the derived file appears under "Files written" for the
        # stage that was contracted to produce it, and emits artifact.written
        # like any other artifact. Nothing about it is hidden.
        found = await self._collect_artifacts(run_id, spec)
        await self._set_stage(run_id, spec.id, artifacts=found, error=error)

        await self._emit(
            run_id,
            "run.stage.fallback",
            {
                "stage": spec.id,
                "degraded": True,
                "label": "fallback",
                "extractor": air_fallback.FALLBACK_EXTRACTOR,
                "extraction_path": str(extraction_path),
                "air_path": str(air_path),
                "components": len(air["components"]),
                "connections": len(air["connections"]),
                "unknowns": len(air["unknowns"]),
                "assumptions": len(air["assumptions"]),
                "blocking_unknown_id": air_fallback.FALLBACK_UNKNOWN_ID,
                "overall_confidence": air["meta"]["overall_confidence"],
                "original_error": (
                    stage_state.error.model_dump(mode="json")
                    if stage_state.error
                    else None
                ),
                "error": error.model_dump(mode="json"),
                "artifacts": [a.model_dump(mode="json") for a in found],
                "expect_gate": "blocked",
            },
            stage=spec.id,
        )
        return True

    @staticmethod
    def _fallback_refusal(extraction_path: Path, air_path: Path) -> str | None:
        """Why the derived AIR must not be written, or None when it may be."""
        if not extraction_path.exists():
            return (
                f"{extraction_path} does not exist, so there is nothing to derive an "
                "AIR from. The run fails as it did before."
            )
        if air_path.exists():
            return (
                f"{air_path} already exists. The analyst may have written a real AIR "
                "before failing, and a derived one must never overwrite a reasoned one."
            )
        return None

    # -- vision stages (Mode A) -------------------------------------------- #

    async def _run_vision_stage(
        self, run_id: str, spec: StageSpec, cancel: asyncio.Event
    ) -> StageState:
        """Run one in-process Mode A stage: capture, then extract.

        Neither stage spawns Bob and neither spends Bobcoin, which is why Mode A
        stays usable on a machine whose Bob install is broken.
        """
        started_at = datetime.now(timezone.utc)
        started_mono = time.monotonic()
        await self._set_stage(run_id, spec.id, status="running", started_at=started_at)
        await self._emit(
            run_id,
            "run.stage.started",
            {
                "stage": spec.id,
                "index": spec.index,
                "slug": None,
                "title": spec.title,
                "approval_mode": None,
                "argv": [],
                "cwd": str(self._store.workspace_dir(run_id)),
                "timeout_s": self._settings.vision_timeout_s,
                "strategy": "inproc",
            },
            stage=spec.id,
        )

        error: ErrorBody | None = None
        try:
            if spec.id == "capture":
                await self._do_capture(run_id)
            else:
                await self._do_extract(run_id)
        except VisionToolError as exc:
            body = exc.to_body()
            await self._emit(
                run_id,
                "vision.tool_error",
                {
                    "tool": exc.tool,
                    "message": body.detail,
                    "remedy": body.remedy,
                    "raw": exc.raw,
                },
                stage=spec.id,
            )
            error = body
        except AppError as exc:
            error = exc.to_body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = ErrorBody(
                code="vision_stage_failed",
                title=f"Stage {spec.id} failed",
                detail=f"{type(exc).__name__}: {exc}",
                remedy=(
                    "Check the health panel: the interpreter (ARCH2CODE_PYTHON) and the "
                    "MCP server are the two things this stage depends on."
                ),
                context={"stage": spec.id},
            )

        duration_ms = int((time.monotonic() - started_mono) * 1000)
        found = await self._collect_artifacts(run_id, spec)
        status = "succeeded" if error is None else "failed"
        stage_state = await self._set_stage(
            run_id,
            spec.id,
            status=status,
            finished_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
            artifacts=found,
            error=error,
        )
        await self._emit(
            run_id,
            "run.stage.finished",
            {
                "stage": spec.id,
                "status": status,
                "exit_code": None,
                "duration_ms": duration_ms,
                "stdout_lines": 0,
                "stdout_bytes": 0,
                "empty_stdout": False,
                "used_pty": False,
                "stats": None,
                "artifacts": [a.model_dump(mode="json") for a in found],
                "error": error.model_dump(mode="json") if error else None,
            },
            stage=spec.id,
        )
        return stage_state

    async def _do_capture(self, run_id: str) -> None:
        state = self._store.load(run_id)
        source = self._source_path(state)
        # cwd = the run's own workspace, so capture_diagram.py writes
        # workspace/.arch/intake/<run_id>/ and a vision preview leaves the
        # repository completely untouched.
        workspace = self._store.workspace_dir(run_id)
        workspace.mkdir(parents=True, exist_ok=True)

        await self._emit(
            run_id,
            "vision.capture.started",
            {"source_path": str(source), "run_id": run_id, "cwd": str(workspace)},
            stage="capture",
        )

        manifest, script = await scripts_mod.capture_diagram(
            self._settings, source=source, run_id=run_id, cwd=workspace
        )
        await self._emit_script(run_id, "capture", script)

        normalization = manifest.normalization or {}
        normalized = normalization.get("normalized") or {}
        normalized_path = (
            str((workspace / manifest.normalized_artifact).resolve())
            if manifest.normalized_artifact
            else None
        )
        _write_json(
            self._store.vision_dir(run_id) / "capture-manifest.json",
            manifest.model_dump(mode="json"),
        )
        await self._emit(
            run_id,
            "vision.capture.finished",
            {
                "manifest": manifest.model_dump(mode="json"),
                "normalized_path": normalized_path,
                "width": normalized.get("width"),
                "height": normalized.get("height"),
                "exif_rotation_applied": bool(
                    normalization.get("exif_rotation_applied", False)
                ),
                "scale": normalization.get("scale"),
                "warnings": list(manifest.warnings or []),
                "exit_code": script.exit_code,
            },
            stage="capture",
        )

    async def _do_extract(self, run_id: str) -> None:
        state = self._store.load(run_id)
        vision_dir = self._store.vision_dir(run_id)
        vision_dir.mkdir(parents=True, exist_ok=True)

        image_path = self._normalized_image_path(run_id)
        started = time.monotonic()
        await self._emit(
            run_id,
            "vision.extract.started",
            {
                "tool": "arch_vision_extract_architecture",
                "image_path": str(image_path),
                "source_kind": state.source_kind,
                "hint": state.hint,
            },
            stage="extract",
        )

        extraction = await self._vision.extract_architecture(
            str(image_path), source_kind=state.source_kind, hint=state.hint
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        _write_json(vision_dir / "extraction.json", extraction)
        quality = summarize_quality(extraction)
        _write_json(vision_dir / "quality.json", quality.model_dump(mode="json"))

        provenance = extraction.get("_provenance") or {}
        await self._emit(
            run_id,
            "vision.extract.finished",
            {
                "components": len(extraction.get("components") or []),
                "connections": len(extraction.get("connections") or []),
                "boundaries": len(extraction.get("boundaries") or []),
                "unknowns": len(extraction.get("unknowns") or []),
                "overall_confidence": extraction.get("overall_confidence"),
                "quality": {
                    "broken_refs": quality.broken_refs,
                    "connections_needing_verification": (
                        quality.connections_needing_verification
                    ),
                    "action_required": quality.action_required,
                },
                "model": provenance.get("model"),
                "prompt_version": provenance.get("prompt_version"),
                "duration_ms": duration_ms,
            },
            stage="extract",
        )

    async def _emit_script(
        self, run_id: str, stage: StageId, script: scripts_mod.ScriptResult
    ) -> None:
        await self._emit(run_id, "script.finished", script.as_event(), stage=stage)

    # -- the gate ---------------------------------------------------------- #

    async def _evaluate_gate(self, run_id: str) -> GateReading:
        """Read ``verdict.md`` and parse the gate line.

        A missing file is not an approval and not a crash: it is
        ``absent`` with an explanatory excerpt, which the UI must present as a
        defect of the run.
        """
        approved, blocked = self._gate_strings
        spec = stage_by_id("critic")
        path = artifact_path_for(spec, run_id, self._settings.bob_cwd)
        if path is None or not path.exists():
            return GateReading(
                verdict="absent",
                gate_line=None,
                matched=None,
                excerpt=(
                    f"{path} does not exist. Stage 3 reported success but produced no "
                    "verdict file."
                ),
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return GateReading(
                verdict="absent",
                gate_line=None,
                matched=None,
                excerpt=f"{path} could not be read: {exc}",
            )
        return parse_gate(text, approved=approved, blocked=blocked)

    async def _park_at_gate(self, run_id: str, spec: StageSpec) -> None:
        """Record the gate, emit, and let the task end. Never block on a human."""
        reading = await self._evaluate_gate(run_id)
        verdict_path = artifact_path_for(spec, run_id, self._settings.bob_cwd)
        artifact_id = (
            artifacts_mod.make_artifact_id(verdict_path, run_id)
            if verdict_path is not None
            else None
        )

        await self._emit(
            run_id,
            "gate.evaluated",
            {
                "stage": "critic",
                "verdict": reading.verdict,
                "gate_line": reading.gate_line,
                "matched": reading.matched,
                "artifact_id": artifact_id,
            },
            stage="critic",
        )

        gate = GateState(
            verdict=reading.verdict,
            gate_line=reading.gate_line,
            verdict_artifact_id=artifact_id,
            verdict_excerpt=reading.excerpt,
        )

        def _apply(s: RunState) -> None:
            s.status = "awaiting_input"
            s.gate = gate
            s.updated_at = datetime.now(timezone.utc)

        await self._store.update(run_id, _apply)

        await self._emit(
            run_id,
            "run.awaiting_input",
            {
                "stage": "critic",
                "reason": "gate",
                "gate": {
                    "verdict": reading.verdict,
                    "gate_line": reading.gate_line,
                    "verdict_artifact_id": artifact_id,
                    "verdict_excerpt": reading.excerpt,
                    "findings_count": _count_findings(reading.excerpt),
                    "choices": ["approve", "block", "send_back"],
                    "default_choice": _default_choice(reading.verdict),
                },
            },
            stage="critic",
        )
        # The task ends here. Nothing is parked; the state is entirely on disk.

    def _write_gate_decision(
        self,
        run_id: str,
        *,
        gate: GateState,
        decision: GateDecision,
        override: bool,
        resume_from: StageId | None,
        decided_at: datetime,
    ) -> None:
        path = self._store.gate_path(run_id)
        _write_json(
            path,
            {
                "run_id": run_id,
                "parsed_verdict": gate.verdict,
                "parsed_gate_line": gate.gate_line,
                "decision": decision.decision,
                "override": override,
                "reason": decision.reason,
                "resume_from": resume_from,
                "decided_at": decided_at.isoformat(),
            },
        )

    # -- terminal transitions ---------------------------------------------- #

    async def _finish(self, run_id: str) -> None:
        state = self._store.load(run_id)
        succeeded = sum(1 for st in state.stages if st.status == "succeeded")
        all_artifacts = [a for st in state.stages for a in st.artifacts]
        await self._emit(
            run_id,
            "run.finished",
            {
                "status": "succeeded",
                "stages_succeeded": succeeded,
                "totals": state.totals.model_dump(mode="json"),
                "artifacts": [a.model_dump(mode="json") for a in all_artifacts],
            },
        )
        await self._terminate(run_id, "succeeded")

    async def _fail(
        self, run_id: str, *, stage: StageId | None, error: ErrorBody
    ) -> None:
        await self._emit(
            run_id,
            "run.failed",
            {"stage": stage, "error": error.model_dump(mode="json")},
            stage=stage,
        )

        def _apply(s: RunState) -> None:
            s.error = error
            for st in s.stages:
                if st.status == "running":
                    st.status = "failed"

        await self._store.update(run_id, _apply)
        await self._terminate(run_id, "failed")

    async def _mark_cancelled(
        self, run_id: str, *, stage: StageId | None, signal: str | None
    ) -> None:
        state = self._store.load(run_id)
        if state.status in _TERMINAL_STATUSES:
            return
        await self._emit(
            run_id,
            "run.cancelled",
            {"stage": stage, "signal": signal},
            stage=stage,
        )

        def _apply(s: RunState) -> None:
            for st in s.stages:
                if st.status == "running":
                    st.status = "failed"
                    st.error = ErrorBody(
                        code="cancelled",
                        title="Cancelled",
                        detail="The stage was cancelled before it finished.",
                        remedy="Start a new run when you are ready.",
                    )

        await self._store.update(run_id, _apply)
        await self._terminate(run_id, "cancelled")

    async def _terminate(self, run_id: str, status: str) -> None:
        log = self._store.eventlog(run_id)

        def _apply(s: RunState) -> None:
            s.status = status  # type: ignore[assignment]
            s.updated_at = datetime.now(timezone.utc)
            s.last_event_id = log.last_id()

        await self._store.update(run_id, _apply)
        with contextlib.suppress(Exception):
            log.close()

    # -- helpers ------------------------------------------------------------ #

    async def _emit(
        self,
        run_id: str,
        type: str,
        data: Mapping[str, Any] | None = None,
        *,
        stage: StageId | None = None,
    ) -> None:
        log = self._store.eventlog(run_id)
        await log.aappend(type, dict(data or {}), stage=stage)

    async def _set_stage(
        self,
        run_id: str,
        stage_id: StageId,
        *,
        add_totals: StageStats | None = None,
        **fields: Any,
    ) -> StageState:
        """Patch one stage inside ``run.json`` under the per-run lock."""
        log = self._store.eventlog(run_id)

        def _apply(s: RunState) -> None:
            for st in s.stages:
                if st.id != stage_id:
                    continue
                for key, value in fields.items():
                    if value is not None or key in ("error", "stats"):
                        setattr(st, key, value)
                break
            if add_totals is not None:
                s.totals.tokens_in += add_totals.input_tokens or 0
                s.totals.tokens_out += add_totals.output_tokens or 0
                s.totals.duration_ms += add_totals.duration_ms or 0
                coins = _as_float(add_totals.session_costs)
                if coins is not None:
                    s.totals.coins = (s.totals.coins or 0.0) + coins
            s.last_event_id = log.last_id()
            s.updated_at = datetime.now(timezone.utc)

        state = await self._store.update(run_id, _apply)
        for st in state.stages:
            if st.id == stage_id:
                return st
        raise NotFound(
            code="stage_not_found",
            title="Stage not found in the run",
            detail=f"Run {run_id} has no stage {stage_id!r}.",
            remedy="This indicates run.json was written by an incompatible version.",
        )

    async def _collect_artifacts(
        self, run_id: str, spec: StageSpec
    ) -> list[ArtifactRef]:
        """Resolve what the stage produced and emit one event per file found."""
        try:
            refs = artifacts_mod.scan_stage_outputs(run_id, spec, self._settings.bob_cwd)
        except Exception:  # noqa: BLE001 - artifact scanning never fails a stage
            refs = []
        expected_path = artifact_path_for(spec, run_id, self._settings.bob_cwd)
        expected_str = str(expected_path) if expected_path else None
        for ref in refs:
            if not ref.exists:
                continue
            await self._emit(
                run_id,
                "artifact.written",
                {
                    "stage": spec.id,
                    "artifact": ref.model_dump(mode="json"),
                    "expected": expected_str is not None and ref.path == expected_str,
                },
                stage=spec.id,
            )
        return refs

    def _record_baseline(self, run_id: str) -> None:
        """Snapshot the project tree so the export can prove what the run wrote.

        Best effort by construction: a failure here costs the project export its
        precision (it falls back to the manifest and the contracted
        directories, and the archive's MANIFEST says so) and must never cost a
        user their run.
        """
        try:
            settings = self._settings
            root = Path(settings.bob_cwd)
            excluded = projectdiff.excluded_for(
                root, settings.runs_root, settings.uploads_root
            )
            snapshot = projectdiff.take_snapshot(root, excluded=excluded)
            projectdiff.write_snapshot(
                self._store.run_dir(run_id) / projectdiff.SNAPSHOT_FILENAME, snapshot
            )
        except Exception:  # noqa: BLE001 - a baseline is an optimisation, not a gate
            pass

    def _prompt_context(self, state: RunState) -> PromptContext:
        root = self._settings.bob_cwd
        run_id = state.run_id
        return PromptContext(
            run_id=run_id,
            project_root=root,
            source_path=self._source_path(state),
            source_kind=state.source_kind,
            hint=state.hint,
            intake_path=root / f".arch/intake/{run_id}/extraction.json",
            air_path=root / f".arch/air/{run_id}/air.json",
            verdict_path=root / f".arch/review/{run_id}/verdict.md",
            manifest_path=root / f".arch/build/{run_id}/manifest.json",
            validation_path=root / f".arch/run/{run_id}/validation.md",
            pipeline_log_path=root / f".arch/run/{run_id}/pipeline.md",
            gate_feedback=(state.gate.reason if state.gate else None),
        )

    def _source_path(self, state: RunState) -> Path:
        """The verbatim copy of the upload inside the run directory."""
        input_dir = self._store.input_dir(state.run_id)
        candidate = input_dir / state.upload.filename
        if candidate.exists():
            return candidate.resolve()
        if input_dir.exists():
            for entry in sorted(input_dir.iterdir()):
                if entry.is_file() and not entry.name.startswith("."):
                    return entry.resolve()
        return Path(state.upload.stored_path).resolve()

    def _normalized_image_path(self, run_id: str) -> Path:
        """Absolute path of the capture stage's normalized PNG.

        The bounding boxes are normalized against the image the model actually
        saw, which is this file and never the original upload.
        """
        workspace = self._store.workspace_dir(run_id)
        manifest_path = workspace / ".arch/intake" / run_id / "capture-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                code="capture_manifest_missing",
                title="The capture stage produced no manifest",
                detail=f"{manifest_path} could not be read: {exc}",
                remedy="Re-run the capture stage; check the health panel for Pillow and ARCH2CODE_PYTHON.",
                status=409,
            ) from exc

        normalized = manifest.get("normalized_artifact") or manifest.get("working_copy")
        if not normalized:
            raise AppError(
                code="normalized_image_missing",
                title="The capture stage produced no normalized image",
                detail=(
                    f"{manifest_path} has neither normalized_artifact nor working_copy. "
                    "That happens when the upload took the deterministic route, which "
                    "does not go through vision at all."
                ),
                remedy=(
                    "Use the deterministic path for this artifact (parse_drawio.py) "
                    "instead of the vision preview."
                ),
                status=409,
            )
        path = Path(normalized)
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()

    def _include_directories(self, run_id: str) -> list[Path]:
        """Extra workspace roots for Bob.

        The run's input directory normally lives under the project root and
        needs no inclusion; it only matters when ARCH2CODE_BOB_CWD points at a
        scratch clone, where the uploaded drawing would otherwise be outside
        the workspace entirely.
        """
        input_dir = self._store.input_dir(run_id).resolve()
        try:
            input_dir.relative_to(self._settings.bob_cwd.resolve())
            return []
        except ValueError:
            return [input_dir]

    def _use_pty(self, state: RunState) -> bool:
        if state.options.use_pty is not None:
            return bool(state.options.use_pty)
        return bool(self._settings.bob_pty)

    def _active_pipeline_run_ids(self) -> list[str]:
        active: list[str] = []
        for run_id, task in self._tasks.items():
            if task.done():
                continue
            with contextlib.suppress(Exception):
                if self._store.load(run_id).mode == "pipeline":
                    active.append(run_id)
        return active


# --------------------------------------------------------------------------- #
# module-level helpers
# --------------------------------------------------------------------------- #

#: How long cancel() waits for the driver's own SIGTERM -> SIGKILL escalation
#: before cancelling the task outright.
_CANCEL_GRACE_S = 14.0


def _mark_running(state: RunState, reset_from: int | None = None) -> None:
    state.status = "running"
    state.updated_at = datetime.now(timezone.utc)
    state.error = None
    if reset_from is not None:
        for st in state.stages:
            if st.index >= reset_from:
                st.status = "pending"
                st.started_at = None
                st.finished_at = None
                st.duration_ms = None
                st.exit_code = None
                st.error = None
                st.attempts = 1
                st.artifacts = []


def _is_override(verdict: GateVerdict, decision: str) -> bool:
    """True when the human decision contradicts what the machine read.

    ``absent`` plus ``approve`` counts as an override: approving with no
    machine-readable verdict is exactly the pattern an auditor looks for.
    """
    if verdict == "approved":
        return decision != "approve"
    return decision == "approve"


def _default_choice(verdict: GateVerdict) -> str:
    if verdict == "approved":
        return "approve"
    if verdict == "blocked":
        return "send_back"
    return "send_back"


def _count_findings(excerpt: str) -> int | None:
    """Rough count of finding rows in the verdict, for the gate card badge."""
    if not excerpt:
        return None
    count = 0
    in_findings = False
    for line in excerpt.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("#") and "finding" in lowered:
            in_findings = True
            continue
        if in_findings and stripped.startswith("#"):
            in_findings = False
        if in_findings and (stripped.startswith("- ") or stripped.startswith("* ")):
            count += 1
    return count or None


def _missing_artifact_remedy(spec: StageSpec, approval: str) -> str:
    if spec.id == "scaffold" and approval != "yolo":
        return (
            "arch-scaffold ran under --approval-mode "
            f"{approval}, which excludes write_to_file, so it cannot create a file. "
            "This stage must run with --yolo."
        )
    if spec.id == "scaffold":
        return (
            "arch-scaffold ran with --yolo, so tool exclusion is not the cause. "
            "Check the stage's stderr and the last tool_use events: the model most "
            "likely stopped on the verdict precondition without generating anything."
        )
    return (
        f"Stage {spec.id} claimed success without writing its contracted artifact. "
        "Open the stage detail, re-run the exact argv in a terminal, and check the "
        "mode's fileRegex in .bob/custom_modes.yaml allows that path."
    )


def _tail(text: str | None, limit: int = 8000) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return "…\n" + text[-limit:]


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        for key in ("total", "coins", "amount", "cost"):
            nested = _as_float(value.get(key))
            if nested is not None:
                return nested
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return float(value)
    return None


def _write_json(path: Path, payload: Any) -> None:
    """Atomic-ish JSON write for the small side files this module owns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    tmp.replace(path)
