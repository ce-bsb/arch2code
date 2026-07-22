"""Knowing which files in the project tree a run actually produced.

The literal requirement is an export of "the code artifacts AND the whole root
of the directory and of the generated files of the project". That is harder
than it sounds, because the scaffold stage does not write only into its
contracted ``.arch/build/<run_id>/`` directory: both historical runs in this
repository wrote a real project tree elsewhere and merely *described* it in
``manifest.json``. Exporting the contracted directory alone hands somebody a
manifest describing code the archive does not contain.

There are three ways to know what a run wrote, and each one is wrong on its own:

1. **The contracted directories.** ``.arch/{intake,air,review,build,run}/<run_id>/``
   are named after the run, so anything inside them belongs to it beyond
   argument. But the interesting output is precisely what escapes them.
2. **The stage-4 manifest.** Authoritative about intent, written by a language
   model, and therefore able to name a file that was never written — or to omit
   one that was.
3. **A filesystem diff.** Exact about what changed on disk, and blind to *who*
   changed it: an editor save or a second run during the same window is
   indistinguishable from the scaffold's own work.

So this module does the third one and the exporter unions all three, labelling
every file with how it was found. :func:`take_snapshot` runs at run start and
records ``(size, mtime_ns)`` per path; :func:`diff_snapshot` runs at export time
and reports what is new, what changed, and what vanished. The archive's
MANIFEST.md prints the whole heuristic, including its failure modes, because a
user who does not know how a set of files was chosen cannot trust it.

Cost: the tree this walks is a few hundred files (7 ms measured on the
development machine). ``_SKIP_DIRS`` keeps ``.git``, ``node_modules`` and the
virtualenvs out, and the app's own ``webapp/runs`` and ``webapp/uploads`` are
excluded by the caller — a run's event log is already carried as ``audit/``, and
a *second* copy of every other run's log is not something anybody asked to
download.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

__all__ = [
    "SNAPSHOT_VERSION",
    "SNAPSHOT_FILENAME",
    "Snapshot",
    "SnapshotDiff",
    "excluded_for",
    "take_snapshot",
    "write_snapshot",
    "read_snapshot",
    "diff_snapshot",
]

#: Bumped when the recorded shape changes in a way that makes an older file
#: unreadable. A snapshot from a previous version is reported as absent rather
#: than misinterpreted.
SNAPSHOT_VERSION = 1

SNAPSHOT_FILENAME = "fs-snapshot.json"

#: Directories that are never part of a deliverable and are expensive to walk.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".tox", ".next",
    ".cache", "dist-info", ".DS_Store", ".gradle", "target",
})

_SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd", ".class", ".o"})
_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db"})

#: A tree larger than this is not walked to the end. The count is recorded, so
#: the MANIFEST can say the diff is partial rather than pretending it is total.
_MAX_ENTRIES = 200_000


@dataclass(frozen=True)
class Snapshot:
    """What the project tree looked like at one instant.

    ``entries`` maps a path relative to ``root`` (POSIX separators, so a
    snapshot written on one platform still reads on another) to
    ``[size, mtime_ns]``.
    """

    version: int
    root: str
    taken_at: datetime
    entries: dict[str, list[int]]
    #: Paths relative to ``root`` that were deliberately not walked.
    excluded: list[str] = field(default_factory=list)
    #: True when the walk hit ``_MAX_ENTRIES`` and stopped early.
    truncated: bool = False

    @property
    def file_count(self) -> int:
        return len(self.entries)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "root": self.root,
            "taken_at": self.taken_at.isoformat(),
            "excluded": list(self.excluded),
            "truncated": self.truncated,
            "file_count": len(self.entries),
            "entries": self.entries,
        }


@dataclass
class SnapshotDiff:
    """What changed under the root between a snapshot and now."""

    #: Relative paths that did not exist when the snapshot was taken.
    added: list[str] = field(default_factory=list)
    #: Relative paths whose size or mtime changed.
    modified: list[str] = field(default_factory=list)
    #: Relative paths that existed then and do not now. Never exported — you
    #: cannot archive a file that is gone — but reported, because a run that
    #: DELETED something is a fact an auditor wants.
    deleted: list[str] = field(default_factory=list)
    unchanged: int = 0
    #: None when no usable snapshot existed, which is the case for every run
    #: started before this file existed. The exporter degrades to the manifest
    #: and the contracted directories and says so in the MANIFEST.
    snapshot: Snapshot | None = None
    truncated: bool = False

    @property
    def has_baseline(self) -> bool:
        return self.snapshot is not None

    @property
    def changed(self) -> list[str]:
        """Everything the run is credited with, newest evidence first."""
        return [*self.added, *self.modified]


def excluded_for(root: Path, *paths: Path) -> list[str]:
    """Which of ``paths`` fall under ``root``, as relative POSIX prefixes.

    Both the snapshot and the diff must exclude exactly the same subtrees or
    everything inside the difference is reported as added. One function, called
    from both, is what guarantees that — and the app's own ``webapp/runs`` and
    ``webapp/uploads`` are the two that matter: they change on every event of
    every run, including runs that have nothing to do with this one.
    """
    out: list[str] = []
    resolved_root = Path(root).resolve()
    for candidate in paths:
        try:
            rel = Path(candidate).resolve().relative_to(resolved_root).as_posix()
        except (ValueError, OSError):
            continue
        if rel and rel != ".":
            out.append(rel)
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# walking
# --------------------------------------------------------------------------- #


def _is_excluded(rel: str, excluded: Sequence[str]) -> bool:
    for prefix in excluded:
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _iter_files(
    root: Path, excluded: Sequence[str]
) -> Iterator[tuple[str, os.stat_result]]:
    """Yield ``(relative_posix_path, stat)`` for every real file under ``root``.

    Symlinks are not followed and not recorded: a symlink into somebody's home
    directory must not become an archive entry, and a symlinked directory is the
    classic way to walk a tree forever.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        try:
            rel_dir = current.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - os.walk cannot leave root
            continue
        if rel_dir == ".":
            rel_dir = ""

        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _SKIP_DIRS
            and not _is_excluded(f"{rel_dir}/{name}".lstrip("/"), excluded)
        )

        for name in sorted(filenames):
            if name in _SKIP_NAMES or Path(name).suffix.lower() in _SKIP_SUFFIXES:
                continue
            rel = f"{rel_dir}/{name}".lstrip("/")
            if _is_excluded(rel, excluded):
                continue
            path = current / name
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            yield rel, stat


