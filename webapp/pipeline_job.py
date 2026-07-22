"""Entrypoint for the Code Engine job that executes one leg of one run.

    python -m webapp.pipeline_job

Why this file exists
--------------------
A Code Engine **Application** caps an HTTP connection at 600 s — the
documentation is explicit that the cut is by absolute duration, *"even if the
connection is not idle"* — and the arch2code pipeline takes minutes. So the app
keeps the UI, the API and the SSE stream, and the pipeline moves to a **Job**,
which is the only primitive with a real time budget (``--maxexecutiontime``,
default 7200 s, maximum 86400 s).

Contract with the app
---------------------
Environment in::

    ARCH2CODE_RUN_ID       required. The run to execute. Must already exist in
                           the store: the app creates it, uploads the drawing
                           and only then submits the job run.
    ARCH2CODE_FROM_STAGE   optional. Stage id to start at ("intake", "analyst",
                           "critic", "scaffold", "validator", "capture",
                           "extract"). Default: the first stage of the run's
                           mode.
    ARCH2CODE_RESUMED_FROM optional. Recorded in the run.started event so the
                           timeline shows the leg boundary.

Exit codes::

    0   the leg finished: the run succeeded, OR it parked at the human gate.
        Parking is a success. The job must NOT sit and wait for a person —
        it would burn billed CPU and could hit the 24 h ceiling. The app shows
        the verdict, the human decides, and a SECOND job run is submitted with
        ARCH2CODE_FROM_STAGE=scaffold.
    1   the run failed, or the job could not start (bad configuration, missing
        run, unreachable store). Either way the reason is on stdout and, when
        the store was reachable, in the run's event log.

Everything else about a run is unchanged from localhost: the same stage table,
the same Bob argv, the same gate string, the same events. This file adds exactly
three things around the existing :class:`~app.pipeline.PipelineRunner`:
hydrating the ephemeral filesystem before it runs, mirroring events while it
runs, and syncing artifacts back after it runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

# Importable both as `python -m webapp.pipeline_job` (from /app) and as
# `python pipeline_job.py` (from /app/webapp). The container uses the first
# form; the second exists so a developer can run it without ceremony.
if __package__ in (None, ""):  # pragma: no cover - direct invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.app import storage as storage_mod
from webapp.app.config import Settings, load_settings
from webapp.app.errors import AppError
from webapp.app.eventlog import EventLogRegistry
from webapp.app.health import HealthCache
from webapp.app.models import RunState
from webapp.app.pipeline import PIPELINE_STAGES, PipelineRunner, VISION_STAGES, stage_by_id
from webapp.app.storage import keys as K
from webapp.app.store import RunStore, UploadStore
from webapp.app.vision import ArchVisionClient

log = logging.getLogger("arch2code.job")

__all__ = ["main", "run_leg", "JobConfig"]

#: Statuses that mean "this leg did what it was asked to do".
_OK_STATUSES = frozenset({"succeeded", "awaiting_input", "blocked", "cancelled"})


class JobConfig:
    """The job's inputs, read once and validated loudly."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        source = dict(os.environ if env is None else env)

        self.run_id = (source.get("ARCH2CODE_RUN_ID") or "").strip()
        if not self.run_id:
            raise SystemExit(
                _fatal(
                    "ARCH2CODE_RUN_ID is not set, so there is nothing to execute.",
                    "Submit the job run with the id, e.g.\n"
                    "  ibmcloud ce jobrun submit --name run-20260721-2130-demo \\\n"
                    "    --job arch2code-pipeline \\\n"
                    "    --env ARCH2CODE_RUN_ID=20260721-2130-demo\n"
                    "The app sets it automatically when it creates the job run.",
                )
            )

        raw_stage = (source.get("ARCH2CODE_FROM_STAGE") or "").strip()
        self.from_stage = raw_stage or None
        if self.from_stage is not None:
            known = {s.id for s in (*PIPELINE_STAGES, *VISION_STAGES)}
            if self.from_stage not in known:
                raise SystemExit(
                    _fatal(
                        f"ARCH2CODE_FROM_STAGE={self.from_stage!r} is not a stage id.",
                        f"Use one of: {', '.join(sorted(known))}. Leave it unset to "
                        "start at the first stage of the run's mode.",
                    )
                )

        self.resumed_from = (source.get("ARCH2CODE_RESUMED_FROM") or "").strip() or None
        self.backend = storage_mod.backend_name(source)
        self.sync_arch = _flag(source, "ARCH2CODE_JOB_SYNC_ARCH", True)


