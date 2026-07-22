"""Subprocess driver behaviour, exercised against a fake Bob.

These are the failures that cannot be caught by reading the code:

* a child that writes a lot to **both** pipes deadlocks unless stdout and
  stderr are drained by separate, concurrent tasks;
* a pre-flight failure produces zero bytes on stdout, text on stderr and
  exit 1, so stderr has to be captured even when stdout never opens;
* a stage that ignores SIGTERM has to be killed, not waited on forever;
* output conditioned on ``isatty`` disappears under a pipe and reappears under
  a pty -- the reason both strategies exist.

The fake Bob is a small Python script so the suite needs no Bob install and
spends nothing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.bobproc import (
    STALL_AFTER_FIRST_LINE_S,
    STALL_BEFORE_FIRST_LINE_S,
    STALL_DEFAULT_S,
    ProcResult,
    run_bob,
    should_suggest_pty,
    strip_ansi,
)
from app.ndjson import StreamEvent

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fake_bob(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_bob.py"
    script.write_text(
        "import os, sys, time, json, signal\n" + body, encoding="utf-8"
    )
    return [sys.executable, str(script)]


class _Collector:
    """Captures everything the driver hands back through the callbacks."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self.stderr: list[str] = []

    async def on_event(self, ev: StreamEvent) -> None:
        self.events.append(ev)

    async def on_stderr(self, chunk: str) -> None:
        self.stderr.append(chunk)


async def _run(argv, tmp_path, **kwargs) -> tuple[ProcResult, _Collector]:
    sink = _Collector()
    result = await run_bob(
        argv,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONUNBUFFERED": "1"},
        on_event=sink.on_event,
        on_stderr=sink.on_stderr,
        **kwargs,
    )
    return result, sink


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


async def test_ndjson_lines_become_events_and_exit_zero_is_success(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "print(json.dumps({'type':'init','session':'s1'}))\n"
        "print(json.dumps({'type':'message','role':'assistant','text':'hi'}))\n"
        "print(json.dumps({'type':'result','stats':{'total_tokens':11}}))\n",
    )
    result, sink = await _run(argv, tmp_path)

    assert result.exit_code == 0
    assert result.empty_stdout is False
    assert result.stdout_lines == 3
    assert [e.type for e in sink.events] == ["init", "message", "result"]
    assert result.used_pty is False


async def test_stdout_and_stderr_are_persisted_to_their_sinks(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "print(json.dumps({'type':'init'}))\n"
        "sys.stderr.write('a warning\\n')\n",
    )
    out = tmp_path / "stdout.ndjson"
    err = tmp_path / "stderr.txt"
    result, _ = await _run(argv, tmp_path, stdout_sink=out, stderr_sink=err)

    assert result.exit_code == 0
    assert json.loads(out.read_text().strip())["type"] == "init"
    assert "a warning" in err.read_text()


async def test_unknown_and_malformed_lines_survive_the_round_trip(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "print(json.dumps({'type':'telemetry','n':1}))\n"
        "print('not json at all')\n"
        "print(json.dumps({'type':'init'}))\n",
    )
    _result, sink = await _run(argv, tmp_path)

    types = [e.type for e in sink.events]
    assert types == ["unknown", "unknown", "init"]
    assert sink.events[1].raw == "not json at all"


# --------------------------------------------------------------------------- #
# the pre-flight failure: zero stdout, text on stderr, exit 1
# --------------------------------------------------------------------------- #


async def test_preflight_failure_is_captured_from_stderr_with_empty_stdout(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "sys.stderr.write('Error: invalid API key\\n')\n" "sys.exit(1)\n",
    )
    result, sink = await _run(argv, tmp_path)

    assert result.exit_code == 1
    assert result.empty_stdout is True
    assert result.stdout_lines == 0
    assert "invalid API key" in result.stderr_text
    assert sink.events == []
    # An exit-1 with no stdout must never be mistaken for the TTY case.
    assert should_suggest_pty(result) is False


async def test_exit_zero_with_no_stdout_suggests_the_pty_retry(tmp_path):
    argv = _fake_bob(tmp_path, "sys.exit(0)\n")
    result, _ = await _run(argv, tmp_path)

    assert result.exit_code == 0
    assert result.empty_stdout is True
    assert should_suggest_pty(result) is True


# --------------------------------------------------------------------------- #
# the deadlock this driver exists to avoid
# --------------------------------------------------------------------------- #


