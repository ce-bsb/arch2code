"""Persistence for uploads and runs.

Everything lives on disk under ``webapp/uploads`` and ``webapp/runs``. There is
no database on purpose: this is a local, single-process demonstration tool, and
a Postgres would be three moving parts to install before anyone can look at a
bounding box. The upgrade path is documented in the README.

Two invariants worth stating, because both are easy to get wrong later:

* ``run.json`` is written atomically (temp file + ``os.replace``). A run that is
  read while another coroutine writes it must never see half a document.
* :meth:`RunStore.workspace_dir` returns the **project root**, not a per-run
  scratch directory. Bob resolves ``--chat-mode`` from the ``.bob/`` of its CWD,
  so a run that executed anywhere else would silently lose the six arch2code
  modes. The ``run_id`` argument exists so that a future per-run git worktree
  can be slotted in without touching any caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import unicodedata
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .errors import BadRequest, ConfigError, NotFound
from .eventlog import EventLog, EventLogRegistry
from .models import RunState, RunSummary, StageId, UploadRef
from .ingest import IngestError
from .routing import as_app_error, route_content, sibling_structured

__all__ = ["UploadStore", "RunStore", "mint_run_id", "slugify"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{4}-[a-z0-9-]+$")
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slugify(value: str, *, fallback: str = "diagram", max_len: int = 24) -> str:
    """Reduce arbitrary text to the ``[a-z0-9-]`` alphabet the run id allows."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_only).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or fallback


def mint_run_id(slug: str, *, now: datetime | None = None) -> str:
    """Build a run id in the pipeline's canonical ``YYYYMMDD-HHMM-<slug>`` form.

    The format is not cosmetic: ``air.schema.json`` pins ``meta.run_id`` to this
    pattern, and the same id names the directory of every stage under ``.arch/``.
    """
    stamp = (now or _utcnow()).strftime("%Y%m%d-%H%M")
    return f"{stamp}-{slugify(slug)}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# uploads
# --------------------------------------------------------------------------- #


