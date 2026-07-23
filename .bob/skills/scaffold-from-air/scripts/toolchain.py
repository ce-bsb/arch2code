#!/usr/bin/env python3
"""
toolchain.py — which offline gates actually exist on THIS machine.

The Terraform model: configuration validation runs without provider
configuration, offline. Translated here — the generated code is checked without
a mainframe, without a watsonx Orchestrate tenant and without a cluster.

The part that matters is the degradation. A check that is skipped in silence
reads exactly like a check that passed, and that is how a target ends up
claiming "validated" for COBOL that has never seen a compiler. Every requirement
that is missing produces a sentence saying what is no longer being checked and
the one command that fixes it.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from importlib import util as import_util
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from profiles import Profile, SKILL_ROOT
except ImportError:  # pragma: no cover
    from .profiles import Profile, SKILL_ROOT  # type: ignore

_FILES_TOKEN = re.compile(r"\{files:([^}]+)\}")


@dataclass
class Requirement:
    kind: str            # binary | python_module
    name: str
    present: bool
    location: Optional[str] = None


@dataclass
class CheckPlan:
    name: str
    cmd: str
    confidence: str
    blocking: bool
    requirements: List[Requirement] = field(default_factory=list)
    degrade: Optional[str] = None

    @property
    def runnable(self) -> bool:
        return all(r.present for r in self.requirements)

    @property
    def missing(self) -> List[str]:
        return [f"{r.kind}:{r.name}" for r in self.requirements if not r.present]


@dataclass
class CheckResult:
    name: str
    status: str          # pass | fail | skipped | no-files
    detail: str
    blocking: bool
    cmd: Optional[str] = None


# ---------------------------------------------------------------------------
def _probe(req: Dict[str, str]) -> Requirement:
    if "binary" in req:
        where = shutil.which(req["binary"])
        return Requirement("binary", req["binary"], where is not None, where)
    if "python_module" in req:
        try:
            spec = import_util.find_spec(req["python_module"])
        except (ImportError, ValueError):
            spec = None
        return Requirement("python_module", req["python_module"], spec is not None,
                           getattr(spec, "origin", None) if spec else None)
    return Requirement("unknown", str(req), False)


def plan(profile: Profile) -> List[CheckPlan]:
    """What this profile would run, and what of it this machine can actually run."""
    out: List[CheckPlan] = []
    for check in profile.checks:
        out.append(CheckPlan(
            name=check["name"],
            cmd=check["cmd"],
            confidence=check["confidence"],
            blocking=bool(check.get("blocking", True)),
            requirements=[_probe(r) for r in check.get("requires", []) or []],
            degrade=check.get("degrade"),
        ))
    return out


def effective_level(profile: Profile, plans: List[CheckPlan]) -> str:
    """The level that is TRUE here, which is not always the level declared.

    A profile can honestly declare `full` and still be structural-only on a
    laptop with no compiler. Reporting the declared level in that case is the
    same lie, one layer up.
    """
    declared = profile.validation_level
    blocking_runnable = [p for p in plans if p.blocking and p.runnable]
    if declared == "full" and not blocking_runnable:
        return "structural-only"
    if declared == "full" and any(p.blocking and not p.runnable for p in plans):
        return "full-degraded"
    return declared


def degradation_report(profile: Profile, plans: List[CheckPlan]) -> List[str]:
    lines = []
    for p in plans:
        if p.runnable:
            continue
        missing = ", ".join(p.missing)
        lines.append(
            f"{profile.id}/{p.name}: NOT RUNNING (missing {missing}). "
            + (p.degrade or "No degradation note — the profile owes you one.")
        )
    return lines


# ---------------------------------------------------------------------------
def _expand(cmd: str, cwd: Path, params: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Substitute {skill_root}, profile params and {files:GLOB}.

    Returns None when a {files:...} glob matched nothing — that is 'nothing to
    check here', not a failure, and conflating the two makes every gate red on
    a project that legitimately has no Java in it.
    """
    cmd = cmd.replace("{skill_root}", str(SKILL_ROOT))
    for key, value in (params or {}).items():
        cmd = cmd.replace("{" + key + "}", str(value))

    def sub(match: "re.Match[str]") -> str:
        pattern = match.group(1)
        matches = sorted(
            p for p in _glob.glob(str(cwd / pattern), recursive=True)
            if os.path.isfile(p)
        )
        if not matches:
            raise _NoFiles(pattern)
        return " ".join(f'"{os.path.relpath(m, cwd)}"' for m in matches)

    try:
        return _FILES_TOKEN.sub(sub, cmd)
    except _NoFiles:
        return None


class _NoFiles(Exception):
    pass


def run_checks(profile: Profile, project_dir: Path,
               params: Optional[Dict[str, Any]] = None,
               timeout: int = 300) -> List[CheckResult]:
    """Run every check this machine can run, in the generated project directory."""
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(
            f"{project_dir} is not a directory. Point this at the generated project root."
        )
    results: List[CheckResult] = []
    for p in plan(profile):
        if not p.runnable:
            results.append(CheckResult(
                p.name, "skipped",
                f"missing {', '.join(p.missing)}. "
                + (p.degrade or "No degradation note in the profile."),
                p.blocking,
            ))
            continue
        cmd = _expand(p.cmd, project_dir, params)
        if cmd is None:
            results.append(CheckResult(p.name, "no-files",
                                       "nothing in this project matches the check's file pattern",
                                       p.blocking))
            continue
        try:
            proc = subprocess.run(cmd, shell=True, cwd=project_dir, timeout=timeout,
                                  capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            results.append(CheckResult(p.name, "fail",
                                       f"timed out after {timeout}s", p.blocking, cmd))
            continue
        if proc.returncode == 0:
            results.append(CheckResult(p.name, "pass", "", p.blocking, cmd))
        else:
            detail = (proc.stdout + proc.stderr).strip()
            results.append(CheckResult(p.name, "fail",
                                       detail[:4000] or f"exit {proc.returncode}",
                                       p.blocking, cmd))
    return results


def results_to_dict(results: List[CheckResult]) -> List[Dict[str, Any]]:
    return [asdict(r) for r in results]


def summarize(results: List[CheckResult]) -> str:
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    failed_blocking = [r for r in results if r.status == "fail" and r.blocking]
    tail = ("  ->  FAILED: " + ", ".join(r.name for r in failed_blocking)) if failed_blocking else ""
    return ", ".join(parts) + tail
