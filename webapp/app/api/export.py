"""Downloading a run: the whole solution, or just the code.

Four endpoints, one shape:

    GET /api/runs/{run_id}/export           the whole solution + audit trail
    GET /api/runs/{run_id}/export/code      only the generated code
    GET /api/runs/{run_id}/export/project   every file the run wrote, anywhere
                                            under the project root, at its real
                                            path — not only .arch/build/
    GET /api/runs/{run_id}/export/preview   JSON inventory, downloads nothing

The archive is planned before the response starts. That ordering is the reason
this file is short: every refusal — an unknown run, a run that generated no
code, a path that would escape the project — happens while the status code can
still be an error the front end renders. Once the first byte of a ZIP is on the
wire, the only way to fail is a truncated download nobody can diagnose.

The preview endpoint exists so a UI can show what a download will contain
(and, more usefully, what it will NOT contain) before spending the bytes. It
runs the same planner, so it can never disagree with the download.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .. import export as export_mod
from ..config import Settings
from ..errors import AppError
from ..store import RunStore

log = logging.getLogger("arch2code.api.export")

router = APIRouter(prefix="/api", tags=["export"])


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


def _stream(plan: export_mod.ExportPlan) -> StreamingResponse:
    """Wrap a plan in the response, with headers a browser saves correctly."""
    filename = plan.filename
    disposition = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    log.info(
        "export %s (%s): %d entries, %d bytes uncompressed",
        plan.run_id, plan.kind, len(plan.entries), plan.total_bytes,
    )
    return StreamingResponse(
        export_mod.iter_zip(plan),
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            # No Content-Length: the compressed size is not known until the
            # archive has been produced, and a wrong one truncates the download.
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Arch2Code-Entries": str(len(plan.entries)),
        },
    )


@router.get(
    "/runs/{run_id}/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}},
                     "description": "The whole solution, streamed as a ZIP."}},
)
async def export_run(
    run_id: str,
    settings: Settings = Depends(get_settings),
    store: RunStore = Depends(get_store),
) -> StreamingResponse:
    """The whole solution: generated code, audit trail, both images, MANIFEST.md.

    This is the archive somebody opens months later. MANIFEST.md is generated at
    download time and states which drawing this came from, which model read it,
    at what confidence, what was assumed and what was left unresolved.
    """
    plan = export_mod.build_export(settings, store, run_id, kind="full")
    return _stream(plan)


@router.get(
    "/runs/{run_id}/export/code",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}},
                     "description": "Only the generated code, streamed as a ZIP."}},
)
async def export_run_code(
    run_id: str,
    settings: Settings = Depends(get_settings),
    store: RunStore = Depends(get_store),
) -> StreamingResponse:
    """Only the generated code, for someone who just wants to run it.

    No audit trail, no images. A run that generated nothing is refused with an
    explanation rather than served as an empty archive.
    """
    plan = export_mod.build_export(settings, store, run_id, kind="code")
    return _stream(plan)


@router.get(
    "/runs/{run_id}/export/project",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}},
                     "description": "Everything the run wrote in the project tree."}},
)
async def export_run_project(
    run_id: str,
    settings: Settings = Depends(get_settings),
    store: RunStore = Depends(get_store),
) -> StreamingResponse:
    """Every file the run produced anywhere under the project root.

    The scaffold does not confine itself to ``.arch/build/<run_id>/``: it writes
    real project trees elsewhere and only *describes* them in its manifest. This
    archive carries all of it at the real paths, so the result is a project
    somebody can open, plus the audit trail and the drawing.

    MANIFEST.md states, per file, how it was attributed to this run — a
    filesystem snapshot taken before the first subprocess started, the stage-4
    manifest, or a directory named after the run — and what that method cannot
    prove. A set of files whose selection rule is unstated is a guess.
    """
    plan = export_mod.build_export(settings, store, run_id, kind="project")
    return _stream(plan)


@router.get("/runs/{run_id}/export/preview")
async def preview_export(
    run_id: str,
    kind: str = "full",
    settings: Settings = Depends(get_settings),
    store: RunStore = Depends(get_store),
) -> dict[str, Any]:
    """What the download would contain, without downloading it.

    Same planner as the download, so the two can never disagree — including
    about what is missing, which is the part worth showing before a click. An
    unrecognised ``kind`` falls back to ``full`` rather than 400ing: the preview
    is decoration on a download that would still work.
    """
    resolved: export_mod.ExportKind = (
        kind if kind in ("code", "project") else "full"  # type: ignore[assignment]
    )
    plan = export_mod.build_export(settings, store, run_id, kind=resolved)
    return plan.summary()