class UploadStore:
    """Uploaded drawings, addressed by the sha256 of their bytes.

    Content addressing means re-uploading the same drawing is idempotent, which
    matters more than it sounds: during a demo the same napkin gets dropped on
    the page repeatedly, and each copy would otherwise become a distinct run
    input with its own directory.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = Path(settings.uploads_root)
        self._root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------------

    def _dir(self, upload_id: str) -> Path:
        if not _UPLOAD_ID_RE.match(upload_id or ""):
            raise BadRequest(
                code="invalid_upload_id",
                title="Malformed upload id",
                detail=f"{upload_id!r} is not a 16-character hexadecimal id.",
                remedy="Use the upload_id returned by POST /api/uploads.",
            )
        return self._root / upload_id

    def _meta_path(self, upload_id: str) -> Path:
        return self._dir(upload_id) / "upload.json"

    def file_path(self, upload_id: str) -> Path:
        """Absolute path of the stored bytes."""
        ref = self.load(upload_id)
        path = Path(ref.stored_path)
        if not path.exists():
            raise NotFound(
                code="upload_file_missing",
                title="The uploaded file is gone",
                detail=f"{path} no longer exists, although its metadata does.",
                remedy="Upload the drawing again.",
            )
        return path

    # -- operations ----------------------------------------------------------

    def save(self, filename: str, data: bytes, content_type: str | None) -> UploadRef:
        """Persist bytes and return the reference, computing the routing decision.

        Routing is decided from the **bytes**, not from the filename. The file is
        written first so the adapters can read it from disk, and a refusal deletes
        it again — a rejected upload must not leave content-addressed litter in
        ``webapp/uploads``. The ``content_type`` the browser sent is recorded but
        never trusted: it is derived from the same suffix we are refusing to
        believe.
        """
        if not data:
            raise BadRequest(
                code="empty_upload",
                title="The uploaded file is empty",
                detail="Zero bytes were received.",
                remedy="Pick a file that actually has content.",
            )

        digest = hashlib.sha256(data).hexdigest()
        upload_id = digest[:16]
        target_dir = self._dir(upload_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        safe_name = Path(filename).name or "upload.bin"
        stored = target_dir / safe_name
        if not stored.exists():
            stored.write_bytes(data)

        try:
            routing, report = route_content(stored, data=data, filename=safe_name)
        except IngestError as exc:
            # The bytes were refused, so nothing about them should survive. Remove
            # the file (and the directory, if this upload created it) before the
            # error propagates: an upload that 415s must not remain fetchable at
            # /api/uploads/<id>/file.
            with suppress(OSError):
                stored.unlink(missing_ok=True)
            with suppress(OSError):
                target_dir.rmdir()
            raise as_app_error(exc) from exc

        siblings = [str(p) for p in sibling_structured(stored)]

        warnings: list[str] = []
        if not report.summary.extension_agrees:
            warnings.append(
                f"The file is named '{safe_name}' but its bytes are "
                f"{report.summary.format_label}. The real type is what was used."
            )
        if report.summary.requires_page_selection:
            warnings.append(
                f"{report.summary.page_count} pages were found. Choose which to read "
                "before starting a run: every page sent to the vision model is a "
                "separate inference call."
            )
        if report.summary.structure_available and report.summary.vision_required:
            warnings.append(
                f"{report.summary.structure_nodes} labels were read exactly from the "
                "file itself. Use them to check the vision result — they cannot be "
                "hallucinated."
            )
        if siblings:
            warnings.append(
                "A structured source for the same drawing is available "
                f"({', '.join(Path(s).name for s in siblings)}). Parsing it is exact "
                "and free; vision is neither."
            )
        limit = self._settings.max_upload_mb * 1024 * 1024
        if len(data) > limit:
            warnings.append(
                f"{len(data) / 1048576:.1f} MB exceeds ARCH2CODE_MAX_UPLOAD_MB "
                f"({self._settings.max_upload_mb})."
            )

        ref = UploadRef(
            upload_id=upload_id,
            filename=safe_name,
            # The detected mime, not the browser's Content-Type: the browser
            # derives that from the same suffix detection just overruled.
            content_type=report.summary.mime or content_type or "application/octet-stream",
            bytes=len(data),
            sha256=digest,
            stored_path=str(stored.resolve()),
            routing=routing,
            ingest=report.summary,
            pages=report.pages,
            structured_siblings=siblings,
            warnings=warnings,
            created_at=_utcnow(),
        )
        _atomic_write_text(self._meta_path(upload_id), ref.model_dump_json(indent=2))
        return ref

    def load(self, upload_id: str) -> UploadRef:
        meta = self._meta_path(upload_id)
        if not meta.exists():
            raise NotFound(
                code="upload_not_found",
                title="No such upload",
                detail=f"No upload with id {upload_id!r} is stored.",
                remedy="Upload the drawing first, then use the returned upload_id.",
            )
        return UploadRef.model_validate_json(meta.read_text(encoding="utf-8"))

    def list(self, *, limit: int = 50) -> list[UploadRef]:
        refs: list[UploadRef] = []
        if not self._root.exists():
            return refs
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            meta = entry / "upload.json"
            if not meta.exists():
                continue
            try:
                refs.append(UploadRef.model_validate_json(meta.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 - one corrupt record must not hide the rest
                continue
        refs.sort(key=lambda r: r.created_at, reverse=True)
        return refs[: max(1, limit)]


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #


class RunStore:
    """Run state on disk, one directory per run.

        webapp/runs/<run_id>/run.json      the RunState document
        webapp/runs/<run_id>/events.jsonl  the append-only event log
        webapp/runs/<run_id>/vision/       Mode A output (extraction, verifications)
        webapp/runs/<run_id>/stages/<id>/  per-stage stdout, stderr, raw NDJSON
    """

    def __init__(self, settings: Settings, events: EventLogRegistry) -> None:
        self._settings = settings
        self._events = events
        self._root = Path(settings.runs_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # -- paths ---------------------------------------------------------------

    def _validate(self, run_id: str) -> str:
        if not _RUN_ID_RE.match(run_id or ""):
            raise BadRequest(
                code="invalid_run_id",
                title="Malformed run id",
                detail=(
                    f"{run_id!r} does not match YYYYMMDD-HHMM-<slug>. That pattern is "
                    "pinned by air.schema.json, so an invalid id would produce an AIR "
                    "that fails validation later, far from here."
                ),
                remedy="Use the run_id returned by POST /api/runs.",
            )
        return run_id

    def run_dir(self, run_id: str) -> Path:
        return self._root / self._validate(run_id)

    def _state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def vision_dir(self, run_id: str) -> Path:
        path = self.run_dir(run_id) / "vision"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def stage_dir(self, run_id: str, stage: StageId | str) -> Path:
        safe = _SLUG_RE.sub("-", str(stage).lower()).strip("-") or "stage"
        path = self.run_dir(run_id) / "stages" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def gate_path(self, run_id: str) -> Path:
        """Where the gate decision is recorded, independent of ``run.json``.

        Kept as its own file so that the auditable fact — a human approved or
        blocked at this timestamp — survives any later rewrite of run state.
        """
        return self.run_dir(run_id) / "gate.json"

    def workspace_dir(self, run_id: str) -> Path:  # noqa: ARG002 - see module docstring
        """The CWD every Bob subprocess uses: the project root.

        Not per-run. Bob discovers ``--chat-mode`` from the ``.bob/`` directory
        of its working directory; running elsewhere silently reduces the mode
        list to the four built-ins and the pipeline stops existing.
        """
        return Path(self._settings.bob_cwd)

    def input_dir(self, run_id: str) -> Path:
        """Where this run's drawing is placed for Bob to read.

        Under the project root so it needs no ``--include-directories``; under a
        per-run subdirectory so two concurrent runs cannot overwrite each other's
        input.
        """
        path = self.workspace_dir(run_id) / ".arch" / "intake" / "inbox" / self._validate(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def eventlog(self, run_id: str) -> EventLog:
        return self._events.get(self._validate(run_id))

    # -- state ---------------------------------------------------------------

    def _lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[run_id] = lock
        return lock

    def exists(self, run_id: str) -> bool:
        try:
            return self._state_path(run_id).exists()
        except BadRequest:
            return False

    def create(self, state: RunState) -> RunState:
        path = self._state_path(state.run_id)
        if path.exists():
            raise BadRequest(
                code="run_exists",
                title="That run id is already taken",
                detail=f"{state.run_id} already has state on disk.",
                remedy="Create the run again; a fresh id is minted per request.",
            )
        self.run_dir(state.run_id).mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, state.model_dump_json(indent=2))
        return state

    def load(self, run_id: str) -> RunState:
        path = self._state_path(run_id)
        if not path.exists():
            raise NotFound(
                code="run_not_found",
                title="No such run",
                detail=f"No run with id {run_id!r} exists under {self._root}.",
                remedy="Start a run with POST /api/runs, or list existing ones with GET /api/runs.",
            )
        try:
            return RunState.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                code="run_state_corrupt",
                title="The run state could not be read",
                detail=f"{path} is not a valid RunState document: {exc}",
                remedy="Delete that run directory and start a new run.",
            ) from exc

    async def update(
        self, run_id: str, mutate: Callable[[RunState], RunState | None]
    ) -> RunState:
        """Read, mutate and write ``run.json`` under a per-run lock.

        ``mutate`` may return the new state or mutate in place and return None;
        both call styles appear at the call sites and both are supported so that
        neither has to be rewritten.
        """
        async with self._lock(run_id):
            state = self.load(run_id)
            result = mutate(state)
            new_state = result if isinstance(result, RunState) else state
            new_state.updated_at = _utcnow()
            _atomic_write_text(
                self._state_path(run_id), new_state.model_dump_json(indent=2)
            )
            return new_state

    def list(self, *, limit: int = 50) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        if not self._root.exists():
            return summaries
        for entry in self._root.iterdir():
            if not entry.is_dir() or not (entry / "run.json").exists():
                continue
            try:
                state = self.load(entry.name)
            except Exception:  # noqa: BLE001 - one bad run must not hide the others
                continue
            done = sum(1 for s in state.stages if s.status == "succeeded")
            current = next(
                (s.id for s in state.stages if s.status == "running"),
                next((s.id for s in state.stages if s.status == "blocked"), None),
            )
            summaries.append(
                RunSummary(
                    run_id=state.run_id,
                    mode=state.mode,
                    status=state.status,
                    slug=state.slug,
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                    source_filename=state.upload.filename,
                    current_stage=current,
                    stages_done=done,
                    stages_total=len(state.stages),
                    last_event_id=state.last_event_id,
                )
            )
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return summaries[: max(1, limit)]