async def test_heavy_output_on_both_pipes_does_not_deadlock(tmp_path):
    """Both pipes filled well past their kernel buffers, concurrently.

    Draining stdout to EOF before touching stderr blocks the child forever on
    a full stderr buffer. This test hangs if the pumps are ever serialised.
    """
    argv = _fake_bob(
        tmp_path,
        "line = json.dumps({'type':'message','text':'x'*400})\n"
        "for i in range(2000):\n"
        "    print(line)\n"
        "    sys.stderr.write('e'*400 + '\\n')\n",
    )
    result, sink = await asyncio.wait_for(_run(argv, tmp_path), timeout=60)

    assert result.exit_code == 0
    assert result.stdout_lines == 2000
    assert len(sink.events) == 2000
    assert result.stderr_text  # stderr was drained, not dropped


async def test_a_raising_event_callback_does_not_stall_the_drain(tmp_path):
    """A broken consumer must not stop the pipe being read.

    If it did, the child would block on a full buffer and the stage would hang
    instead of failing.
    """
    argv = _fake_bob(
        tmp_path,
        "line = json.dumps({'type':'message','text':'y'*200})\n"
        "for i in range(500):\n    print(line)\n",
    )

    async def boom(_ev: StreamEvent) -> None:
        raise RuntimeError("consumer exploded")

    result = await asyncio.wait_for(
        run_bob(
            argv,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            on_event=boom,
            on_stderr=lambda _c: asyncio.sleep(0),
            timeout_s=30,
        ),
        timeout=45,
    )
    assert result.exit_code == 0
    assert result.stdout_lines == 500
    assert "consumer exploded" in result.stderr_text


# --------------------------------------------------------------------------- #
# timeout and cancellation
# --------------------------------------------------------------------------- #


async def test_a_hung_stage_is_terminated_and_reported_as_timed_out(tmp_path):
    argv = _fake_bob(tmp_path, "time.sleep(120)\n")
    result, _ = await asyncio.wait_for(
        _run(argv, tmp_path, timeout_s=1.0), timeout=30
    )

    assert result.timed_out is True
    assert result.exit_code != 0
    assert result.signal_sent in ("SIGTERM", "SIGKILL")


async def test_a_stage_that_ignores_sigterm_is_killed(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" "time.sleep(120)\n",
    )
    result, _ = await asyncio.wait_for(
        _run(argv, tmp_path, timeout_s=0.5), timeout=40
    )

    assert result.timed_out is True
    assert result.signal_sent == "SIGKILL"


async def test_the_cancel_event_stops_the_subprocess(tmp_path):
    argv = _fake_bob(tmp_path, "time.sleep(120)\n")
    cancel = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(0.4)
        cancel.set()

    asyncio.get_running_loop().create_task(trigger())
    result, _ = await asyncio.wait_for(
        _run(argv, tmp_path, cancel=cancel, timeout_s=60), timeout=30
    )

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.exit_code != 0


async def test_task_cancellation_leaves_no_orphan_process(tmp_path):
    argv = _fake_bob(tmp_path, "time.sleep(120)\n")
    task = asyncio.ensure_future(_run(argv, tmp_path, timeout_s=60))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# the pty strategy
# --------------------------------------------------------------------------- #


async def test_tty_conditioned_output_is_invisible_under_a_pipe(tmp_path):
    """Reproduces the --list-sessions observation in miniature."""
    argv = _fake_bob(
        tmp_path,
        "if os.isatty(1):\n"
        "    print(json.dumps({'type':'init','tty':True}))\n",
    )
    result, sink = await _run(argv, tmp_path)

    assert result.exit_code == 0
    assert result.empty_stdout is True
    assert sink.events == []
    assert should_suggest_pty(result) is True


async def test_the_same_stage_produces_output_under_a_pty(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "if os.isatty(1):\n"
        "    print(json.dumps({'type':'init','tty':True}))\n",
    )
    result, sink = await asyncio.wait_for(
        _run(argv, tmp_path, use_pty=True), timeout=30
    )

    assert result.exit_code == 0
    assert result.used_pty is True
    assert result.empty_stdout is False
    assert [e.type for e in sink.events] == ["init"]
    # Already on the pty: there is nothing left to suggest.
    assert should_suggest_pty(result) is False


async def test_stderr_stays_on_its_own_pipe_under_the_pty_strategy(tmp_path):
    argv = _fake_bob(
        tmp_path,
        "print(json.dumps({'type':'init'}))\n"
        "sys.stderr.write('separate channel\\n')\n",
    )
    result, sink = await asyncio.wait_for(
        _run(argv, tmp_path, use_pty=True), timeout=30
    )

    assert "separate channel" in result.stderr_text
    assert all("separate channel" not in e.raw for e in sink.events)


