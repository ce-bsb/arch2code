"""Append-only JSONL event log, one per run.

This is the PRIMARY STORE, not a cache. Every event is appended to
webapp/runs/<run_id>/events.jsonl with a monotonic id and fsynced BEFORE any
in-memory subscriber is notified. Consequences worth stating:

  * There is no ephemeral queue that can drop an event.
  * SSE replays from the file starting after Last-Event-ID and only then tails,
    so a browser refresh at any moment loses nothing.
  * A run parked at the stage-3 gate survives a server restart, because the
    entire history is on disk.

The in-memory condition is only a wake-up hint. Subscribers also poll on a
short timeout, so a missed notification costs latency and never correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Collection, Mapping

from .models import Event

log = logging.getLogger("arch2code.eventlog")

__all__ = ["EventLog", "EventLogRegistry", "TERMINAL_EVENT_TYPES"]

#: Appending one of these closes the log: the run will never move again, so
#: every open SSE stream may finish. This is a belt-and-braces guarantee — the
#: pipeline runner also calls close() explicitly — so that a runner crash after
#: the final event cannot leave browsers hanging on a stream forever.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"run.finished", "run.failed", "run.blocked", "run.cancelled"}
)

#: How long a tailing subscriber waits before re-reading the file even without a
#: notification. Bounds the damage of any missed wake-up.
_POLL_INTERVAL_S = 1.0


class EventLog:
    """Single-writer append-only log for one run.

    Thread-safety: `append` takes a threading.Lock, so it is safe from an
    executor thread. Ids are minted here and nowhere else.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._closed_marker = path.with_suffix(".closed")
        self._closed = self._closed_marker.exists()
        #: Transient: set at server shutdown to end live subscriptions without
        #: declaring the run terminal. A run parked at the gate must still be
        #: resumable after a restart.
        self._stopping = False
        self._waiters: set[asyncio.Event] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._next_id = self._scan_last_id() + 1

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _scan_last_id(self) -> int:
        """Recover the highest id already on disk. Tolerant of a torn tail."""
        highest = 0
        if not self.path.exists():
            return highest
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line).get("id")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if isinstance(value, int) and value > highest:
                        highest = value
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("could not scan %s: %s", self.path, exc)
        return highest

    def append(
        self,
        type: str,
        data: Mapping[str, Any] | None = None,
        *,
        stage: str | None = None,
    ) -> Event:
        """Append one event and return it, with its freshly minted id.

        O_APPEND + flush + fsync: the durability boundary is here, before any
        subscriber is told the event exists.
        """
        with self._lock:
            event_id = self._next_id
            self._next_id += 1
            record = {
                "id": event_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "stage": stage,
                "type": type,
                "data": dict(data or {}),
            }
            line = _encode(record)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:  # pragma: no cover - fsync unsupported
                    pass
            if type in TERMINAL_EVENT_TYPES:
                self._mark_closed()

        self._notify()
        # Parsed back from the encoded line rather than from `record`, so the
        # Event handed to an SSE subscriber is byte-for-byte what a later replay
        # off disk will produce. The live path and the replay path must not be
        # able to disagree.
        return Event.model_validate(json.loads(line))

    async def aappend(
        self,
        type: str,
        data: Mapping[str, Any] | None = None,
        *,
        stage: str | None = None,
    ) -> Event:
        """Async append: does the blocking write in a thread, then notifies."""
        self._loop = asyncio.get_running_loop()
        return await asyncio.to_thread(self.append, type, data, stage=stage)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(
        self,
        after: int = 0,
        limit: int | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        """Read events with id > `after`.

        Tolerant by construction: a truncated final line — which is exactly what
        a reader sees if it opens the file mid-write — is skipped, never raised.
        """
        if not self.path.exists():
            return []

        wanted = set(types) if types else None
        out: list[Event] = []
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn tail or a partially flushed line
                    if not isinstance(record, dict):
                        continue
                    event_id = record.get("id")
                    if not isinstance(event_id, int) or event_id <= after:
                        continue
                    if wanted is not None and record.get("type") not in wanted:
                        continue
                    try:
                        out.append(Event.model_validate(record))
                    except Exception:  # pragma: no cover - shape drift
                        continue
                    if limit is not None and len(out) >= limit:
                        break
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("could not read %s: %s", self.path, exc)
        return out

    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_closed(self) -> bool:
        """True once the run is terminal.

        Backed by a marker file next to the log, so terminality survives a
        restart and a stream opened against an old finished run ends instead of
        hanging on a heartbeat forever.
        """
        if self._closed:
            return True
        if self._closed_marker.exists():
            self._closed = True
        return self._closed

    def _mark_closed(self) -> None:
        self._closed = True
        try:
            self._closed_marker.write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - defensive
            pass

    def close(self) -> None:
        """Mark the run terminal and wake every subscriber so its stream ends."""
        with self._lock:
            self._mark_closed()
        self._notify()

    def detach(self) -> None:
        """End live subscriptions WITHOUT declaring the run terminal.

        Used at server shutdown. Writing the terminal marker here would be a
        lie: a run sitting at the stage-3 gate is not finished, and it must
        still be resumable when the server comes back.
        """
        self._stopping = True
        self._notify()

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def _notify(self) -> None:
        """Wake tailing subscribers. Best effort by design.

        `append` may be called from an executor thread, where touching an
        asyncio.Event directly is unsafe, so the wake-up is scheduled on the
        loop. If there is no loop yet, or it has closed, the poll timeout in
        `subscribe` covers the gap.
        """
        loop = self._loop
        waiters = list(self._waiters)
        if not waiters:
            return
        if loop is None:
            for waiter in waiters:
                waiter.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            for waiter in waiters:
                waiter.set()
            return
        for waiter in waiters:
            try:
                loop.call_soon_threadsafe(waiter.set)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass

    async def subscribe(self, after: int = 0) -> AsyncIterator[Event]:
        """Replay from `after` off disk, then tail until close or cancellation.

        The file is the source of truth at every step: after each wake-up the
        tail is re-read from the last id actually delivered, so a subscriber can
        never skip an event that a notification failed to announce.
        """
        self._loop = asyncio.get_running_loop()
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        cursor = after
        try:
            while True:
                for event in self.read(after=cursor):
                    cursor = event.id
                    yield event

                if self._stopping:
                    return
                if self.is_closed() and cursor >= self.last_id():
                    return

                waiter.clear()
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=_POLL_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass  # fall through and re-read; the file decides
        finally:
            self._waiters.discard(waiter)


def _encode(record: dict[str, Any]) -> str:
    """Serialize one record, degrading rather than raising.

    Narration must never be able to kill a run, so a value json cannot handle
    becomes its str()/repr() instead of an exception. `default=str` covers most
    of it; the fallback exists for the pathological cases default cannot reach,
    such as a circular reference or a dict with unhashable-repr keys.
    """
    try:
        return json.dumps(record, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        safe = dict(record)
        data = safe.get("data")
        safe["data"] = (
            {str(k): _stringify(v) for k, v in data.items()}
            if isinstance(data, Mapping) else repr(data)
        )
        try:
            return json.dumps(safe, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - last resort
            safe["data"] = {"_unserializable": repr(record.get("data"))}
            return json.dumps(safe, ensure_ascii=False, default=str)


def _stringify(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except (TypeError, ValueError):
        return repr(value)


class EventLogRegistry:
    """Memoized EventLog per run id."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self._logs: dict[str, EventLog] = {}
        self._lock = threading.Lock()

    def get(self, run_id: str) -> EventLog:
        with self._lock:
            existing = self._logs.get(run_id)
            if existing is not None:
                return existing
            path = self.runs_root / run_id / "events.jsonl"
            created = EventLog(path, run_id)
            self._logs[run_id] = created
            return created

    def drop(self, run_id: str) -> None:
        with self._lock:
            existing = self._logs.pop(run_id, None)
        if existing is not None:
            existing.close()

    def detach_all(self) -> None:
        """Wake every subscriber at shutdown so no stream is left hanging.

        Deliberately `detach`, not `close`: shutting the server down does not
        make an unfinished run terminal.
        """
        with self._lock:
            logs = list(self._logs.values())
        for item in logs:
            try:
                item.detach()
            except Exception:  # pragma: no cover - defensive
                pass