def _flag(source: dict[str, str], key: str, default: bool) -> bool:
    raw = (source.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def _fatal(what: str, remedy: str) -> str:
    """A startup failure, formatted the way every other failure in this app is."""
    return f"\narch2code job cannot start.\n\n  what: {what}\n\n  remedy: {remedy}\n"


# --------------------------------------------------------------------------- #
# hydration and sync
# --------------------------------------------------------------------------- #


def hydrate(store: storage_mod.ObjectStore, settings: Settings, run_id: str) -> RunState:
    """Materialize everything the run needs onto the container's local disk.

    Bob is a subprocess: it opens real files in a real working directory. No
    object store changes that, so the job pulls the run down, executes exactly
    as localhost does, and pushes the results back. That is also why the local
    backend needs no hydration at all — the files are already there, which is
    what makes the ``ARCH2CODE_STORAGE_BACKEND=local`` path identical to the
    demo everyone has already seen work.
    """
    run_dir = settings.runs_root / run_id

    if store.backend == "local":
        # Deliberately no mkdir on this branch: with the local backend the run
        # directory is the app's, and creating an empty one for a typo'd id
        # would leave litter that GET /api/runs then lists as a broken run.
        state_path = run_dir / "run.json"
        if not state_path.exists():
            raise SystemExit(
                _fatal(
                    f"{state_path} does not exist.",
                    "Create the run through the API first (POST /api/runs). With the "
                    "local backend the job must run on the same filesystem as the app, "
                    "which is true for the Plan B single-container deployment and for "
                    "localhost.",
                )
            )
        return RunState.model_validate_json(state_path.read_text(encoding="utf-8"))

    # --- COS -------------------------------------------------------------- #
    run_dir.mkdir(parents=True, exist_ok=True)
    storage_mod.download_prefix(
        store, K.run_prefix(run_id), run_dir, required=["run.json"]
    )
    state = RunState.model_validate_json(
        (run_dir / "run.json").read_text(encoding="utf-8")
    )

    # The drawing. The app placed it under uploads/<id>/<filename>; the pipeline
    # expects a verbatim copy inside the workspace input directory, and
    # store.input_dir() is the one function that knows where that is.
    upload = state.upload
    uploads_dir = settings.uploads_root / upload.upload_id
    storage_mod.download_prefix(
        store,
        K.upload_prefix(upload.upload_id),
        uploads_dir,
        required=[upload.filename],
    )

    input_dir = settings.bob_cwd / ".arch" / "intake" / "inbox" / run_id
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / upload.filename).write_bytes(
        (uploads_dir / upload.filename).read_bytes()
    )

    # A resumed leg (stage 4 onwards) needs everything the earlier leg wrote:
    # the extraction, the AIR and the verdict all live under .arch/ in the
    # workspace, in a container that no longer exists.
    arch_root = settings.bob_cwd / ".arch"
    restored = storage_mod.download_prefix(store, K.arch_prefix(run_id), arch_root)
    if restored:
        log.info("restored %d artifact(s) from the previous leg", len(restored))

    return state