async def test_ansi_decoration_is_stripped_before_parsing(tmp_path):
    argv = _fake_bob(
        tmp_path,
        r"sys.stdout.write('\x1b[32m' + json.dumps({'type':'init'}) + '\x1b[0m\n')" "\n",
    )
    _result, sink = await asyncio.wait_for(
        _run(argv, tmp_path, use_pty=True), timeout=30
    )

    assert [e.type for e in sink.events] == ["init"]


# --------------------------------------------------------------------------- #
# strip_ansi
# --------------------------------------------------------------------------- #


def test_strip_ansi_removes_colour_and_leaves_plain_text_untouched():
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert strip_ansi("plain") == "plain"
    assert strip_ansi("") == ""


def test_strip_ansi_removes_osc_title_sequences():
    assert strip_ansi("\x1b]0;a title\x07done") == "done"


# --------------------------------------------------------------------------- #
# the stall watchdog's two budgets
# --------------------------------------------------------------------------- #
#
# The bug these pin: Bob emits NOTHING on stdout while the model generates a
# tool call's arguments, and for arch-analyst that argument is the whole AIR.
# Measured on three healthy analyst runs, that silent window was 48.5 s, 69.8 s
# and 72.8 s for artifacts of 12.5 kB, 19.4 kB and 20.1 kB. A single 180 s
# watchdog killed a stage that was working. See app.bobproc for the table.


def test_the_post_progress_budget_clears_the_measured_silent_window():
    """A healthy analyst goes quiet for over a minute. The budget must cover it.

    The longest legitimate silence measured is 72.8 s. Anything close to it is
    a watchdog that kills working stages on a slower host, which is exactly the
    production failure this constant was changed for.
    """
    assert STALL_AFTER_FIRST_LINE_S >= 8 * 72.8
    assert STALL_DEFAULT_S == STALL_AFTER_FIRST_LINE_S
    # ...and still fails inside the default hard stage timeout, so a genuinely
    # dead stage reports a stall rather than running out the wall clock.
    assert STALL_AFTER_FIRST_LINE_S < 1800.0


def test_the_pre_progress_budget_is_the_shorter_of_the_two():
    """Silence before the first line never reached the model. Fail it sooner."""
    assert STALL_BEFORE_FIRST_LINE_S < STALL_AFTER_FIRST_LINE_S


async def test_a_child_that_never_speaks_is_killed_on_the_early_budget(tmp_path):
    """Zero output: the short budget applies, not the long one.

    The post-progress budget here is 60 s and the child would sleep through it.
    It dies on the 0.4 s early budget instead, because silence before the first
    line never reached the model and there is nothing to wait for.
    """
    argv = _fake_bob(
        tmp_path,
        "signal.signal(signal.SIGTERM, signal.SIG_DFL)\ntime.sleep(60)\n",
    )
    result, sink = await asyncio.wait_for(
        _run(
            argv,
            tmp_path,
            stall_s=60.0,
            stall_before_first_line_s=0.4,
            timeout_s=30,
        ),
        timeout=30,
    )

    assert result.timed_out
    assert result.stdout_lines == 0
    assert sink.events == []


async def test_the_first_line_switches_the_watchdog_to_the_longer_budget(tmp_path):
    """One line proves Bob reached the model; the silence after it is tolerated.

    This is the production bug in miniature. The child speaks once, then goes
    quiet for far longer than the early budget allows -- exactly what the
    analyst does while the model generates the AIR -- and then finishes. A
    single-budget watchdog set to the early value kills it; the two-budget one
    lets it work.
    """
    argv = _fake_bob(
        tmp_path,
        "print(json.dumps({'type':'init','session':'s1'}), flush=True)\n"
        "time.sleep(1.5)\n"
        "print(json.dumps({'type':'result','status':'success'}), flush=True)\n",
    )
    result, sink = await asyncio.wait_for(
        _run(
            argv,
            tmp_path,
            stall_s=20.0,
            stall_before_first_line_s=0.4,
            timeout_s=30,
        ),
        timeout=30,
    )

    assert not result.timed_out
    assert result.exit_code == 0
    assert [e.type for e in sink.events] == ["init", "result"]


async def test_the_early_budget_never_outlives_the_post_progress_one(tmp_path):
    """A caller asking for an aggressive watchdog gets one from second zero.

    ``stall_s`` shorter than the early budget must clamp it, or the default
    120 s would silently override a test (or an operator) asking for 0.4 s.
    """
    argv = _fake_bob(
        tmp_path,
        "signal.signal(signal.SIGTERM, signal.SIG_DFL)\ntime.sleep(60)\n",
    )
    result, _sink = await asyncio.wait_for(
        _run(argv, tmp_path, stall_s=0.4, timeout_s=30), timeout=30
    )

    assert result.timed_out
    assert result.duration_ms < 20_000
