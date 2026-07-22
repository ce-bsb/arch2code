"""The Bob subprocess driver: pipe strategy, pty fallback, hard timeouts.

Invariants this module exists to hold. Every one of them is a real failure that
was observed or is provable from the CLI's behaviour:

* **stdout and stderr are drained by separate, concurrent tasks.** A pipe that
  is not drained fills its kernel buffer and the child blocks forever on
  ``write``. Reading stdout to EOF *before* touching stderr is that deadlock.
* **The exit code is the only source of truth.** Pre-flight failures (invalid
  auth, unaccepted licence, unknown chat-mode slug) produce *zero bytes* on
  stdout, plain text on stderr and exit 1. The NDJSON is not an error channel.
* **stderr is captured for every stage regardless of outcome**, because on the
  failure above it is the only evidence that exists.
* **Timeout and cancellation escalate**: SIGTERM, wait 5s, SIGKILL. A stage that
  ignores SIGTERM does not hold the single pipeline slot forever.
* **``empty_stdout`` is reported regardless of the exit code.** Exit 0 with no
  output is the TTY-conditioned-path symptom: ``--list-sessions`` prints 464
  bytes under a pty and 0 under a pipe, so at least one Bob output path is
  conditioned on ``isatty``. Whether stream-json is one of them is unverified,
  which is exactly why both strategies exist behind one signature.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .ndjson import LineAccumulator, StreamEvent, normalize_line

__all__ = [
    "ProcResult",
    "OnEvent",
    "OnStderr",
    "run_bob",
    "strip_ansi",
    "should_suggest_pty",
    "PTY_REMEDY",
    "STALL_DEFAULT_S",
    "STALL_BEFORE_FIRST_LINE_S",
    "STALL_AFTER_FIRST_LINE_S",
]

#: Grace period between SIGTERM and SIGKILL.
TERM_GRACE_S = 5.0
#: How long to keep draining after the child is reaped before giving up.
DRAIN_GRACE_S = 5.0

#: Kill a stage that has emitted nothing for this long, even with time left on
#: the hard timeout.
#:
#: THE PREMISE THAT USED TO BE HERE WAS WRONG, AND IT KILLED HEALTHY STAGES.
#: It said "Bob streams continuously while it works, so a long silence is no
#: progress". Bob does not. It streams the model's *visible text* one token per
#: line, and then emits **nothing at all** while the model generates a tool
#: call's arguments: the ``tool_use`` line appears only once the whole argument
#: object is complete. For ``write_to_file`` that argument *is* the artifact.
#:
#: Measured on this machine, three independent healthy arch-analyst runs, each
#: with a single silent window covering the whole ``write_to_file`` generation:
#:
#:   ==================================  ========  =========  ==========
#:   run                                 artifact   silence    chars/s
#:   ==================================  ========  =========  ==========
#:   20260722-0528-modeb2 (5 components)  12,502 c   48.5 s     258
#:   local A1 (13 components)             19,367 c   69.8 s     278
#:   local A2 (13 components)             20,063 c   72.8 s     276
#:   ==================================  ========  =========  ==========
#:
#: So the legitimate silent window is linear in the size of the artifact the
#: model is writing, at roughly 270 chars/s here. The production stall that
#: this constant used to cause -- one ``read_file``, then 180 s of silence, then
#: SIGTERM -- is the same window at about a third of that throughput. The stage
#: was working. We killed it.
#:
#: Nothing on stdout distinguishes "the model started a tool block" from "the
#: backend stopped answering": both look like the stream going quiet after a
#: token that carries no marker. What stdout *does* distinguish is whether Bob
#: ever got as far as talking to the model at all, so the watchdog uses two
#: budgets instead of one.
#:
#: Before the first line: authentication, the license check and the MCP
#: handshake. Healthy runs reach the ``init`` line in about 5 s; the known
#: HTTP 504 from the IBM authorization service dies here in ~12 s with zero
#: lines. Silence this early is a broken pre-flight, never a working model.
STALL_BEFORE_FIRST_LINE_S = 120.0
#: After the first line: Bob has demonstrably reached the model, so a silent
#: window is most likely a tool call being generated. This has to clear the
#: worst legitimate window by a wide margin -- 8x the longest one measured
#: above -- while still failing well inside the hard stage timeout (1200 s by
#: default) with an accurate reason instead of running out the wall clock.
STALL_AFTER_FIRST_LINE_S = 600.0
#: Kept as the name the rest of the code and the tests import. It is the
#: post-progress budget, because that is the one that governs almost every
#: second of a stage's life.
STALL_DEFAULT_S = STALL_AFTER_FIRST_LINE_S
#: Cap on the stderr text kept in memory (the full text still reaches the sink).
STDERR_TAIL_BYTES = 64 * 1024

_READ_CHUNK = 65536

#: The remedy string carried by the ``bob.empty_output`` event. Stated once
#: here so the API, the timeline and the docs cannot drift apart.
PTY_REMEDY = (
    "Re-run with ARCH2CODE_BOB_PTY=1: at least one Bob output path is "
    "conditioned on a TTY (--list-sessions prints 464 bytes under a PTY and 0 "
    "under a pipe)."
)

_ANSI_RE = re.compile(
    r"""
    \x1B\][^\x07\x1B]*(?:\x07|\x1B\\)   # OSC ... BEL | ST
    | \x1B[@-Z\\-_]                      # single-character escapes
    | \x1B\[[0-?]*[ -/]*[@-~]            # CSI ... final byte
    | \x1B[PX^_][^\x1B]*\x1B\\           # DCS/SOS/PM/APC ... ST
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class ProcResult:
    """The outcome of one stage subprocess.

    ``exit_code`` decides the stage's fate. Everything else is evidence for the
    human: ``stderr_text`` explains a pre-flight failure, ``empty_stdout``
    flags the TTY case, ``timed_out`` distinguishes a hung stage from a crash.
    """

    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stdout_lines: int
    stderr_text: str
    used_pty: bool
    timed_out: bool
    empty_stdout: bool
    #: "SIGTERM" / "SIGKILL" when the driver had to escalate, else None.
    signal_sent: str | None = None
    #: Set when the driver stopped because the cancel Event was raised.
    cancelled: bool = False


OnEvent = Callable[[StreamEvent], Awaitable[None]]
OnStderr = Callable[[str], Awaitable[None]]


async def run_bob(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    on_event: OnEvent,
    on_stderr: OnStderr,
    stdout_sink: Path | None = None,
    stderr_sink: Path | None = None,
    use_pty: bool = False,
    timeout_s: float | None = None,
    stall_s: float | None = STALL_DEFAULT_S,
    stall_before_first_line_s: float | None = STALL_BEFORE_FIRST_LINE_S,
    cancel: asyncio.Event | None = None,
) -> ProcResult:
    """Run one Bob invocation and stream its output through the callbacks.

    Args:
        argv: The exact command line, as built by :func:`bobcli.build_argv`.
        cwd: Working directory. For a pipeline stage this is always the project
            root, because Bob discovers its chat modes from that directory's
            ``.bob/custom_modes.yaml``.
        env: The complete child environment.
        on_event: Awaited once per parsed stdout line.
        on_stderr: Awaited once per stderr chunk.
        stdout_sink: Optional file receiving every stdout line verbatim.
        stderr_sink: Optional file receiving stderr verbatim.
        use_pty: Select the pty strategy instead of pipes.
        timeout_s: Hard wall-clock limit; ``None`` means no limit.
        stall_s: Silence allowed once Bob has emitted at least one line.
        stall_before_first_line_s: Silence allowed while Bob has emitted
            nothing at all. Clamped to ``stall_s``; see
            :data:`STALL_BEFORE_FIRST_LINE_S` for why the two differ.
        cancel: Setting this event terminates the child.

    Returns:
        A :class:`ProcResult`. This function does not raise on a failed stage --
        a non-zero exit is data, not an exception. It re-raises
        ``asyncio.CancelledError`` after killing the child, so task
        cancellation can never leave an orphan Bob process behind.
    """
    strategy = _run_pty if use_pty else _run_pipe
    return await strategy(
        argv,
        cwd=cwd,
        env=env,
        on_event=on_event,
        on_stderr=on_stderr,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
        timeout_s=timeout_s,
        stall_s=stall_s,
        stall_before_first_line_s=stall_before_first_line_s,
        cancel=cancel,
    )


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (colour, cursor moves, OSC titles).

    Required for the pty strategy, where Bob believes it is talking to a
    terminal and may decorate its output; applied on the pipe path too, since
    stripping is harmless for clean JSON and cheap insurance if it is not.
    """
    if not text or "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


def should_suggest_pty(result: ProcResult) -> bool:
    """True when this looks like the TTY-conditioned-output failure.

    Exit 0 with nothing on stdout under the pipe strategy: the stage "succeeded"
    and narrated nothing. The UI offers a one-click pty re-run instead of
    showing a silent success.
    """
    return result.exit_code == 0 and result.empty_stdout and not result.used_pty


# --------------------------------------------------------------------------- #
# pipe strategy (default)
# --------------------------------------------------------------------------- #


async def _run_pipe(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    on_event: OnEvent,
    on_stderr: OnStderr,
    stdout_sink: Path | None,
    stderr_sink: Path | None,
    timeout_s: float | None,
    stall_s: float | None,
    stall_before_first_line_s: float | None,
    cancel: asyncio.Event | None,
) -> ProcResult:
    started = time.monotonic()
    counters = _Counters()

    proc = await asyncio.create_subprocess_exec(
        *[str(a) for a in argv],
        cwd=str(cwd),
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Both pumps start before we wait on the process. This ordering is the
    # whole point: a child writing to a pipe nobody reads blocks forever.
    out_task = asyncio.create_task(
        _pump_stream_reader(proc.stdout, on_event, stdout_sink, counters)
    )
    err_task = asyncio.create_task(
        _pump_stderr_reader(proc.stderr, on_stderr, stderr_sink, counters)
    )

    try:
        timed_out, cancelled, signal_sent = await _await_exit(
            proc,
            timeout_s=timeout_s,
            cancel=cancel,
            counters=counters,
            stall_s=stall_s,
            stall_before_first_line_s=stall_before_first_line_s,
        )
    except asyncio.CancelledError:
        await _kill_now(proc)
        await _finish_pumps(out_task, err_task)
        raise

    await _finish_pumps(out_task, err_task)

    return _result(
        proc=proc,
        counters=counters,
        started=started,
        used_pty=False,
        timed_out=timed_out,
        cancelled=cancelled,
        signal_sent=signal_sent,
    )


# --------------------------------------------------------------------------- #
# pty strategy (fallback)
# --------------------------------------------------------------------------- #


async def _run_pty(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    on_event: OnEvent,
    on_stderr: OnStderr,
    stdout_sink: Path | None,
    stderr_sink: Path | None,
    timeout_s: float | None,
    stall_s: float | None,
    stall_before_first_line_s: float | None,
    cancel: asyncio.Event | None,
) -> ProcResult:
    """Run Bob with stdout attached to a pseudo-terminal.

    stderr stays on a plain pipe so pre-flight text is never interleaved with
    the NDJSON: keeping the two channels apart is what lets an exit-1 with an
    empty stdout still produce a readable diagnosis.
    """
    import pty  # stdlib; imported lazily so the module loads on any platform
    import tty

    started = time.monotonic()
    counters = _Counters()

    master_fd, slave_fd = pty.openpty()
    try:
        # Raw mode: no echo and no NL -> CRNL translation on the slave side.
        # The accumulator tolerates \r\n anyway, but not translating keeps the
        # bytes we persist identical to the bytes Bob wrote.
        with contextlib.suppress(Exception):
            tty.setraw(slave_fd)

        child_env = dict(env)
        child_env.setdefault("TERM", "xterm-256color")

        proc = await asyncio.create_subprocess_exec(
            *[str(a) for a in argv],
            cwd=str(cwd),
            env=child_env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise

    # The parent must drop the slave, otherwise the master never sees EOF.
    os.close(slave_fd)

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    os.set_blocking(master_fd, False)

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # EIO on the master is how a pty reports "the child closed it".
            data = b""
        if not data:
            with contextlib.suppress(Exception):
                loop.remove_reader(master_fd)
            queue.put_nowait(None)
            return
        queue.put_nowait(data)

    loop.add_reader(master_fd, _on_readable)

    out_task = asyncio.create_task(
        _pump_queue(queue, on_event, stdout_sink, counters)
    )
    err_task = asyncio.create_task(
        _pump_stderr_reader(proc.stderr, on_stderr, stderr_sink, counters)
    )

    try:
        try:
            timed_out, cancelled, signal_sent = await _await_exit(
                proc,
                timeout_s=timeout_s,
                cancel=cancel,
                counters=counters,
                stall_s=stall_s,
                stall_before_first_line_s=stall_before_first_line_s,
            )
        except asyncio.CancelledError:
            await _kill_now(proc)
            raise
    finally:
        # Give the master a bounded chance to surface anything still buffered,
        # then force the pump to end. A pty that never signals EOF must not
        # hang the run.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(out_task), timeout=DRAIN_GRACE_S)
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        queue.put_nowait(None)
        await _finish_pumps(out_task, err_task)
        with contextlib.suppress(OSError):
            os.close(master_fd)

    return _result(
        proc=proc,
        counters=counters,
        started=started,
        used_pty=True,
        timed_out=timed_out,
        cancelled=cancelled,
        signal_sent=signal_sent,
    )


# --------------------------------------------------------------------------- #
# pumps
# --------------------------------------------------------------------------- #


class _Counters:
    """Mutable tallies shared between the pumps and the result builder."""

    def __init__(self) -> None:
        self.stdout_bytes = 0
        self.stdout_lines = 0
        #: Monotonic timestamp of the last line Bob emitted. The stall watchdog
        #: reads it; the pump writes it. A hard timeout answers "is this taking
        #: too long"; this answers "has it stopped talking", which is a
        #: different failure and the one that looks like a spinner forever.
        self.last_output_at = time.monotonic()
        self.stderr_bytes = 0
        self.stderr_chunks: list[str] = []
        self.callback_errors: list[str] = []

    def stderr_text(self) -> str:
        text = "".join(self.stderr_chunks)
        if len(text) > STDERR_TAIL_BYTES:
            return text[-STDERR_TAIL_BYTES:]
        return text

    def note_stderr(self, text: str) -> None:
        self.stderr_bytes += len(text.encode("utf-8", errors="replace"))
        self.stderr_chunks.append(text)
        # Keep the in-memory tail bounded without losing recency.
        if len(self.stderr_chunks) > 512:
            joined = "".join(self.stderr_chunks)
            self.stderr_chunks = [joined[-STDERR_TAIL_BYTES:]]


async def _pump_stream_reader(
    reader: asyncio.StreamReader | None,
    on_event: OnEvent,
    sink: Path | None,
    counters: _Counters,
) -> None:
    """Drain a stdout pipe into normalized events until EOF."""
    if reader is None:
        return
    acc = LineAccumulator()
    with _open_sink(sink) as fh:
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    break
                counters.stdout_bytes += len(chunk)
                for line in acc.feed(chunk):
                    await _dispatch_line(line, on_event, fh, counters)
            for line in acc.flush():
                await _dispatch_line(line, on_event, fh, counters)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken pump must not deadlock
            counters.callback_errors.append(f"stdout pump: {type(exc).__name__}: {exc}")


async def _pump_queue(
    queue: "asyncio.Queue[bytes | None]",
    on_event: OnEvent,
    sink: Path | None,
    counters: _Counters,
) -> None:
    """Drain the pty master queue into normalized events until the sentinel."""
    acc = LineAccumulator()
    with _open_sink(sink) as fh:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                counters.stdout_bytes += len(chunk)
                for line in acc.feed(chunk):
                    await _dispatch_line(line, on_event, fh, counters)
            for line in acc.flush():
                await _dispatch_line(line, on_event, fh, counters)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            counters.callback_errors.append(f"pty pump: {type(exc).__name__}: {exc}")


async def _pump_stderr_reader(
    reader: asyncio.StreamReader | None,
    on_stderr: OnStderr,
    sink: Path | None,
    counters: _Counters,
) -> None:
    """Drain stderr independently of stdout.

    This is the only channel that carries pre-flight failures, and it must be
    read even when stdout produces nothing at all.
    """
    if reader is None:
        return
    with _open_sink(sink) as fh:
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                counters.note_stderr(text)
                if fh is not None:
                    fh.write(chunk)
                    fh.flush()
                try:
                    await on_stderr(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    counters.callback_errors.append(
                        f"on_stderr: {type(exc).__name__}: {exc}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            counters.callback_errors.append(f"stderr pump: {type(exc).__name__}: {exc}")


async def _dispatch_line(
    line: str,
    on_event: OnEvent,
    fh: Any,
    counters: _Counters,
) -> None:
    """Normalize one line, persist it and hand it to the consumer.

    A consumer that raises is recorded and ignored: letting it kill the pump
    would stop the pipe being drained, which is precisely how the child ends up
    blocked on a full buffer.
    """
    clean = strip_ansi(line)
    event = normalize_line(clean)
    if event is None:
        return
    counters.stdout_lines += 1
    counters.last_output_at = time.monotonic()
    if fh is not None:
        fh.write(clean.encode("utf-8", errors="replace") + b"\n")
        fh.flush()
    try:
        await on_event(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        counters.callback_errors.append(f"on_event: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# process lifecycle
# --------------------------------------------------------------------------- #


async def _await_exit(
    proc: asyncio.subprocess.Process,
    *,
    timeout_s: float | None,
    stall_s: float | None,
    cancel: asyncio.Event | None,
    counters: "_Counters | None" = None,
    stall_before_first_line_s: float | None = STALL_BEFORE_FIRST_LINE_S,
) -> tuple[bool, bool, str | None]:
    """Wait for exit, a timeout, a stall or a cancellation, escalating if needed.

    ``stall_s`` is the watchdog: if Bob emits no line for that long it is killed
    even though the hard timeout has not been reached.

    The budget is **not** constant across the life of the process, because the
    same amount of silence means two different things depending on whether Bob
    has produced a line yet. See :data:`STALL_BEFORE_FIRST_LINE_S` for the
    measurements. Before the first line, silence is a pre-flight that never
    reached the model, and waiting the full post-progress budget out just makes
    an authentication failure take ten minutes to report. After the first line,
    silence is usually the model generating a tool call's arguments -- a window
    during which Bob emits nothing by design -- and killing into it destroys a
    stage that was working.

    The pre-first-line budget is clamped to ``stall_s`` so that a caller asking
    for an aggressively short watchdog still gets one from the first second.

    Returns ``(timed_out, cancelled, signal_sent)``.
    """
    waiter: asyncio.Task[int] = asyncio.ensure_future(proc.wait())
    watchers: list[asyncio.Future[Any]] = [waiter]
    cancel_task: asyncio.Task[bool] | None = None
    if cancel is not None:
        cancel_task = asyncio.ensure_future(cancel.wait())
        watchers.append(cancel_task)

    watchdog = stall_s if (counters is not None and stall_s and stall_s > 0) else None
    early = (
        stall_before_first_line_s
        if stall_before_first_line_s and stall_before_first_line_s > 0
        else None
    )
    early_watchdog = (
        min(early, watchdog)
        if (watchdog is not None and early is not None)
        else watchdog
    )
    deadline = (
        time.monotonic() + timeout_s if timeout_s and timeout_s > 0 else None
    )

    def budget() -> float | None:
        """The silence allowance that applies right now."""
        if watchdog is None:
            return None
        if counters is not None and counters.stdout_lines == 0:
            return early_watchdog
        return watchdog

    try:
        while True:
            current = budget()
            if deadline is None:
                slice_s = current
            elif current is None:
                slice_s = max(0.0, deadline - time.monotonic())
            else:
                slice_s = min(current, max(0.0, deadline - time.monotonic()))

            done, _pending = await asyncio.wait(
                watchers,
                timeout=slice_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done:
                return False, False, None
            if cancel_task is not None and cancel_task in done:
                signal_sent = await _escalate(proc, waiter)
                return False, True, signal_sent

            now = time.monotonic()
            if deadline is not None and now >= deadline:
                signal_sent = await _escalate(proc, waiter)
                return True, False, signal_sent
            current = budget()
            if current is not None and (now - counters.last_output_at) >= current:
                signal_sent = await _escalate(proc, waiter)
                return True, False, signal_sent
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_task
        if not waiter.done():
            with contextlib.suppress(Exception):
                await waiter


async def _escalate(
    proc: asyncio.subprocess.Process, waiter: "asyncio.Future[int]"
) -> str | None:
    """SIGTERM, wait :data:`TERM_GRACE_S`, then SIGKILL."""
    if proc.returncode is not None:
        return None
    try:
        proc.terminate()
    except ProcessLookupError:
        return None
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout=TERM_GRACE_S)
        return "SIGTERM"
    except asyncio.TimeoutError:
        pass
    except Exception:  # noqa: BLE001
        return "SIGTERM"

    try:
        proc.kill()
    except ProcessLookupError:
        return "SIGTERM"
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=TERM_GRACE_S)
    return "SIGKILL"


async def _kill_now(proc: asyncio.subprocess.Process) -> None:
    """Unconditional teardown used when our own task is cancelled."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_S)


async def _finish_pumps(*tasks: asyncio.Task[Any]) -> None:
    """Let the pumps reach EOF, then cancel whatever is still running."""
    pending = [t for t in tasks if not t.done()]
    if pending:
        with contextlib.suppress(Exception):
            await asyncio.wait(pending, timeout=DRAIN_GRACE_S)
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _result(
    *,
    proc: asyncio.subprocess.Process,
    counters: _Counters,
    started: float,
    used_pty: bool,
    timed_out: bool,
    cancelled: bool,
    signal_sent: str | None,
) -> ProcResult:
    exit_code = proc.returncode
    if exit_code is None:
        # Reaping failed; report it as killed rather than inventing a success.
        exit_code = -9
    stderr_text = counters.stderr_text()
    if counters.callback_errors:
        stderr_text += "\n" + "\n".join(
            f"[webapp] {line}" for line in counters.callback_errors
        )
    return ProcResult(
        exit_code=exit_code,
        duration_ms=int((time.monotonic() - started) * 1000),
        stdout_bytes=counters.stdout_bytes,
        stdout_lines=counters.stdout_lines,
        stderr_text=stderr_text,
        used_pty=used_pty,
        timed_out=timed_out,
        empty_stdout=counters.stdout_bytes == 0,
        signal_sent=signal_sent,
        cancelled=cancelled,
    )


@contextlib.contextmanager
def _open_sink(sink: Path | None):
    """Open a sink file for append, or yield ``None`` when there is none."""
    if sink is None:
        yield None
        return
    sink.parent.mkdir(parents=True, exist_ok=True)
    fh = open(sink, "ab")
    try:
        yield fh
    finally:
        with contextlib.suppress(Exception):
            fh.close()
