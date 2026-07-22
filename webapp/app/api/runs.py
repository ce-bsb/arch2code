"""Run lifecycle: create, start, inspect, decide the gate, cancel.

Creation and start are deliberately two calls. Creating a run mints the id and
copies the drawing into the workspace — cheap, synchronous, and safe to retry.
Starting spawns subprocesses and spends quota. Fusing them would mean a browser
retry silently paying twice.

The gate endpoint is the only place a human decision enters the pipeline. It
refuses to approve a run whose ``verdict.md`` never contained the gate string
unless the caller says so explicitly and gives a reason: "the critic never
approved this" must never be the same code path as "the critic approved this".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import artifacts as artifacts_mod
from ..config import Settings
from ..errors import AppError, BadRequest, Conflict, NotFound
from ..models import (
    ArtifactListResponse,
    CreateRunRequest,
    GateDecision,
    RunListResponse,
    RunState,
    StageDetail,
    StageState,
)
from ..pipeline import PipelineRunner, stages_for
from ..store import RunStore, UploadStore, mint_run_id, slugify

router = APIRouter(prefix="/api", tags=["runs"])


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #


def _from_state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:  # pragma: no cover - only if the lifespan did not run
        raise AppError(
            "app_not_initialised",
            "The application did not finish starting",
            f"app.state.{name} is missing.",
            remedy="Restart the server with ./run.sh and read the startup log.",
            status=500,
        )
    return value


def get_settings(request: Request) -> Settings:
    return _from_state(request, "settings")


def get_store(request: Request) -> RunStore:
    return _from_state(request, "store")


def get_uploads(request: Request) -> UploadStore:
    return _from_state(request, "uploads")


def get_runner(request: Request) -> PipelineRunner:
    return _from_state(request, "runner")


# --------------------------------------------------------------------------- #
# create / list / read
# --------------------------------------------------------------------------- #


@router.post("/runs", status_code=201, response_model=RunState)
async def create_run(
    payload: CreateRunRequest,
    settings: Settings = Depends(get_settings),
    store: RunStore = Depends(get_store),
    uploads: UploadStore = Depends(get_uploads),
) -> RunState:
    """Mint a run id, copy the drawing into the workspace, write ``run.json``.

    Spawns nothing. The drawing is copied rather than referenced because Bob only
    reads inside its working directory, and because the run's audit trail should
    not break when someone clears the upload cache.
    """
    upload = uploads.load(payload.upload_id)
    source = uploads.file_path(payload.upload_id)

    if payload.mode == "vision" and upload.routing.extraction_path == "deterministic":
        raise BadRequest(
            code="deterministic_source",
            title="This artifact does not need vision",
            detail=(
                f"{upload.filename} is a structured source. Reading it with "
                "parse_drawio.py is exact and costs nothing; vision would be less "
                "accurate and would spend tokens."
            ),
            remedy="Run the full pipeline, which routes structured sources to the parser.",
        )

    slug = slugify(payload.slug or Path(upload.filename).stem)
    run_id = mint_run_id(slug)
    while store.exists(run_id):
        run_id = mint_run_id(f"{slug}-b")

    input_dir = store.input_dir(run_id)
    target = input_dir / upload.filename
    target.write_bytes(source.read_bytes())

    now = upload.created_at.__class__.now(upload.created_at.tzinfo)  # same tz as UploadRef
    state = RunState(
        run_id=run_id,
        mode=payload.mode,
        status="created",
        slug=slug,
        created_at=now,
        updated_at=now,
        upload=upload,
        source_kind=payload.source_kind,
        hint=payload.hint,
        options=payload.options,
        project_root=str(settings.project_root),
        bob_cwd=str(settings.bob_cwd),
        stages=[
            StageState(id=spec.id, index=spec.index, title=spec.title, slug=spec.slug)
            for spec in stages_for(payload.mode)
        ],
    )
    return store.create(state)


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    store: RunStore = Depends(get_store),
) -> RunListResponse:
    return RunListResponse(runs=store.list(limit=limit))


@router.get("/runs/{run_id}", response_model=RunState)
async def get_run(run_id: str, store: RunStore = Depends(get_store)) -> RunState:
    return store.load(run_id)


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


@router.post("/runs/{run_id}/start", response_model=RunState)
async def start_run(
    run_id: str,
    store: RunStore = Depends(get_store),
    runner: PipelineRunner = Depends(get_runner),
) -> RunState:
    """Begin executing the run's stages in the background."""
    state = store.load(run_id)
    if runner.is_active(run_id):
        raise Conflict(
            code="run_already_active",
            title="That run is already executing",
            detail=f"{run_id} has a task in flight.",
            remedy="Watch GET /api/runs/{run_id}/stream, or cancel it first.",
        )
    if state.status in {"succeeded", "failed", "cancelled"}:
        raise Conflict(
            code="run_finished",
            title="That run already finished",
            detail=f"{run_id} is {state.status}.",
            remedy="Create a new run for another attempt.",
        )
    await runner.start(run_id)
    return store.load(run_id)


