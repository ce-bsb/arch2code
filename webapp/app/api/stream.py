"""The live timeline: SSE plus a non-streaming replay of the same log.

Both endpoints read webapp/runs/<run_id>/events.jsonl and emit the identical
Event envelope, so the client reducer is shared between the live path and the
replay path. There is no second source of truth and no in-memory queue that
could disagree with the file.

These two routes deliberately depend on nothing but Settings and the event log
registry. A run whose pipeline module failed to load can still be inspected.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..config import Settings
from ..errors import AppError, NotFound
from ..eventlog import EventLogRegistry
from ..models import EventPage
from ..sse import resolve_start_id, sse_response

router = APIRouter(prefix="/api", tags=["stream"])

#: run ids are minted as YYYYMMDD-HHMM-<slug>, with a -2/-3 suffix on collision.
#: Validated before touching the filesystem: the id is a path segment, and no
#: client-supplied string is ever joined onto a path unchecked.
_RUN_ID_RE = re.compile(r"^\d{8}-\d{4}-[a-z0-9][a-z0-9-]{0,40}$")


def _state(request: Request, name: str):
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


def _resolve_run(request: Request, run_id: str):
    """Validate the id and confirm the run directory exists."""
    if not _RUN_ID_RE.match(run_id or ""):
        raise NotFound(
            "run_not_found",
            "No such run",
            f"{run_id!r} is not a valid run id.",
            remedy="Run ids look like 20260721-1615-checkout. Pick one from the "
                   "run list.",
            run_id=run_id,
        )
    settings: Settings = _state(request, "settings")
    run_dir = settings.runs_root / run_id
    if not run_dir.is_dir():
        raise NotFound(
            "run_not_found",
            "No such run",
            f"{run_dir} does not exist.",
            remedy="The run may have been deleted. Reload the run list.",
            run_id=run_id,
        )
    events: EventLogRegistry = _state(request, "events")
    return settings, events.get(run_id)


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    request: Request,
    after: int | None = Query(None, ge=0),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Live event stream.

    Resume order is Last-Event-ID, then ?after=, then 0. The browser resends the
    header by itself on an automatic reconnect, so ?after= only ever matters for
    the first connection. Everything after the resume point is replayed from
    disk before tailing begins, which is why a refresh mid-run loses nothing.

    On a terminal run the tail is delivered and the stream closes rather than
    heartbeating forever.
    """
    settings, log = _resolve_run(request, run_id)
    start = resolve_start_id(last_event_id, after)
    return sse_response(
        log,
        after=start,
        heartbeat_s=settings.sse_heartbeat_s,
        request=request,
    )


@router.get("/runs/{run_id}/events", response_model=EventPage)
async def replay_events(
    run_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    types: str | None = Query(None),
) -> EventPage:
    """Non-streaming replay, for hydration, debugging and any client that
    cannot hold a connection open.

    `complete` means: the run is terminal AND this page reached the end of the
    log. A client that sees complete=false should keep paging or open the stream.
    """
    _settings, log = _resolve_run(request, run_id)

    wanted = None
    if types:
        wanted = {t.strip() for t in types.split(",") if t.strip()}

    events = log.read(after=after, limit=limit, types=wanted)
    next_after = events[-1].id if events else after
    drained = not log.read(after=next_after, limit=1)

    return EventPage(
        events=events,
        next_after=next_after,
        complete=drained and log.is_closed(),
    )
