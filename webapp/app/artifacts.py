"""Resolving, listing and reading the ``.arch/`` artifacts a stage produced.

Two rules shape this module.

**An absent artifact is a result, not an error.** A stage that exits 0 without
writing its contracted file is the exact signature of ``arch-scaffold`` running
without ``--yolo`` — Bob strips ``write_to_file`` from the tool set in
non-interactive mode, so the stage "succeeds" and produces nothing. Reporting
``exists=False`` loudly is how that reaches the user instead of being mistaken
for success.

**Artifact ids are opaque and path traversal is refused.** The id encodes the
path, but every read re-resolves it against the allowed roots. A client that
forges an id cannot walk out of the project.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .errors import BadRequest, NotFound
from .models import ArtifactRef

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .pipeline import StageSpec

__all__ = [
    "make_artifact_id",
    "decode_artifact_id",
    "scan_stage_outputs",
    "describe",
    "read_text",
    "classify",
]

#: Extensions we are willing to render as text in the browser.
_TEXT_SUFFIXES = {
    ".md", ".json", ".yaml", ".yml", ".py", ".txt", ".sh", ".toml",
    ".cfg", ".ini", ".xml", ".drawio", ".puml", ".mmd", ".jsonl",
}

_MAX_TEXT_BYTES = 2 * 1024 * 1024


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def classify(path: Path) -> str:
    """Map a path to an :data:`~app.models.ArtifactKind`.

    Driven by the pipeline's own directory contract, so a file gets the same
    kind regardless of which stage happened to report it.
    """
    parts = {p.lower() for p in path.parts}
    name = path.name.lower()

    if name == "extraction.json" or "capture-manifest.json" == name:
        return "extraction"
    if name == "air.json":
        return "air"
    if name == "verdict.md":
        return "verdict"
    if name == "manifest.json":
        return "manifest"
    if name == "pipeline.md":
        return "pipeline_log"
    if name == "validation.md":
        return "validation"
    if ".arch" in parts and "build" in parts:
        return "code"
    if ".arch" in parts and "intake" in parts:
        return "extraction"
    return "other"


def make_artifact_id(path: Path, run_id: str) -> str:
    """Opaque, URL-safe id for one artifact of one run.

    The path is embedded so the id survives a restart with no index, and a short
    digest of ``run_id`` is prefixed so ids from different runs never collide in
    a client-side map.
    """
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
    encoded = base64.urlsafe_b64encode(str(path).encode("utf-8")).decode("ascii")
    return f"{digest}.{encoded.rstrip('=')}"


def decode_artifact_id(artifact_id: str) -> Path:
    """Recover the path an id encodes. Never trusted on its own — see :func:`describe`."""
    try:
        _, _, encoded = artifact_id.partition(".")
        padding = "=" * (-len(encoded) % 4)
        return Path(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise BadRequest(
            code="invalid_artifact_id",
            title="Malformed artifact id",
            detail=f"{artifact_id!r} could not be decoded.",
            remedy="Use an artifact_id from GET /api/runs/{run_id}/artifacts.",
        ) from exc


def describe(path: Path, run_id: str, *, stage: str | None = None) -> ArtifactRef:
    """Build an :class:`ArtifactRef`, whether or not the file is there."""
    exists = path.is_file()
    stat = path.stat() if exists else None
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type is None:
        media_type = "text/plain" if path.suffix.lower() in _TEXT_SUFFIXES else "application/octet-stream"

    return ArtifactRef(
        artifact_id=make_artifact_id(path, run_id),
        stage=stage,  # type: ignore[arg-type]
        kind=classify(path),
        path=str(path),
        rel_path=path.name,
        bytes=stat.st_size if stat else 0,
        mtime=_utc(stat.st_mtime) if stat else None,
        media_type=media_type,
        exists=exists,
    )


def _relativize(refs: Iterable[ArtifactRef], root: Path) -> list[ArtifactRef]:
    out: list[ArtifactRef] = []
    for ref in refs:
        try:
            ref.rel_path = str(Path(ref.path).relative_to(root))
        except ValueError:
            ref.rel_path = Path(ref.path).name
        out.append(ref)
    return out


def scan_stage_outputs(run_id: str, spec: "StageSpec", root: Path) -> list[ArtifactRef]:
    """Everything a stage is known to have written, contracted file first.

    The contracted artifact is always included even when missing: that absence
    is the most informative thing this function can report.
    """
    root = Path(root)
    refs: list[ArtifactRef] = []
    seen: set[Path] = set()

    if spec.artifact_template:
        contracted = root / spec.artifact_template.format(run_id=run_id)
        refs.append(describe(contracted, run_id, stage=spec.id))
        seen.add(contracted)

    # Directories each stage owns, by convention of the pipeline.
    candidates = [
        root / ".arch" / "intake" / run_id,
        root / ".arch" / "air" / run_id,
        root / ".arch" / "review" / run_id,
        root / ".arch" / "build" / run_id,
        root / ".arch" / "run" / run_id,
    ]
    for directory in candidates:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.rglob("*")):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry in seen:
                continue
            # Only attribute a file to this stage when the stage owns the tree.
            if spec.artifact_template:
                owner = (root / spec.artifact_template.format(run_id=run_id)).parent
                if directory != owner and not str(entry).startswith(str(owner)):
                    continue
            refs.append(describe(entry, run_id, stage=spec.id))
            seen.add(entry)

    return _relativize(refs, root)


def resolve(artifact_id: str, run_id: str, allowed_roots: Iterable[Path]) -> Path:
    """Decode an id and prove the path lies inside one of ``allowed_roots``."""
    path = decode_artifact_id(artifact_id)
    resolved = path.resolve() if path.is_absolute() else path

    for root in allowed_roots:
        try:
            resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        if not resolved.is_file():
            raise NotFound(
                code="artifact_missing",
                title="The artifact does not exist",
                detail=(
                    f"{resolved} is not on disk. For a stage that exited 0 this is the "
                    "signature of arch-scaffold running without --yolo: Bob removes "
                    "write_to_file from the tool set in non-interactive mode, so the "
                    "stage completes and writes nothing."
                ),
                remedy="Re-run the stage with the yolo approval mode, inside a sandbox.",
            )
        return resolved

    raise BadRequest(
        code="artifact_outside_project",
        title="That artifact is outside the project",
        detail=f"{resolved} does not lie under any directory this run may read.",
        remedy="Only artifacts produced by the run itself can be read.",
    )


def read_text(path: Path) -> str:
    """Read an artifact as text, refusing what should not be rendered as text."""
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        raise BadRequest(
            code="artifact_not_text",
            title="That artifact is not text",
            detail=f"{path.name} has no text-renderable extension.",
            remedy="Download it instead of asking for its text.",
        )
    size = path.stat().st_size
    if size > _MAX_TEXT_BYTES:
        raise BadRequest(
            code="artifact_too_large",
            title="That artifact is too large to display",
            detail=f"{path.name} is {size / 1048576:.1f} MB; the limit is 2 MB.",
            remedy="Open the file directly from disk.",
        )
    return path.read_text(encoding="utf-8", errors="replace")
