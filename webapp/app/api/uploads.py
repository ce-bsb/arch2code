"""Upload endpoints: the entry point of both modes.

The original bytes are persisted verbatim. Everything downstream — the sha256 in
the capture manifest, the normalized PNG the model reads, the bboxes drawn over
it — is derived from this file and has to be traceable back to it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse

from ..config import Settings
from ..errors import AppError, NotFound, PayloadTooLarge
from ..ingest import IngestError
from ..models import UploadListResponse, UploadRef
from ..ingest import spec_for
from ..routing import accepted_upload_extensions, as_app_error, reject_by_extension
from ..store import UploadStore

router = APIRouter(prefix="/api", tags=["uploads"])

_CHUNK = 1 << 20  # 1 MiB


def get_settings(request: Request) -> Settings:
    """Read Settings off app.state, where the lifespan handler put it."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:  # pragma: no cover - only if the lifespan did not run
        raise AppError(
            "app_not_initialised",
            "The application did not finish starting",
            "app.state.settings is missing.",
            remedy="Restart the server with ./run.sh and read the startup log.",
            status=500,
        )
    return settings


def get_uploads(request: Request) -> UploadStore:
    store = getattr(request.app.state, "uploads", None)
    if store is None:  # pragma: no cover
        raise AppError(
            "app_not_initialised",
            "The application did not finish starting",
            "app.state.uploads is missing.",
            remedy="Restart the server with ./run.sh and read the startup log.",
            status=500,
        )
    return store


@router.post("/uploads", status_code=201, response_model=UploadRef)
async def create_upload(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    uploads: UploadStore = Depends(get_uploads),
) -> UploadRef:
    """Persist one diagram and classify which extraction path it belongs on.

    The classification is made from the file's **bytes**, in ``UploadStore.save``,
    not from its name. Two things follow, and both are deliberate:

    * The 415 for an unreadable file now names what the file *actually is* and how
      to convert it — ``.pptx`` says which menu item exports the slide, ``.vsd``
      says which Save As turns it into a ``.vsdx`` we read exactly. A rejection
      without a next step is the failure mode this endpoint exists to avoid.
    * A file whose bytes contradict its suffix (a PDF renamed ``sketch.png``) is
      refused rather than handed to the wrong decoder. That was a real latent
      bug: ``mimetypes.guess_type`` and ``Path.suffix`` are both pure string
      matching, and the failure surfaced deep inside Pillow with no useful text.

    Only one check happens before the body is streamed: a suffix that *proves* the
    file is one of the declared refusals. Reading 25 MB we will certainly reject
    is a waste, but a suffix can never unlock an adapter on its own.

    Classification is also what stops the UI from offering vision for a
    ``.drawio``: a structured source is read exactly, for free, and using vision
    there is a forbidden move in this harness.
    """
    filename = (file.filename or "").strip() or "upload"
    safe_name = Path(filename).name

    try:
        reject_by_extension(safe_name)
    except IngestError as exc:
        raise as_app_error(exc) from exc

    limit_bytes = int(settings.max_upload_mb * 1024 * 1024)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            # Stop reading rather than buffering the rest of a file we will refuse.
            raise PayloadTooLarge(
                "upload_too_large",
                "That file is over the upload limit",
                f"'{safe_name}' exceeds the {settings.max_upload_mb:g} MB limit.",
                remedy=(
                    "Raise ARCH2CODE_MAX_UPLOAD_MB, or shrink the image first — "
                    "capture_diagram.py resizes the longest edge to 1568px anyway, so "
                    "anything larger is bytes the model will never see."
                ),
                limit_mb=settings.max_upload_mb,
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise AppError(
            "upload_empty",
            "The uploaded file is empty",
            f"'{safe_name}' contains zero bytes.",
            remedy="Re-export the diagram and upload it again.",
            status=422,
            filename=safe_name,
        )

    ref = uploads.save(safe_name, data, file.content_type)

    if ref.ingest and ref.ingest.vision_required:
        spec = spec_for(ref.ingest.format_id)
        if spec.capability in {"raster", "hybrid"}:
            if ref.ingest.mime not in _MODEL_READY_MIME:
                ref.warnings.append(
                    f"{ref.ingest.format_label} is not a format the watsonx endpoint "
                    "reads directly; it is normalized to PNG first, which this app "
                    "always does — but never point the MCP tool at the original."
                )
        else:
            ref.warnings.append(
                f"A {ref.ingest.format_label} yields no image here, so the structural "
                "read is all there is: exact labels, no arrows. Upload a PNG or PDF "
                "export of the same drawing if the connections matter."
            )
    return ref


#: What ``mcp/arch_vision/server.py::ALLOWED_IMAGE_TYPES`` accepts as *model*
#: input. Anything else on the vision path has to be normalized to PNG first, and
#: that guard is correct: HEIC must never reach the watsonx endpoint.
_MODEL_READY_MIME: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


@router.get("/uploads/formats")
async def list_formats() -> dict[str, object]:
    """Every format this build reads, and how to convert the ones it does not.

    The front end uses ``extensions`` for the file picker's ``accept`` attribute
    and renders ``refused`` as a lookup table, so a user who has a ``.vsd`` or an
    Astah model finds the two clicks that fix it without uploading anything first.
    """
    from ..ingest import FORMATS

    readable = [
        {
            "id": spec.id,
            "label": spec.label,
            "family": spec.family,
            "extensions": list(spec.extensions),
            "capability": spec.capability,
            "multipage": spec.multipage,
            "note": spec.note,
        }
        for spec in FORMATS.values()
        if spec.capability != "refuse"
    ]
    refused = [
        {
            "id": spec.id,
            "label": spec.label,
            "extensions": list(spec.extensions),
            "remedy": spec.remedy,
        }
        for spec in FORMATS.values()
        if spec.capability == "refuse"
    ]
    return {
        "extensions": list(accepted_upload_extensions()),
        "readable": sorted(readable, key=lambda f: f["label"]),
        "refused": sorted(refused, key=lambda f: f["label"]),
    }


@router.get("/uploads", response_model=UploadListResponse)
async def list_uploads(
    limit: int = 50, uploads: UploadStore = Depends(get_uploads)
) -> UploadListResponse:
    """Previously uploaded artifacts, newest first.

    A demo re-runs the same diagram several times; making the user find the file
    again each time is the kind of friction that eats a live slot.
    """
    limit = max(1, min(int(limit), 500))
    return UploadListResponse(uploads=uploads.list(limit=limit))


@router.get("/uploads/{upload_id}/file")
async def get_upload_file(
    upload_id: str, uploads: UploadStore = Depends(get_uploads)
) -> FileResponse:
    """Serve the untouched original.

    Mode A's overlay deliberately draws on the *normalized* PNG instead
    (``GET /api/runs/{run_id}/image``): the bboxes are normalized against what the
    model actually saw, and a rotated phone photo would put every box in the wrong
    place here.
    """
    ref = uploads.load(upload_id)
    path = uploads.file_path(upload_id)
    if not path.exists():
        raise NotFound(
            "upload_file_missing",
            "The stored upload is gone",
            f"{ref.filename} was recorded but {path} no longer exists.",
            remedy="Upload the diagram again; webapp/uploads/ is git-ignored and safe to clear.",
            expected_path=str(path),
        )
    return FileResponse(
        path,
        media_type=ref.content_type or "application/octet-stream",
        filename=ref.filename,
        headers={"Cache-Control": "no-store"},
    )
