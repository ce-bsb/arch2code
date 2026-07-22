"""Publishing the event log across container boundaries, one object per event.

The problem, precisely
----------------------
On localhost the pipeline and the SSE endpoint are the same process reading the
same ``events.jsonl``. On Code Engine they are not: an Application cannot run the
pipeline, because an HTTP connection is capped at 600 s and a pipeline takes
minutes. The pipeline moves to a Job — a different container, a different
filesystem — and the app has to read a log it cannot see.

What this module does NOT do
----------------------------
It does not replace :class:`app.eventlog.EventLog`. That class is the primary
store and its guarantee — fsync before any subscriber is told the event exists —
is worth keeping exactly as it is, especially inside a job container where the
local disk is fast and free. Rewriting it to write straight to COS would put a
network round trip on the durability path of every narration line.

Instead :class:`EventPublisher` **tails the local log and mirrors it**, one
immutable object per event, at ``runs/<id>/events/{seq:08d}.json``. The app reads
those with :class:`EventReader`, whose ``read(after=…)`` is a single
``list_objects_v2(StartAfter=…)`` because the keys are zero-padded — the same
semantics ``Last-Event-ID`` already has. So:

* the job keeps its durable local log and its existing code path;
* the app gets the same ``Event`` objects, in the same order, with the same ids;
* nothing writes the same object twice, which is the one thing object storage
  genuinely cannot tolerate.

Latency is the mirror interval (default 1 s) plus the app's poll. That is the
price of not having a push channel: Code Engine offers no webhook from a job run
back to an app — ``jobrun get``/``logs``/``events`` are all pull.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import keys as K
from .base import ObjectStore, StorageError

log = logging.getLogger("arch2code.storage.publisher")

__all__ = [
    "EventPublisher",
    "EventReader",
    "rebuild_local_log",
    "DEFAULT_INTERVAL_S",
]

#: How often the publisher looks for new lines in the local log. One second is
#: below the threshold at which a human reads a timeline as "live" and is well
#: above the per-object cost of a PUT.
DEFAULT_INTERVAL_S = 1.0


class EventPublisher:
    """Mirrors ``events.jsonl`` to the object store, incrementally.

    Single writer by construction: exactly one job run owns a run's log at a
    time. That is what makes ``put_if_absent`` sufficient on a backend whose
    conditional put is not atomic.

    Failure policy: a publish failure is logged and retried on the next tick, and
    never propagated into the pipeline. Losing the mirror degrades the live
    timeline; raising here would kill a run that is otherwise healthy. The final
    :meth:`flush` is the one that matters, and the job checks its return value.
    """

    def __init__(
        self,
        store: ObjectStore,
        run_id: str,
        log_path: Path,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        published_through: int = 0,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._path = Path(log_path)
        self._interval = max(0.1, float(interval_s))
        # A run is executed in more than one job run when it stops at the human
        # gate. The second leg must not re-publish the first leg's events under
        # the same keys, so the caller passes the highest id already in the
        # store (see rebuild_local_log).
        self._published = max(0, int(published_through))
        self._offset = 0
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------- #

    def start(self) -> "EventPublisher":
        """Begin mirroring in a daemon thread. Idempotent."""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._loop, name=f"event-publisher:{self._run_id}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *, timeout_s: float = 10.0) -> None:
        """Stop the thread after one last flush."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
            self._thread = None

    def __enter__(self) -> "EventPublisher":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
        self.flush()

    # -- the work ------------------------------------------------------------ #

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def flush(self) -> int:
        """Publish every event not yet mirrored. Returns how many were written.

        Reads from a byte offset rather than re-parsing the file, so the cost is
        proportional to what is new, not to the length of the run.
        """
        with self._lock:
            try:
                return self._flush_locked()
            except StorageError as exc:
                self._failures += 1
                log.warning(
                    "event mirror failed for %s (%d consecutive): %s",
                    self._run_id, self._failures, exc.detail,
                )
                return 0
            except OSError as exc:
                log.warning("could not read %s: %s", self._path, exc)
                return 0

    def _flush_locked(self) -> int:
        if not self._path.exists():
            return 0

        published = 0
        with self._path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            for line in handle:
                if not line.endswith("\n"):
                    # A torn tail: the writer is mid-append. Stop here and keep
                    # the offset, so the complete line is picked up next tick.
                    break
                self._offset += len(line.encode("utf-8", errors="replace"))
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                event_id = record.get("id")
                if not isinstance(event_id, int) or event_id <= self._published:
                    continue
                self._store.put_bytes(
                    K.event_key(self._run_id, event_id),
                    stripped.encode("utf-8"),
                    content_type="application/json; charset=utf-8",
                )
                self._published = event_id
                published += 1

        self._failures = 0
        return published

    def mark_closed(self, status: str) -> None:
        """Write the terminal marker so a reader can end its stream.

        Mirrors ``events.closed`` on disk. Without it an app tailing the bucket
        would heartbeat forever against a run whose job container is long gone.
        """
        try:
            self._store.put_json(
                K.closed_marker_key(self._run_id),
                {
                    "run_id": self._run_id,
                    "status": status,
                    "last_event_id": self._published,
                    "closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
        except StorageError as exc:
            log.error(
                "could not write the terminal marker for %s: %s. A client tailing "
                "this run will keep polling until it times out.",
                self._run_id, exc.detail,
            )

    @property
    def published_through(self) -> int:
        return self._published


def rebuild_local_log(store: ObjectStore, run_id: str, log_path: Path) -> int:
    """Reconstruct ``events.jsonl`` from the store. Returns the highest id found.

    This is what makes a two-leg run coherent. The gate splits a run across two
    job runs in two containers; the second one starts with an empty disk. Without
    this, its :class:`app.eventlog.EventLog` would mint ids starting at 1 again
    and the publisher would **overwrite** the first leg's event objects — the
    timeline would lose stages 1 to 3 and ``Last-Event-ID`` would replay the
    wrong events to every connected browser.

    The objects are the original lines byte for byte, so the rebuilt file is the
    file the first leg wrote, and ``EventLog._scan_last_id`` resumes numbering
    from the right place with no change to that class.

    Rebuilds only when the local file is absent or shorter, so calling it on the
    first leg, or twice, costs one listing and changes nothing.
    """
    target = Path(log_path)
    keys = [
        key
        for key in store.list_keys(K.events_prefix(run_id))
        if K.event_id_from_key(key) is not None
    ]
    if not keys:
        return 0

    highest = 0
    lines: list[str] = []
    for key in keys:  # list_keys is lexicographic, i.e. event order
        event_id = K.event_id_from_key(key)
        try:
            raw = store.get_bytes(key).decode("utf-8", errors="replace").strip()
        except StorageError:
            log.warning("event object %s could not be read while rebuilding", key)
            continue
        if raw:
            lines.append(raw)
            highest = max(highest, event_id or 0)

    existing = 0
    if target.exists():
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            existing = sum(1 for line in handle if line.strip())
    if existing >= len(lines):
        return max(highest, existing)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("rebuilt %s with %d event(s) from the previous leg", target, len(lines))
    return highest


class EventReader:
    """Reads a run's events back out of the object store.

    Deliberately shaped like the read side of :class:`app.eventlog.EventLog`
    (``read``/``last_id``/``is_closed``/``subscribe``) so that wiring it into
    ``sse.py`` is a substitution rather than a rewrite. ``sse.py`` already
    replays from ``Last-Event-ID`` and only then tails, which is exactly the
    access pattern a bucket supports well.
    """

    def __init__(
        self,
        store: ObjectStore,
        run_id: str,
        *,
        poll_interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._poll = max(0.2, float(poll_interval_s))
        self._closed = False

    @property
    def run_id(self) -> str:
        return self._run_id

    def read(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """Events with id > ``after``, in order, as plain dicts.

        Dicts rather than ``Event`` models: this module is imported by the job
        entrypoint as well, and keeping it free of the pydantic layer means the
        publisher can run in a container where a model version differs.
        """
        prefix = K.events_prefix(self._run_id)
        start_after = K.event_key(self._run_id, after) if after > 0 else None
        try:
            found = self._store.list_keys(prefix, start_after=start_after, limit=limit)
        except StorageError:
            raise
        out: list[dict[str, Any]] = []
        for key in found:
            if K.event_id_from_key(key) is None:
                continue  # the _closed.json marker, or anything else
            try:
                record = self._store.get_json(key)
            except StorageError:
                # The listing raced ahead of the object body. Stop the page here
                # rather than skipping: the next call re-reads from the same id
                # and order is never broken.
                break
            if isinstance(record, dict):
                out.append(record)
        return out

    def last_id(self) -> int:
        keys = self._store.list_keys(K.events_prefix(self._run_id))
        ids = [K.event_id_from_key(k) for k in keys]
        return max((i for i in ids if i is not None), default=0)

    def is_closed(self) -> bool:
        if self._closed:
            return True
        self._closed = self._store.exists(K.closed_marker_key(self._run_id))
        return self._closed

    async def subscribe(self, after: int = 0):
        """Replay from ``after``, then poll until the run is terminal.

        There is no notification channel across containers, so this polls. The
        interval is the honest latency floor of the split architecture; if it
        ever reads as slow, the documented alternative is Databases for Redis
        with ``XADD``/``XRANGE``, at the cost of one more service and one more
        secret.
        """
        cursor = after
        while True:
            batch = await asyncio.to_thread(self.read, cursor)
            for record in batch:
                event_id = record.get("id")
                if isinstance(event_id, int):
                    cursor = event_id
                yield record
            if await asyncio.to_thread(self.is_closed) and not batch:
                return
            await asyncio.sleep(self._poll)
