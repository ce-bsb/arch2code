"""Mode A endpoints: the vision preview and the second-pass verification.

Everything here reads what the run already wrote under ``webapp/runs/<run_id>/
vision/``. The extraction itself is executed by the run's background task
(``vision.run_vision_preview``), so a browser refresh, a reconnect or a second tab
all see the same persisted state rather than triggering another inference.

The verify endpoint is the exception: it is synchronous and it does spend an
inference call, because it is a deliberate human action on one specific element.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..config import Settings
from ..errors import AppError, NotFound
from ..models import (
    VerificationListResponse,
    VerifyRecord,
    VerifyRequest,
    VisionPreview,
)
from ..store import RunStore
from ..vision import (
    ArchVisionClient,
    capture_paths,
    image_dimensions,
    load_capture,
    load_extraction,
    normalized_image_path,
    read_verifications,
    summarize_quality,
    verify_target,
)

router = APIRouter(prefix="/api", tags=["vision"])


def get_settings(request: Request) -> Settings:
    return _from_state(request, "settings")


def get_store(request: Request) -> RunStore:
    return _from_state(request, "store")


def get_vision(request: Request) -> ArchVisionClient:
    return _from_state(request, "vision")


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


@router.get("/runs/{run_id}/vision", response_model=VisionPreview)
async def get_vision_preview(
    run_id: str, store: RunStore = Depends(get_store)
) -> VisionPreview:
    """Everything the Mode A screen renders, in one request.

    Tolerant by construction: a run whose extraction has not happened yet returns
    200 with empty lists and an ``error`` block explaining what to do, because this
    page's whole job is to show what state the run is in. A 500 here would be the
    app hiding exactly the information the user came for.

    Every bbox in ``extraction`` is already ``{x, y, w, h}`` in 0..1 with the origin
    at the top-left, clamped into the image. The front end knows nothing about the
    vision model's output format.
    """
    state = store.load(run_id)  # raises NotFound
    paths = capture_paths(store, run_id)

    capture = load_capture(store, run_id)
    width, height = image_dimensions(capture)
    image_ready = normalized_image_path(store, run_id) is not None

    extraction = load_extraction(store, run_id)
    provenance = None
    error = None

    if extraction is None:
        extraction = {
            "components": [],
            "connections": [],
            "boundaries": [],
            "unknowns": [],
            "overall_confidence": None,
            "legibility_notes": None,
            "_bboxes": {},
            "_bbox_warnings": [],
        }
        quality = summarize_quality({})
        if state.status in ("created", "running"):
            error = AppError(
                "vision_not_ready",
                "The extraction has not finished yet",
                f"Run {run_id} is '{state.status}'; vision/extraction.json does not exist yet.",
                remedy="Watch GET /api/runs/{run_id}/stream — vision.extract.finished lands here.",
                status=409,
            ).to_body()
        else:
            error = AppError(
                "vision_no_extraction",
                "This run produced no extraction",
                (
                    f"Run {run_id} is '{state.status}' and vision/extraction.json was never "
                    f"written."
                ),
                remedy=(
                    "Check the timeline for vision.tool_error: an arch_vision failure carries "
                    "the exact fix (missing WATSONX_APIKEY, TLS interception, VPN, a 404 model "
                    "id, a timeout). If the run never started, POST /api/runs/{run_id}/start."
                ),
                status=409,
            ).to_body()
    else:
        quality = summarize_quality(extraction)
        raw_provenance = extraction.get("_provenance")
        provenance = dict(raw_provenance) if isinstance(raw_provenance, dict) else None

    return VisionPreview(
        run_id=run_id,
        status=state.status,
        image={
            "variant": "normalized",
            "width": width,
            "height": height,
            "url": f"/api/runs/{run_id}/image?variant=normalized",
            "original_url": f"/api/uploads/{state.upload.upload_id}/file",
            "ready": image_ready,
        },
        capture=capture,
        extraction=extraction,
        provenance=provenance,
        quality=quality,
        verifications=read_verifications(store, run_id),
        raw_available=paths["extraction_raw"].exists() or paths["extraction"].exists(),
        error=error,
    )


@router.post("/runs/{run_id}/vision/verify", response_model=VerifyRecord)
async def verify_element(
    run_id: str,
    req: VerifyRequest,
    store: RunStore = Depends(get_store),
    settings: Settings = Depends(get_settings),
    client: ArchVisionClient = Depends(get_vision),
) -> VerifyRecord:
    """The second pass that makes Mode A worth watching.

    A different prompt and a different pass over the same image, framed
    adversarially ("somebody may have read this wrong"), which produces error
    decorrelated from the extraction. That is why it catches what the extraction
    did not — asking the same question the same way would only confirm the same
    bias.

    ``uncertain`` is a legitimate answer. A tool-level failure becomes
    ``verdict="error"`` carrying the server's own actionable message, never an
    HTTP 500: the tool does not throw, and neither does this.
    """
    store.load(run_id)  # raises NotFound before anything is spent
    log = store.eventlog(run_id)
    return await verify_target(
        settings,
        store,
        client,
        run_id,
        target_kind=req.target_kind,
        target_id=req.target_id,
        claim=req.claim,
        emit=log.aappend,
    )


@router.get("/runs/{run_id}/vision/verifications", response_model=VerificationListResponse)
async def list_verifications(
    run_id: str, target_id: str | None = None, store: RunStore = Depends(get_store)
) -> VerificationListResponse:
    """Replay the verification history so the side-by-side survives a reload."""
    store.load(run_id)
    return VerificationListResponse(
        verifications=read_verifications(store, run_id, target_id)
    )


@router.get("/runs/{run_id}/vision/raw")
async def get_raw_extraction(
    run_id: str, store: RunStore = Depends(get_store)
) -> dict[str, Any]:
    """The untouched MCP payload, for anyone auditing what the model actually said.

    ``extraction.json`` is enriched (clamped bboxes, coerced confidences); this is
    the answer as it arrived, and the two being separate files is what makes the
    normalization auditable rather than a black box.
    """
    paths = capture_paths(store, run_id)
    path = paths["extraction_raw"] if paths["extraction_raw"].exists() else paths["extraction"]
    if not path.exists():
        raise NotFound(
            "vision_raw_missing",
            "No raw extraction was stored for this run",
            f"Expected {path}.",
            remedy="Run the vision mode for this run first (POST /api/runs/{run_id}/start).",
            expected_path=str(path),
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NotFound(
            "vision_raw_unreadable",
            "The raw extraction could not be read",
            f"{path}: {exc}",
            remedy="Re-run the extraction; the file was written incompletely.",
            expected_path=str(path),
        ) from exc
