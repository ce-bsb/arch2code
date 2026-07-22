"""The baseline that tells a project export what a run actually wrote.

These test the two things that make the diff trustworthy rather than merely
functional: that the snapshot and the diff exclude exactly the same subtrees
(otherwise every file in the difference is reported as created), and that a
snapshot which cannot be read degrades to "no baseline" instead of to a
confident wrong answer.

Run with:  /opt/anaconda3/bin/python -m pytest webapp/tests/test_projectdiff.py -v
"""

from __future__ import annotations

import time
from pathlib import Path

from app.projectdiff import (
    SNAPSHOT_FILENAME,
    diff_snapshot,
    excluded_for,
    read_snapshot,
    take_snapshot,
    write_snapshot,
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "keep.py").write_text("v1\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    return root


def test_added_modified_and_deleted_are_told_apart(tmp_path: Path) -> None:
    root = _project(tmp_path)
    baseline = take_snapshot(root)
    assert baseline.file_count == 2

    time.sleep(0.01)
    (root / "src" / "generated.py").write_text("new\n", encoding="utf-8")
    (root / "src" / "keep.py").write_text("v2 — longer\n", encoding="utf-8")
    (root / "README.md").unlink()

    diff = diff_snapshot(baseline, root)
    assert diff.added == ["src/generated.py"]
    assert diff.modified == ["src/keep.py"]
    assert diff.deleted == ["README.md"]
    assert diff.unchanged == 0
    assert diff.has_baseline is True


def test_noise_is_never_recorded_and_never_reported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    baseline = take_snapshot(root)

    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "keep.cpython-313.pyc").write_bytes(b"\x00")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00")

    diff = diff_snapshot(baseline, root)
    assert diff.added == [], f"build noise leaked into the diff: {diff.added}"


def test_the_apps_own_state_is_excluded_from_both_halves(tmp_path: Path) -> None:
    """webapp/runs changes on every event of every run, including other runs.

    If the snapshot excluded it and the diff did not, every event log in the
    installation would be reported as a file this run created.
    """
    root = _project(tmp_path)
    runs = root / "webapp" / "runs"
    runs.mkdir(parents=True)
    (runs / "old-run.jsonl").write_text("{}\n", encoding="utf-8")

    excluded = excluded_for(root, runs, root / "webapp" / "uploads")
    assert excluded == ["webapp/runs", "webapp/uploads"]

    baseline = take_snapshot(root, excluded=excluded)
    assert not any(name.startswith("webapp/runs") for name in baseline.entries)

    time.sleep(0.01)
    (runs / "this-run.jsonl").write_text('{"id":1}\n', encoding="utf-8")

    diff = diff_snapshot(baseline, root)
    assert diff.added == [], "the app's own bookkeeping was credited to the run"


def test_excluded_for_ignores_paths_outside_the_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert excluded_for(root, tmp_path / "elsewhere") == []


def test_a_symlink_is_neither_recorded_nor_followed(tmp_path: Path) -> None:
    """A symlink into a home directory must not become an archive entry."""
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    snapshot = take_snapshot(root)
    assert "link.txt" not in snapshot.entries
    assert not any(name.startswith("linkdir/") for name in snapshot.entries)


def test_a_snapshot_round_trips_through_disk(tmp_path: Path) -> None:
    root = _project(tmp_path)
    path = tmp_path / SNAPSHOT_FILENAME
    write_snapshot(path, take_snapshot(root, excluded=["webapp/runs"]))

    restored = read_snapshot(path)
    assert restored is not None
    assert restored.file_count == 2
    assert restored.excluded == ["webapp/runs"]
    assert diff_snapshot(restored, root).added == []


def test_an_unusable_snapshot_reads_as_absent_not_as_empty(tmp_path: Path) -> None:
    """The difference matters: absent degrades the export and says so; empty
    would claim every file in the project was created by the run."""
    missing = tmp_path / "nope.json"
    assert read_snapshot(missing) is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_snapshot(corrupt) is None

    wrong_version = tmp_path / "old.json"
    wrong_version.write_text('{"version": 0, "entries": {}}', encoding="utf-8")
    assert read_snapshot(wrong_version) is None

    assert diff_snapshot(None, tmp_path).has_baseline is False


def test_writing_a_snapshot_never_raises(tmp_path: Path) -> None:
    """A run must not fail to start because its baseline could not be written."""
    root = _project(tmp_path)
    blocked = tmp_path / "a-file"
    blocked.write_text("in the way\n", encoding="utf-8")
    write_snapshot(blocked / "child" / SNAPSHOT_FILENAME, take_snapshot(root))