def sync_up(
    store: storage_mod.ObjectStore, settings: Settings, run_id: str, *, arch: bool
) -> int:
    """Push the run directory and the ``.arch/`` tree back to the store.

    Called in a ``finally``: a failed run's stderr, argv and partial artifacts
    are the whole point of having a failed run to look at. A completed job run
    is deleted by Code Engine after one week, so if it is not in the bucket it
    does not exist.
    """
    if store.backend == "local":
        return 0

    written = 0
    try:
        written += len(
            storage_mod.upload_tree(store, settings.runs_root / run_id, K.run_prefix(run_id))
        )
        if arch:
            written += len(
                storage_mod.upload_tree(
                    store, settings.bob_cwd / ".arch", K.arch_prefix(run_id)
                )
            )
    except storage_mod.StorageError as exc:
        log.error(
            "artifact sync failed: %s — remedy: %s",
            exc.detail, exc.remedy,
        )
    return written


# --------------------------------------------------------------------------- #
# the leg
# --------------------------------------------------------------------------- #


async def run_leg(config: JobConfig) -> int:
    """Execute one leg of one run. Returns the process exit code."""
    settings = load_settings()
    settings.ensure_dirs()

    log.info("arch2code pipeline job")
    log.info("  run id       : %s", config.run_id)
    log.info("  from stage   : %s", config.from_stage or "<first>")
    log.info("  storage      : %s", config.backend)
    log.info("  project root : %s", settings.project_root)
    log.info("  bob cwd      : %s", settings.bob_cwd)
    log.info("  bob binary   : %s", " ".join(settings.bob_bin) or "<not configured>")
    log.info("  interpreter  : %s", settings.python_bin)

    store = storage_mod.open_object_store(settings)
    try:
        store.probe()
    except storage_mod.StorageError as exc:
        print(_fatal(exc.detail, exc.remedy), file=sys.stderr)
        return 1

    state = hydrate(store, settings, config.run_id)

    events = EventLogRegistry(settings.runs_root)
    run_store = RunStore(settings, events)
    UploadStore(settings)  # creates uploads/ with the same invariants as the app
    health = HealthCache(settings)
    vision = ArchVisionClient(settings)
    runner = PipelineRunner(settings, run_store, health, vision)

    # Probes never block startup — they block a mode. Running them here means a
    # missing Bob is reported as a stage failure with a remedy instead of an
    # ImportError-shaped mystery five seconds into stage 1.
    report = await health.refresh()
    for probe in getattr(report, "probes", []):
        if probe.level != "ok":
            log.warning("[%s] %s — %s", probe.level, probe.title, probe.remedy or "")

    from_index = _resolve_from_index(state, config.from_stage)

    publisher: storage_mod.EventPublisher | None = None
    already = 0
    if store.backend != "local":
        # This has to happen BEFORE the EventLog is constructed. EventLog reads
        # the highest id off disk once, in its constructor; rebuilding the file
        # afterwards would leave it minting ids from 1 and the publisher would
        # overwrite the previous leg's event objects.
        already = storage_mod.rebuild_local_log(
            store, config.run_id, settings.runs_root / config.run_id / "events.jsonl"
        )

    event_log = run_store.eventlog(config.run_id)

    if store.backend != "local":
        publisher = storage_mod.EventPublisher(
            store, config.run_id, event_log.path, published_through=already
        ).start()
        if already:
            log.info("resuming the event log after id %d", already)

    # Register the cancellation event the runner's stage loop and cancel() both
    # look for. _spawn() normally does this; the job drives _execute directly,
    # so without this line a SIGTERM would mark the run cancelled while leaving
    # the Bob subprocess running until the container is killed.
    runner._cancels[config.run_id] = asyncio.Event()  # noqa: SLF001
    _install_sigterm(runner, config.run_id)

    try:
        await _mark_running(run_store, config.run_id, from_index)
        # _execute is the runner's own stage loop. It is used directly rather
        # than through start()/resume() because those two also own the
        # in-process concurrency policy and the gate bookkeeping, both of which
        # belong to the app in this architecture. The seam is the signature the
        # briefing names: _spawn(run_id, from_index, resumed_from).
        await runner._execute(  # noqa: SLF001 - documented seam
            config.run_id, from_index, resumed_from=config.resumed_from
        )
    except AppError as exc:
        log.error("%s: %s — remedy: %s", exc.code, exc.detail, exc.remedy)
    except Exception:  # noqa: BLE001 - nothing leaves this process untranslated
        log.exception("the pipeline job crashed")
    finally:
        final = _load_quietly(run_store, config.run_id)
        if publisher is not None:
            publisher.flush()
            publisher.stop()
            publisher.flush()
            if final is not None and final.status in ("succeeded", "failed", "cancelled", "blocked"):
                publisher.mark_closed(final.status)
        count = sync_up(store, settings, config.run_id, arch=config.sync_arch)
        if count:
            log.info("synced %d object(s) to %s", count, store.describe().get("bucket"))

    final = _load_quietly(run_store, config.run_id)
    if final is None:
        log.error("run.json could not be read after the leg finished")
        return 1

    log.info("leg finished: run status = %s", final.status)
    if final.status == "awaiting_input":
        log.info(
            "The run is parked at the human gate. This job run is DONE and exits 0. "
            "Submit the next leg after the decision with "
            "ARCH2CODE_FROM_STAGE=scaffold."
        )
    if final.status == "failed" and final.error is not None:
        log.error("%s: %s", final.error.title, final.error.detail)
        log.error("remedy: %s", final.error.remedy)

    return 0 if final.status in _OK_STATUSES else 1


