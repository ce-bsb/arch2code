"""Bob CLI knowledge: capability probing, argv construction, approval policy.

Two facts drive everything in this module.

1. **The working directory is part of the contract.** Bob resolves
   ``--chat-mode`` from the ``.bob/custom_modes.yaml`` of its *working
   directory*. Running ``--help`` from the repository root offers ten chat
   modes; running it from a directory with no ``.bob/`` offers four. So the
   probe must use ``settings.bob_cwd`` and so must every stage subprocess.

2. **The approval mode is a correctness requirement, not a preference.** Under
   ``default`` Bob excludes ``execute_command``, ``search_and_replace`` and
   ``write_to_file``; under ``auto_edit`` it excludes ``execute_command``.
   ``arch-scaffold`` therefore *cannot* write a file unless it runs with
   ``--yolo``: it exits 0 and produces nothing. See :data:`APPROVAL_BY_SLUG`.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .config import Settings, subprocess_env

__all__ = [
    "BobHelp",
    "APPROVAL_BY_SLUG",
    "probe_bob",
    "parse_chat_modes",
    "parse_approval_modes",
    "parse_version",
    "build_argv",
    "redact_argv",
    "bob_binary_exists",
]


@dataclass(frozen=True)
class BobHelp:
    """The outcome of probing the Bob binary.

    ``ok`` is ``False`` for every failure mode -- binary absent, not
    executable, timed out, non-zero exit -- and the reason is in ``stderr``.
    This dataclass never carries an exception; the health probe must be able to
    render a broken install rather than crash on it.
    """

    ok: bool
    version: str | None
    chat_modes: tuple[str, ...]
    approval_modes: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


#: Per-stage approval policy. Read the module docstring before changing this.
#:
#: A stage gets the weakest mode that still grants every tool its own exit
#: criterion depends on. Granting less does not make the stage safer; it makes it
#: fail in a way that looks like the model being slow.
#:
#: arch-scaffold MUST be yolo: `default` and `auto_edit` both exclude
#: write_to_file, so scaffolding under either exits 0 and produces no file --
#: which is exactly the failure the `artifact.missing` event exists to name.
#:
#: arch-analyst, arch-critic and arch-validator MUST be yolo for the same class
#: of reason, on the other tool: `auto_edit` excludes execute_command, and all
#: three are told to run a script. The analyst's exit criterion is literally
#: "validate_air.py exits 0 on it"; the critic must validate the AIR before
#: ruling; the validator runs run_experiment.sh. Under auto_edit the tool simply
#: is not in their tool list, so they cannot satisfy the instruction and cannot
#: report why -- they retry, burn tokens and stall until the stage times out. On
#: a small drawing the model eventually gives up and writes the artifact anyway,
#: which is why this survived a five-component napkin and only surfaced on a
#: thirteen-component diagram.
#:
#: arch-intake stays on auto_edit: it writes extraction.json and runs no script.
APPROVAL_BY_SLUG: Mapping[str, str] = {
    "arch-intake": "auto_edit",
    "arch-analyst": "yolo",
    "arch-critic": "yolo",
    "arch-validator": "yolo",
    "arch-scaffold": "yolo",
}

#: Fallback when a slug is not in the table: the safest thing Bob can do is
#: edit its own artifact and nothing else.
DEFAULT_APPROVAL_MODE = "auto_edit"

_PROMPT_PREVIEW_CHARS = 200

_VERSION_LABELLED = re.compile(
    r"(?:version|bob)[^0-9\n]{0,12}(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)",
    re.IGNORECASE,
)
_VERSION_BARE = re.compile(r"^\s*v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)\s*$")


def bob_binary_exists(settings: Settings) -> bool:
    """True when the configured binary looks launchable, without running it."""
    argv = list(settings.bob_bin)
    if not argv:
        return False
    head = argv[0]
    if os.path.sep in head:
        if not (Path(head).is_file() and os.access(head, os.X_OK)):
            return False
    elif shutil.which(head) is None:
        return False
    # "node /abs/bob.js": the script itself must exist too.
    for arg in argv[1:]:
        if arg.endswith(".js"):
            return Path(arg).is_file()
    return True


async def probe_bob(settings: Settings, *, timeout_s: float = 30.0) -> BobHelp:
    """Run ``<bob_bin> --help`` with ``cwd=settings.bob_cwd`` and parse it.

    The cwd is load-bearing: the arch2code chat modes come from the
    ``.bob/custom_modes.yaml`` of that directory. Probing from anywhere else
    reports four modes and makes a perfectly healthy install look broken.

    Never raises. A missing binary, a timeout or a non-zero exit all come back
    as ``ok=False`` with the reason in ``stderr``.
    """
    started = time.monotonic()
    argv = [*settings.bob_bin, "--help"]

    try:
        rc, stdout, stderr = await _capture(
            argv, cwd=settings.bob_cwd, env=subprocess_env(settings), timeout_s=timeout_s
        )
    except FileNotFoundError:
        return BobHelp(
            ok=False,
            version=None,
            chat_modes=(),
            approval_modes=(),
            exit_code=127,
            stdout="",
            stderr=(
                f"Executable not found: {argv[0]!r}. "
                "Set ARCH2CODE_BOB_BIN to 'bob' or to 'node /absolute/path/to/bob.js'."
            ),
            duration_ms=_elapsed_ms(started),
        )
    except asyncio.TimeoutError:
        return BobHelp(
            ok=False,
            version=None,
            chat_modes=(),
            approval_modes=(),
            exit_code=124,
            stdout="",
            stderr=f"`{' '.join(argv)}` did not finish within {timeout_s:g}s.",
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:  # noqa: BLE001 - the probe never propagates
        return BobHelp(
            ok=False,
            version=None,
            chat_modes=(),
            approval_modes=(),
            exit_code=1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            duration_ms=_elapsed_ms(started),
        )

    version = parse_version(stdout, stderr)
    if version is None:
        # `--help` does not print the version on 1.0.6; `--version` does. One
        # extra cheap call keeps the health panel honest about which build ran.
        version = await _probe_version(settings, timeout_s=min(timeout_s, 15.0))

    return BobHelp(
        ok=rc == 0,
        version=version,
        chat_modes=parse_chat_modes(stdout),
        approval_modes=parse_approval_modes(stdout),
        exit_code=rc,
        stdout=stdout,
        stderr=stderr,
        duration_ms=_elapsed_ms(started),
    )


def parse_chat_modes(help_text: str) -> tuple[str, ...]:
    """Extract the ``--chat-mode`` choices from ``--help`` output.

    Tolerant by construction: the choices list is wrapped across lines and
    right-aligned by yargs, and both the ``[choices: "a", "b"]`` form and the
    prose ``must be one of: 'a', 'b'`` form are accepted. Returns ``()`` rather
    than raising when nothing matches -- an empty tuple is what makes the
    health probe report "no chat modes discovered" instead of a stack trace.
    """
    return _parse_choices(help_text, "--chat-mode")


def parse_approval_modes(help_text: str) -> tuple[str, ...]:
    """Extract the ``--approval-mode`` choices from ``--help`` output."""
    return _parse_choices(help_text, "--approval-mode")


def parse_version(help_text: str, stderr: str = "") -> str | None:
    """Find a version string in ``--help`` / ``--version`` output.

    Bob 1.0.6 prints a bare ``1.0.6`` for ``--version`` and nothing at all in
    ``--help``, and node's punycode deprecation warning lands on stderr, so
    both streams are searched and node's own noise is skipped.
    """
    for text in (help_text or "", stderr or ""):
        for line in text.splitlines():
            if "DeprecationWarning" in line or "node:" in line:
                continue
            bare = _VERSION_BARE.match(line)
            if bare:
                return bare.group(1)
        labelled = _VERSION_LABELLED.search(text)
        if labelled:
            return labelled.group(1)
    return None


def build_argv(
    settings: Settings,
    *,
    chat_mode: str,
    prompt: str,
    approval_mode: str,
    output_format: str = "stream-json",
    include_directories: Sequence[Path] = (),
    max_coins: int | None = None,
    resume: str | None = None,
) -> list[str]:
    """Build the exact command line for one stage.

    The prompt is the **last positional argument**. ``-p/--prompt`` is
    deprecated in 1.0.6 and is never emitted.

    ``approval_mode == "yolo"`` emits the bare ``--yolo`` flag rather than
    ``--approval-mode yolo``; both are accepted by the CLI, and ``--yolo`` is
    the documented spelling for "approve everything".
    """
    argv: list[str] = [*settings.bob_bin]
    argv += ["--chat-mode", chat_mode]
    argv += ["--output-format", output_format]

    if approval_mode == "yolo":
        argv.append("--yolo")
    elif approval_mode:
        argv += ["--approval-mode", approval_mode]

    if settings.bob_accept_license:
        argv.append("--accept-license")
    if settings.bob_auth_method:
        argv += ["--auth-method", settings.bob_auth_method]

    for directory in include_directories:
        argv += ["--include-directories", str(directory)]

    coins = max_coins if max_coins is not None else settings.bob_max_coins
    if coins is not None:
        argv += ["--max-coins", str(int(coins))]

    if resume:
        argv += ["-r", resume]

    argv.append(prompt)
    return argv


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Shorten the prompt for logging and for the UI.

    The prompt is the last positional and can run to thousands of characters;
    the timeline and ``stages/<id>/argv.json`` only need enough to recognise
    it. Nothing here is a secret -- prompts carry paths, not credentials -- but
    an unreadable command line is a command line nobody reproduces by hand.
    """
    out = [str(a) for a in argv]
    if out and len(out[-1]) > _PROMPT_PREVIEW_CHARS:
        out[-1] = out[-1][:_PROMPT_PREVIEW_CHARS] + f"… (+{len(out[-1]) - _PROMPT_PREVIEW_CHARS} chars)"
    return out


