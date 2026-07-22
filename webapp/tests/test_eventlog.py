"""Tests for the append-only event log.

The log is the primary store, not a cache, so the properties tested here are the
ones the whole app rests on: ids are monotonic and minted in exactly one place,
a reader tolerates a torn tail, and a tailing subscriber sees an event that was
appended from another thread.

Run with:  /opt/anaconda3/bin/python -m pytest webapp/tests -q
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Awaitable, Callable

from app.eventlog import EventLog, EventLogRegistry


def make_log(tmp_path, run_id: str = "20260721-1200-test") -> EventLog:
    return EventLog(tmp_path / run_id / "events.jsonl", run_id)


def run_async(main: Callable[[], Awaitable[Any]]) -> Any:
    """Drive one coroutine to completion.

    Deliberately not pytest-asyncio: the app depends on nothing beyond
    requirements.txt, and its test suite must run on the same machine with no
    extra install and no network.
    """
    return asyncio.run(main())


# ----------------------------------------------------------------------
# Ids
# ----------------------------------------------------------------------


def test_ids_are_monotonic_from_one(tmp_path):
    log = make_log(tmp_path)
    ids = [log.append("log", {"n": i}).id for i in range(5)]
    assert ids == [1, 2, 3, 4, 5]
    assert log.last_id() == 5


def test_ids_resume_after_reopen(tmp_path):
    """A restart must not reuse ids: SSE clients dedupe by id."""
    first = make_log(tmp_path)
    for _ in range(3):
        first.append("log")

    second = EventLog(first.path, first.run_id)
    assert second.append("log").id == 4


def test_ids_are_unique_under_concurrent_appends(tmp_path):
    """append() is called from executor threads; the id counter is locked."""
    log = make_log(tmp_path)
    seen: list[int] = []
    lock = threading.Lock()

    def worker(offset: int) -> None:
        for i in range(20):
            event = log.append("log", {"worker": offset, "i": i})
            with lock:
                seen.append(event.id)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 80
    assert sorted(seen) == list(range(1, 81))
    # Every line landed on disk exactly once, none interleaved mid-write.
    assert len(log.read()) == 80


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def test_replay_after_n(tmp_path):
    log = make_log(tmp_path)
    for i in range(10):
        log.append("log", {"n": i})

    assert [e.id for e in log.read(after=7)] == [8, 9, 10]
    assert [e.id for e in log.read(after=0, limit=3)] == [1, 2, 3]
    assert log.read(after=10) == []


def test_read_filters_by_type(tmp_path):
    log = make_log(tmp_path)
    log.append("bob.message")
    log.append("bob.stderr")
    log.append("bob.message")

    assert [e.type for e in log.read(types={"bob.message"})] == [
        "bob.message", "bob.message",
    ]


def test_truncated_final_line_is_skipped_not_raised(tmp_path):
    """Exactly what a reader sees when it opens the file mid-write."""
    log = make_log(tmp_path)
    log.append("log", {"n": 1})
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": 2, "type": "log", "da')  # torn, no newline

    events = log.read()
    assert [e.id for e in events] == [1]


def test_non_json_noise_is_skipped(tmp_path):
    log = make_log(tmp_path)
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json at all\n\n")
    log.append("log", {"n": 1})

    assert [e.type for e in log.read()] == ["log"]


def test_unserializable_data_degrades_instead_of_raising(tmp_path):
    """Narration must never be able to kill a run."""
    log = make_log(tmp_path)
    event = log.append("log", {"obj": object(), "ok": 1})

    assert event.data["ok"] == 1
    assert isinstance(event.data["obj"], str)
    # And it is still valid JSON on disk.
    line = log.path.read_text(encoding="utf-8").strip()
    assert json.loads(line)["data"]["ok"] == 1


def test_circular_reference_does_not_raise(tmp_path):
    """default=str cannot save a self-referencing dict; the fallback must."""
    log = make_log(tmp_path)
    loop: dict[str, Any] = {"name": "cycle"}
    loop["self"] = loop

    event = log.append("log", {"loop": loop})

    assert log.read()[0].id == event.id
    assert json.loads(log.path.read_text(encoding="utf-8").strip())["type"] == "log"


def test_returned_event_equals_the_replayed_one(tmp_path):
    """The SSE path hands out the returned Event while the replay path reads
    the file. They must be indistinguishable, or a reconnect would show the
    same event differently."""
    log = make_log(tmp_path)
    live = log.append("bob.result", {"stats": {"total_tokens": 12}}, stage="intake")
    replayed = log.read()[0]

    assert live.model_dump(mode="json") == replayed.model_dump(mode="json")


def test_read_of_missing_file_is_empty(tmp_path):
    log = make_log(tmp_path)
    assert log.read() == []
    assert log.last_id() == 0


# ----------------------------------------------------------------------
# Subscription
# ----------------------------------------------------------------------


def test_subscribe_replays_then_tails(tmp_path):
    log = make_log(tmp_path)
    for i in range(3):
        log.append("log", {"n": i})

    received: list[int] = []

    async def main() -> None:
        async def consume() -> None:
            async for event in log.subscribe(after=1):
                received.append(event.id)
                if event.type == "run.finished":
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await log.aappend("log", {"n": 3})
        await log.aappend("run.finished", {"status": "succeeded"})
        await asyncio.wait_for(task, timeout=5)

    run_async(main)
    assert received == [2, 3, 4, 5]


def test_subscription_ends_when_the_log_closes(tmp_path):
    """A terminal run must let every open stream finish by itself."""
    log = make_log(tmp_path)
    log.append("log")

    received: list[int] = []

    async def main() -> None:
        async def consume() -> None:
            async for event in log.subscribe(after=0):
                received.append(event.id)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        log.close()
        await asyncio.wait_for(task, timeout=5)

    run_async(main)
    assert received == [1]
    assert log.is_closed()


def test_terminal_event_closes_the_log(tmp_path):
    """Belt and braces: a runner that crashes after the final event must not
    leave browsers hanging on an open stream."""
    log = make_log(tmp_path)
    assert not log.is_closed()
    log.append("run.failed", {"stage": "intake"})
    assert log.is_closed()


def test_append_from_a_thread_wakes_a_tailing_subscriber(tmp_path):
    """append() runs in an executor thread; the wake-up must cross back."""
    log = make_log(tmp_path)
    received: list[int] = []

    async def main() -> None:
        async def consume() -> None:
            async for event in log.subscribe(after=0):
                received.append(event.id)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await asyncio.to_thread(log.append, "log", {"from": "thread"})
        await asyncio.wait_for(task, timeout=5)

    run_async(main)
    assert received == [1]


def test_detach_ends_streams_without_marking_terminal(tmp_path):
    """Server shutdown must not declare an unfinished run finished: a run parked
    at the stage-3 gate has to stay resumable."""
    log = make_log(tmp_path)
    log.append("run.awaiting_input", {"stage": "critic"})

    async def main() -> None:
        async def consume() -> None:
            async for _ in log.subscribe(after=0):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        log.detach()
        await asyncio.wait_for(task, timeout=5)

    run_async(main)
    assert not log.is_closed()
    assert not log.path.with_suffix(".closed").exists()


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


def test_registry_memoizes_per_run(tmp_path):
    registry = EventLogRegistry(tmp_path)
    assert registry.get("a") is registry.get("a")
    assert registry.get("a") is not registry.get("b")


def test_registry_drop_closes(tmp_path):
    registry = EventLogRegistry(tmp_path)
    log = registry.get("20260721-1200-test")
    registry.drop("20260721-1200-test")

    assert log.is_closed()
    assert registry.get("20260721-1200-test") is not log