def _resolve_from_index(state: RunState, from_stage: str | None) -> int:
    """Zero-based index into the run's stage list."""
    if from_stage is None:
        return 0
    spec = stage_by_id(from_stage)  # type: ignore[arg-type]
    if spec.mode != state.mode:
        raise SystemExit(
            _fatal(
                f"Stage {from_stage!r} belongs to mode {spec.mode!r}, but run "
                f"{state.run_id} is a {state.mode!r} run.",
                "Use a stage of the run's own mode: capture/extract for vision, "
                "intake/analyst/critic/scaffold/validator for pipeline.",
            )
        )
    return spec.index - 1


async def _mark_running(store: RunStore, run_id: str, from_index: int) -> None:
    """Put the run into 'running' and clear the stages this leg will redo.

    Mirrors what :func:`app.pipeline._mark_running` does for the in-process
    runner. Duplicated rather than imported because it is a two-line state
    transition and importing a private from another module across a process
    boundary is a worse dependency than the duplication.
    """

    def apply(state: RunState) -> None:
        state.status = "running"
        state.error = None
        for stage in state.stages:
            if stage.index >= from_index + 1:
                stage.status = "pending"
                stage.started_at = None
                stage.finished_at = None
                stage.duration_ms = None
                stage.exit_code = None
                stage.error = None
                stage.artifacts = []

    await store.update(run_id, apply)


def _load_quietly(store: RunStore, run_id: str) -> RunState | None:
    try:
        return store.load(run_id)
    except Exception:  # noqa: BLE001
        return None


def _install_sigterm(runner: PipelineRunner, run_id: str) -> None:
    """Turn the platform's SIGTERM into a clean cancellation.

    Code Engine sends SIGTERM before it kills a job run — on
    ``jobrun delete``, on hitting ``--maxexecutiontime``, or when an instance is
    reclaimed. Ignoring it means the Bob subprocess is killed mid-write and the
    run ends with no terminal event, leaving the browser tailing forever. This
    turns it into the cancellation path that already exists and already emits
    ``run.cancelled``.
    """
    loop = asyncio.get_running_loop()

    def handler() -> None:
        log.warning(
            "SIGTERM received: cancelling run %s. The event log and the artifacts "
            "written so far are synced before this process exits.", run_id,
        )
        loop.create_task(runner.cancel(run_id))

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, handler)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=getattr(
            logging, os.environ.get("ARCH2CODE_LOG_LEVEL", "INFO").upper(), logging.INFO
        ),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,  # Code Engine collects stdout; keep both streams ordered
    )
    config = JobConfig()
    try:
        return asyncio.run(run_leg(config))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
