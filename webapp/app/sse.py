"""Server-sent events framing over StreamingResponse.

The stream is a view onto webapp/runs/<run_id>/events.jsonl, never a separate
queue. On connect it replays from the log starting after Last-Event-ID (falling
back to ?after=, then 0) and only then tails, so a browser reconnect — which the
EventSource does on its own, resending Last-Event-ID — loses nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable

from fastapi import Request
from fastapi.responses import StreamingResponse

from .eventlog import EventLog
from .models import Event

log = logging.getLogger("arch2code.sse")

__all__ = [
    "sse_frame", "sse_comment", "resolve_start_id", "event_stream",
    "sse_response", "SSE_HEADERS",
]

#: no-store keeps a caching proxy from replaying a stale prefix; X-Accel-Buffering
#: disables nginx buffering, which would otherwise hold a whole stage's events
#: back and make a live timeline arrive in one lump at the end.
SSE_HEADERS = {
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_frame(event: Event) -> bytes:
    """Render one event as an SSE frame.

    The id line is what makes reconnection work: the browser echoes the last one
    it saw back as Last-Event-ID.
    """
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, default=str)
    # A data field must not contain a bare newline; JSON never emits one, but a
    # future non-JSON payload would silently truncate the frame without this.
    payload = payload.replace("\n", " ")
    return (
        f"id: {event.id}\n"
        f"event: {event.type}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")


def sse_comment(text: str = "heartbeat") -> bytes:
    """A comment frame. Keeps the connection and any intermediary alive."""
    return f": {text}\n\n".encode("utf-8")


def resolve_start_id(last_event_id_header: str | None, after_query: int | None) -> int:
    """Decide where to resume from: header, then query, then the beginning.

    A malformed header degrades to 0 rather than erroring — replaying the whole
    log is harmless (the client dedupes by id), while a 400 would leave the user
    staring at a stream that will not open.
    """
    if last_event_id_header is not None:
        try:
            value = int(str(last_event_id_header).strip())
            if value >= 0:
                return value
        except (TypeError, ValueError):
            log.debug("ignoring malformed Last-Event-ID: %r", last_event_id_header)
    if after_query is not None and after_query >= 0:
        return after_query
    return 0


async def event_stream(
    log_: EventLog,
    *,
    after: int,
    heartbeat_s: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[bytes]:
    """Yield SSE bytes until the run is terminal or the client goes away.

    Two independent termination conditions, both needed: the log closing (the
    run finished) and the client disconnecting (the tab was closed). Without the
    second, a tailing subscriber would outlive its browser and leak a task per
    refresh.
    """
    yield sse_comment(f"stream open at {after}")

    subscription = log_.subscribe(after=after)
    pending: asyncio.Task[Event] | None = None
    try:
        while True:
            if await is_disconnected():
                return

            if pending is None:
                pending = asyncio.ensure_future(anext_event(subscription))

            done, _ = await asyncio.wait({pending}, timeout=heartbeat_s)
            if not done:
                yield sse_comment()
                continue

            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None

            yield sse_frame(event)
    finally:
        if pending is not None:
            pending.cancel()
        await subscription.aclose()


async def anext_event(iterator: AsyncIterator[Event]) -> Event:
    """`anext` as a coroutine, so it can be wrapped in a Task under 3.10 too."""
    return await iterator.__anext__()


def sse_response(
    log_: EventLog,
    *,
    after: int,
    heartbeat_s: float,
    request: Request,
) -> StreamingResponse:
    return StreamingResponse(
        event_stream(
            log_,
            after=after,
            heartbeat_s=heartbeat_s,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