def approval_for_slug(slug: str | None) -> str:
    """Approval mode for a chat-mode slug, per :data:`APPROVAL_BY_SLUG`."""
    if not slug:
        return DEFAULT_APPROVAL_MODE
    return APPROVAL_BY_SLUG.get(slug, DEFAULT_APPROVAL_MODE)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

_QUOTED = re.compile(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)["']""")


def _parse_choices(help_text: str, flag: str) -> tuple[str, ...]:
    """Return the choices advertised for ``flag``, in the order Bob lists them."""
    if not help_text:
        return ()

    # yargs right-aligns and wraps, so work on a whitespace-collapsed copy but
    # cut it at the next option so a later flag's choices cannot leak in.
    flat = re.sub(r"\s+", " ", help_text)
    start = flat.find(flag)
    if start < 0:
        return ()
    segment = flat[start + len(flag) :]

    # Bound the segment at the following option definition (" --other" or
    # " -x, --other"), otherwise a flag with no choices would swallow the next.
    bound = re.search(r"\s-{1,2}[A-Za-z][A-Za-z0-9-]*\s+(?:[A-Za-z]|\[)", segment)
    if bound:
        segment = segment[: bound.start()]

    choices_at = segment.find("[choices:")
    if choices_at >= 0:
        tail = segment[choices_at + len("[choices:") :]
        end = tail.find("]")
        if end >= 0:
            values = _QUOTED.findall(tail[:end])
            if values:
                return tuple(dict.fromkeys(values))

    # Fallback: the prose form "must be one of: 'plan', 'code', ...".
    prose_at = segment.lower().find("one of:")
    if prose_at >= 0:
        tail = segment[prose_at + len("one of:") :]
        stop = tail.find("[")
        values = _QUOTED.findall(tail if stop < 0 else tail[:stop])
        if values:
            return tuple(dict.fromkeys(values))

    return ()


async def _probe_version(settings: Settings, *, timeout_s: float) -> str | None:
    """Best-effort ``<bob_bin> --version``. Returns ``None`` on any failure."""
    try:
        _rc, stdout, stderr = await _capture(
            [*settings.bob_bin, "--version"],
            cwd=settings.bob_cwd,
            env=subprocess_env(settings),
            timeout_s=timeout_s,
        )
    except Exception:  # noqa: BLE001 - the version is a nicety, not a gate
        return None
    return parse_version(stdout, stderr)


async def _capture(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
) -> tuple[int, str, str]:
    """Run a short-lived command and capture both streams.

    stdout and stderr are read by ``communicate()``, which drains both
    concurrently -- reading one to EOF before the other is how a child
    deadlocks on a full pipe.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _terminate_quietly(proc)
        raise
    return (
        proc.returncode if proc.returncode is not None else 1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _terminate_quietly(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