@router.post("/runs/{run_id}/gate", response_model=RunState)
async def decide_gate(
    run_id: str,
    decision: GateDecision = Body(...),
    store: RunStore = Depends(get_store),
    runner: PipelineRunner = Depends(get_runner),
) -> RunState:
    """Record the human decision at the stage-3 gate and resume from there.

    An approval that contradicts the parsed verdict — including approving a run
    whose ``verdict.md`` has no gate string at all — requires a written reason.
    That reason is what an auditor reads later; a silent override would make the
    whole gate decorative.
    """
    state = store.load(run_id)
    if state.status != "awaiting_input" or state.gate is None:
        raise Conflict(
            code="gate_not_open",
            title="This run is not waiting at the gate",
            detail=f"{run_id} is {state.status}.",
            remedy="Only a run in awaiting_input accepts a gate decision.",
        )

    contradicts = (
        (decision.decision == "approve" and state.gate.verdict != "approved")
        or (decision.decision == "block" and state.gate.verdict == "approved")
    )
    if contradicts and not (decision.reason or "").strip():
        detail = (
            "verdict.md contains no gate string at all"
            if state.gate.verdict == "absent"
            else f"the critic's verdict was {state.gate.verdict!r}"
        )
        raise BadRequest(
            code="override_needs_reason",
            title="Overriding the critic requires a reason",
            detail=f"You chose {decision.decision!r} but {detail}.",
            remedy="Send a reason explaining why you are overriding the critic's verdict.",
        )

    await runner.resume(run_id, decision)
    return store.load(run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunState)
async def cancel_run(
    run_id: str,
    store: RunStore = Depends(get_store),
    runner: PipelineRunner = Depends(get_runner),
) -> RunState:
    store.load(run_id)
    await runner.cancel(run_id)
    return store.load(run_id)


# --------------------------------------------------------------------------- #
# inspection
# --------------------------------------------------------------------------- #