def take_snapshot(root: Path, *, excluded: Sequence[str] = ()) -> Snapshot:
    """Record every file under ``root`` with its size and modification time.

    ``excluded`` holds paths relative to ``root`` (e.g. ``"webapp/runs"``) that
    are skipped entirely. Never raises: an unreadable directory is simply not in
    the snapshot, which downgrades a file inside it to "unattributable" later
    rather than failing a run at its very first step.
    """
    root = Path(root)
    entries: dict[str, list[int]] = {}
    truncated = False
    for rel, stat in _iter_files(root, excluded):
        entries[rel] = [int(stat.st_size), int(stat.st_mtime_ns)]
        if len(entries) >= _MAX_ENTRIES:
            truncated = True
            break
    return Snapshot(
        version=SNAPSHOT_VERSION,
        root=str(root),
        taken_at=datetime.now(timezone.utc),
        entries=entries,
        excluded=list(excluded),
        truncated=truncated,
    )


def write_snapshot(path: Path, snapshot: Snapshot) -> None:
    """Persist a snapshot next to the run state. Atomic, and never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(snapshot.to_json(), ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError:
        # A run must not fail to start because its baseline could not be
        # written. The export reports the missing baseline instead.
        pass


def read_snapshot(path: Path) -> Snapshot | None:
    """Read a snapshot back, returning None for anything unusable."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("version") != SNAPSHOT_VERSION:
        return None
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, dict):
        return None

    entries: dict[str, list[int]] = {}
    for key, value in raw_entries.items():
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                entries[str(key)] = [int(value[0]), int(value[1])]
            except (TypeError, ValueError):
                continue

    taken_at = datetime.now(timezone.utc)
    raw_taken = document.get("taken_at")
    if isinstance(raw_taken, str):
        try:
            taken_at = datetime.fromisoformat(raw_taken)
        except ValueError:
            pass

    excluded = document.get("excluded")
    return Snapshot(
        version=SNAPSHOT_VERSION,
        root=str(document.get("root") or ""),
        taken_at=taken_at,
        entries=entries,
        excluded=[str(x) for x in excluded] if isinstance(excluded, list) else [],
        truncated=bool(document.get("truncated")),
    )


def diff_snapshot(snapshot: Snapshot | None, root: Path) -> SnapshotDiff:
    """Compare the tree as it is now against the baseline.

    A file counts as modified when its size OR its mtime differs. mtime alone
    would miss a same-second rewrite of the same length; size alone would miss
    every in-place edit. Content hashing the whole tree would catch both and
    costs a full read of every file in the project on every export, which is not
    a trade this tool needs to make — a file wrongly listed as modified is a
    harmless extra entry in an archive, and the MANIFEST says so.
    """
    if snapshot is None:
        return SnapshotDiff(snapshot=None)

    diff = SnapshotDiff(snapshot=snapshot, truncated=snapshot.truncated)
    baseline = snapshot.entries
    seen: set[str] = set()

    for rel, stat in _iter_files(Path(root), snapshot.excluded):
        seen.add(rel)
        before = baseline.get(rel)
        if before is None:
            diff.added.append(rel)
        elif before[0] != int(stat.st_size) or before[1] != int(stat.st_mtime_ns):
            diff.modified.append(rel)
        else:
            diff.unchanged += 1

    diff.deleted = sorted(set(baseline) - seen)
    diff.added.sort()
    diff.modified.sort()
    return diff
