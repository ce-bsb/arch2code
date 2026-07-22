"""Async wrappers around the three deterministic helper scripts.

These scripts cost no inference, need no credentials and cannot hallucinate. They
are the honest half of stage 1 and the reason a ``.drawio`` upload must never be
routed through the vision model.

Every one of them is launched with ``settings.python_bin`` explicitly, never with
``python3``: the system interpreter on this machine is 3.9.6 and has neither
Pillow nor pydantic, so the implicit choice fails with an ``ImportError`` several
frames away from the actual cause.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, subprocess_env
from .errors import UpstreamError
from .models import CaptureManifest

CAPTURE_SCRIPT = ".bob/skills/diagram-intake/scripts/capture_diagram.py"
PARSE_DRAWIO_SCRIPT = ".bob/skills/diagram-intake/scripts/parse_drawio.py"
VALIDATE_AIR_SCRIPT = ".bob/skills/air-normalizer/scripts/validate_air.py"

#: Every script the app is allowed to launch. Nothing outside this table is
#: reachable from an HTTP request, so a path can never arrive from a client.
KNOWN_SCRIPTS: tuple[str, ...] = (
    CAPTURE_SCRIPT,
    PARSE_DRAWIO_SCRIPT,
    VALIDATE_AIR_SCRIPT,
)


@dataclass(frozen=True)
class ScriptResult:
    """One helper-script execution, complete enough to reproduce by hand."""

    script: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_event(self) -> dict[str, Any]:
        """The ``script.finished`` payload."""
        return {
            "script": Path(self.script).name,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout[-4000:],
            "stderr_tail": self.stderr[-4000:],
            "duration_ms": self.duration_ms,
        }


async def run_script(
    settings: Settings,
    script_rel: str,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
) -> ScriptResult:
    """Run ``<python_bin> <project_root/script_rel> <args...>`` and capture both pipes.

    A non-zero exit is returned, not raised: ``validate_air.py --gate`` exits 1 to
    mean "blocked", which is a finding to render rather than a crash to report. The
    only conditions that raise here are the ones that make the run impossible —
    a missing script or an interpreter that will not start.
    """
    if script_rel not in KNOWN_SCRIPTS:
        raise UpstreamError(
            "script_not_allowed",
            "Unknown helper script",
            f"'{script_rel}' is not one of {list(KNOWN_SCRIPTS)}.",
            remedy="This is a bug in the webapp; script paths are hard-coded, never user input.",
            script=script_rel,
        )

    script_path = (settings.project_root / script_rel).resolve()
    if not script_path.is_file():
        raise UpstreamError(
            "script_missing",
            "A required helper script is missing",
            f"Expected {script_path} but it does not exist.",
            remedy=(
                "Check ARCH2CODE_PROJECT_ROOT: it must point at the repository root that "
                "contains the .bob/ directory. The health report's deterministic_scripts "
                "probe reports the same thing."
            ),
            expected_path=str(script_path),
        )

    cwd.mkdir(parents=True, exist_ok=True)
    argv: tuple[str, ...] = (settings.python_bin, str(script_path), *[str(a) for a in args])
    child_env = subprocess_env(settings, env)
    # Unbuffered, so a script killed by the timeout still leaves us its output.
    child_env.setdefault("PYTHONUNBUFFERED", "1")

    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise UpstreamError(
            "python_interpreter_missing",
            "The Python interpreter could not be launched",
            f"{settings.python_bin} does not exist or is not executable ({exc}).",
            remedy=(
                "Set ARCH2CODE_PYTHON to an interpreter that has mcp, httpx, pydantic and "
                "Pillow installed. The default is /opt/anaconda3/bin/python; the system "
                "python3 is 3.9.6 and has none of them."
            ),
            python_bin=settings.python_bin,
        ) from exc
    except OSError as exc:
        raise UpstreamError(
            "script_spawn_failed",
            "Could not start the helper script",
            f"{type(exc).__name__}: {exc}",
            remedy=f"Try it by hand: `cd {cwd} && {' '.join(argv)}`",
            python_bin=settings.python_bin,
        ) from exc

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        stdout_b, stderr_b = b"", b""
        await _terminate(proc)
    duration_ms = int((time.monotonic() - started) * 1000)

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    if timed_out:
        stderr = (
            f"{stderr}\n[arch2code] killed after {timeout_s:g}s "
            f"(ARCH2CODE_STAGE_TIMEOUT_S)."
        ).strip()

    return ScriptResult(
        script=script_rel,
        argv=argv,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL after 5s. The same policy the Bob driver uses."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        with_suppressed_wait = proc.wait()
        try:
            await asyncio.wait_for(with_suppressed_wait, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover - the OS gave up on us
            pass


async def capture_diagram(
    settings: Settings, *, source: Path, run_id: str, cwd: Path
) -> tuple[CaptureManifest, ScriptResult]:
    """Normalize one artifact and read back its capture manifest.

    ``capture_diagram.py`` writes ``.arch/intake/<run_id>/`` **relative to its own
    working directory**, which is why ``cwd`` is an explicit parameter rather than a
    detail. Mode A passes ``webapp/runs/<run_id>/workspace/`` so a vision preview
    never touches the repository; Mode B passes the repo root, where the artifacts
    belong to the real audit trail.

    What the script does and why it matters: it fixes EXIF rotation (a phone photo
    arrives sideways, and a vision model reads a rotated diagram as confident
    nonsense), resizes the longest edge to 1568px, converts to PNG and records the
    sha256 of the original. The bboxes the model returns are normalized against
    *that* PNG — never against the upload.
    """
    result = await run_script(
        settings,
        CAPTURE_SCRIPT,
        [str(source), "--run", run_id],
        cwd=cwd,
        timeout_s=min(settings.stage_timeout_s, 300.0),
    )

    manifest_path = cwd / ".arch" / "intake" / run_id / "capture-manifest.json"
    if result.exit_code != 0 or not manifest_path.exists():
        raise UpstreamError(
            "capture_failed",
            "capture_diagram.py did not produce a manifest",
            (
                f"Exit code {result.exit_code}. Expected {manifest_path}.\n"
                f"stdout: {result.stdout.strip()[-800:] or '(empty)'}\n"
                f"stderr: {result.stderr.strip()[-800:] or '(empty)'}"
            ),
            remedy=(
                "Reproduce it by hand: "
                f"`cd {cwd} && {settings.python_bin} "
                f"{settings.project_root / CAPTURE_SCRIPT} {source} --run {run_id}`. "
                "A missing Pillow in ARCH2CODE_PYTHON is the usual cause."
            ),
            exit_code=result.exit_code,
            expected_path=str(manifest_path),
        )

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = CaptureManifest.model_validate(payload)
    except (OSError, ValueError) as exc:
        raise UpstreamError(
            "capture_manifest_unreadable",
            "The capture manifest could not be read",
            f"{manifest_path}: {exc}",
            remedy="Delete the run and start again; the manifest was written incompletely.",
            expected_path=str(manifest_path),
        ) from exc

    return manifest, result


async def parse_drawio(
    settings: Settings, *, source: Path, out: Path, cwd: Path
) -> tuple[dict[str, Any], ScriptResult]:
    """The deterministic path for ``.drawio``/``.xml``: exact, free, no hallucination.

    Using vision where a structured source exists is a forbidden move in this
    harness — ``arch-intake``'s instructions refuse it and the MCP server refuses it
    too, in ``_encode_image``. This is the tool the UI must steer to instead.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    result = await run_script(
        settings,
        PARSE_DRAWIO_SCRIPT,
        [str(source), "--out", str(out)],
        cwd=cwd,
        timeout_s=min(settings.stage_timeout_s, 120.0),
    )

    payload: dict[str, Any] = {}
    if out.exists():
        try:
            loaded = json.loads(out.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {"result": loaded}
        except (OSError, ValueError):
            payload = {}
    if not payload and result.stdout.strip().startswith("{"):
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            payload = {}

    if result.exit_code != 0 and not payload:
        raise UpstreamError(
            "parse_drawio_failed",
            "parse_drawio.py could not read the file",
            (
                f"Exit code {result.exit_code}.\n"
                f"stdout: {result.stdout.strip()[-800:] or '(empty)'}\n"
                f"stderr: {result.stderr.strip()[-800:] or '(empty)'}"
            ),
            remedy=(
                "A compressed .drawio (the default 'compressed XML' save option) has to be "
                "exported as uncompressed XML first: File -> Properties -> uncheck "
                "Compressed, then save."
            ),
            exit_code=result.exit_code,
        )
    return payload, result


async def validate_air(
    settings: Settings, *, air_path: Path, gate: bool, cwd: Path
) -> ScriptResult:
    """Validate an AIR document. Exit 0 = valid, exit 1 = invalid or blocked.

    A non-zero exit is a **finding to render**, not a crash to raise: with
    ``--gate`` the script is deliberately answering "this must not proceed", and
    turning that into a 500 would hide the one answer the caller asked for.
    """
    args: list[str] = [str(air_path)]
    if gate:
        args.append("--gate")
    return await run_script(
        settings,
        VALIDATE_AIR_SCRIPT,
        args,
        cwd=cwd,
        timeout_s=min(settings.stage_timeout_s, 120.0),
    )


def script_exists(settings: Settings, script_rel: str) -> bool:
    """Used by the health probe; never raises on a broken project root."""
    try:
        path = (settings.project_root / script_rel).resolve()
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False