@router.get("/runs/{run_id}/stages/{stage_id}", response_model=StageDetail)
async def get_stage(
    run_id: str,
    stage_id: str,
    tail: int = Query(200, ge=1, le=5000),
    store: RunStore = Depends(get_store),
) -> StageDetail:
    """The debugging pane: the exact argv, the raw NDJSON and the stderr tail.

    stderr is surfaced verbatim because Bob's pre-flight failures — invalid auth,
    unaccepted licence, unknown mode slug — produce zero bytes of NDJSON and put
    the only diagnosis there.
    """
    state = store.load(run_id)
    stage = next((s for s in state.stages if s.id == stage_id), None)
    if stage is None:
        raise NotFound(
            code="stage_not_found",
            title="No such stage in this run",
            detail=f"{run_id} has no stage {stage_id!r}.",
            remedy=f"Valid stages: {', '.join(s.id for s in state.stages)}.",
        )

    sdir = store.stage_dir(run_id, stage_id)
    ndjson_path = sdir / "stdout.ndjson"
    stderr_path = sdir / "stderr.txt"
    # PipelineRunner._run_stage writes this file as ``argv.json``. This pane
    # asked for ``command.json``, which nothing has ever written, so the argv,
    # cwd and env_keys fields came back empty for every stage of every run.
    # ``command.json`` is kept as a fallback purely so an older run directory
    # written under the other name would still render.
    meta_path = sdir / "argv.json"
    if not meta_path.exists():
        meta_path = sdir / "command.json"

    argv: list[str] = []
    cwd = ""
    env_keys: list[str] = []
    if meta_path.exists():
        try:
            import json

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            argv = list(meta.get("argv") or [])
            cwd = str(meta.get("cwd") or "")
            env_keys = sorted(meta.get("env_keys") or [])
        except Exception:  # noqa: BLE001 - the pane must render even when this is corrupt
            pass

    stdout_tail: list[str] = []
    if ndjson_path.exists():
        try:
            lines = ndjson_path.read_text(encoding="utf-8", errors="replace").splitlines()
            stdout_tail = lines[-tail:]
        except Exception:  # noqa: BLE001
            pass

    stderr_tail = stage.stderr_tail
    if not stderr_tail and stderr_path.exists():
        try:
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:]
        except Exception:  # noqa: BLE001
            pass

    return StageDetail(
        stage=stage,
        argv=argv,
        cwd=cwd or state.bob_cwd,
        env_keys=env_keys,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        ndjson_path=str(ndjson_path),
        stderr_path=str(stderr_path),
    )


@router.get("/runs/{run_id}/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    run_id: str,
    store: RunStore = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> ArtifactListResponse:
    """Every artifact each stage was contracted to write, present or not."""
    state = store.load(run_id)
    root = Path(state.bob_cwd or settings.bob_cwd)
    refs = []
    seen: set[str] = set()
    for spec in stages_for(state.mode):
        for ref in artifacts_mod.scan_stage_outputs(run_id, spec, root):
            if ref.path in seen:
                continue
            seen.add(ref.path)
            refs.append(ref)
    return ArtifactListResponse(artifacts=refs)


@router.get("/runs/{run_id}/artifacts/{artifact_id}", response_class=PlainTextResponse)
async def read_artifact(
    run_id: str,
    artifact_id: str,
    store: RunStore = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> PlainTextResponse:
    """Read one artifact as text, refusing anything outside this run's roots."""
    state = store.load(run_id)
    roots = [Path(state.bob_cwd or settings.bob_cwd), store.run_dir(run_id)]
    path = artifacts_mod.resolve(artifact_id, run_id, roots)
    return PlainTextResponse(
        artifacts_mod.read_text(path), media_type="text/plain; charset=utf-8"
    )


@router.get("/runs/{run_id}/image")
async def get_run_image(
    run_id: str,
    variant: str = Query("normalized", pattern="^(normalized|original)$"),
    store: RunStore = Depends(get_store),
) -> FileResponse:
    """The image the bounding boxes are drawn on.

    ``normalized`` is the PNG the vision model actually saw, and it is the only
    correct backdrop for the boxes: the capture step applies EXIF rotation and
    resizes to 1568px, so boxes normalized against it would sit wrong on the
    original. ``original`` exists for comparison only.
    """
    state = store.load(run_id)

    if variant == "original":
        path = store.input_dir(run_id) / state.upload.filename
        if not path.exists():
            path = Path(state.upload.stored_path)
    else:
        from ..vision import normalized_image_path

        found = normalized_image_path(store, run_id)
        if found is None:
            raise NotFound(
                code="normalized_image_missing",
                title="This run has no normalized image yet",
                detail=(
                    "The capture stage has not produced one. A deterministic source "
                    "(.drawio) never produces one at all, because it never goes "
                    "through vision."
                ),
                remedy="Request variant=original, or run the capture stage first.",
            )
        path = found

    if not path.exists():
        raise NotFound(
            code="image_missing",
            title="The image file is gone",
            detail=f"{path} does not exist.",
            remedy="Upload the drawing again and create a new run.",
        )
    return FileResponse(path)
