"""The minimal object-storage interface arch2code needs, and nothing more.

Why this file exists
--------------------
On a laptop the run state is a directory tree: ``webapp/runs/<id>/run.json``,
``events.jsonl``, ``stages/``, ``vision/``. On IBM Code Engine there is no such
tree. The documentation is unambiguous: *"Code Engine containers use ephemeral
filesystems — data written locally to the container is not persisted across
restarts"*, and no per-instance persistent disk exists. Cloud Object Storage is
the only supported answer.

So the interface below is deliberately **not** a filesystem. It is the smallest
set of operations that the existing :mod:`app.store` and :mod:`app.eventlog`
actually perform, expressed as key/value operations that both a POSIX directory
and an S3 bucket can honour with the same guarantees:

============================  =====================================================
operation                     guarantee both backends give
============================  =====================================================
``put_bytes``                 atomic per key. A reader sees the old object or the
                              new one, never a half-written one.
``put_if_absent``             creates only when the key is free (see the caveat).
``get_bytes`` / ``exists``    read-your-write for a key this process just wrote.
``list_keys(prefix,           lexicographic order. Zero-padded numeric keys
start_after=…)``              therefore replay in event order, which is exactly
                              what ``Last-Event-ID`` needs.
``local_path``                a real path, or ``None`` when the backend has none.
============================  =====================================================

``local_path`` is the honest seam. Bob is a subprocess: it reads and writes real
files in a real working directory, and no object store changes that. Code that
must hand a path to a child process asks for ``local_path`` and gets ``None``
from the COS backend, which forces the caller to stage the bytes on ephemeral
disk first (see :mod:`app.storage.sync`) instead of silently passing a key that
Bob cannot open.

What is NOT here: rename, append-in-place, directory listing with metadata,
locking. Every one of those is a filesystem affordance that S3 does not have,
and pretending otherwise is how ``events.jsonl`` gets corrupted on s3fs.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import AppError

__all__ = [
    "ObjectStore",
    "StorageError",
    "ObjectInfo",
    "validate_key",
    "KEY_RE",
]


class StorageError(AppError):
    """A storage operation failed, with a stated next action.

    Subclasses :class:`app.errors.AppError` on purpose: a storage failure inside
    a request handler must render through the same four-field envelope
    (``code``/``title``/``detail``/``remedy``) as every other failure, so the
    front end needs no special case.
    """

    def __init__(
        self,
        code: str,
        title: str,
        detail: str,
        *,
        remedy: str,
        status: int = 500,
        **context: Any,
    ) -> None:
        super().__init__(code, title, detail, remedy=remedy, status=status, **context)


#: Keys are POSIX-ish relative paths. Anchored and explicit so that no
#: client-supplied string can escape the bucket prefix or the local root:
#: no leading slash, no ``..`` segment, no backslash, no NUL.
KEY_RE = re.compile(r"^(?!/)(?!.*//)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._\-/ ()]{1,900}$")


def validate_key(key: str) -> str:
    """Return ``key`` if it is a legal object key, else raise.

    Path traversal is checked here rather than at every call site because both
    backends join the key onto something — a directory in one case, a bucket
    prefix in the other — and exactly one of those two failures is silent.
    """
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise StorageError(
            "invalid_object_key",
            "Malformed storage key",
            f"{key!r} is not a legal object key.",
            remedy=(
                "Keys are relative POSIX paths built from [A-Za-z0-9._-/ ()], with "
                "no leading slash and no '..' segment. Build them with the helpers "
                "in app.storage.keys instead of by hand."
            ),
        )
    return key


@dataclass(frozen=True)
class ObjectInfo:
    """What a listing knows about one object."""

    key: str
    size: int
    #: ISO-8601 string, or None when the backend does not report one cheaply.
    last_modified: str | None = None


class ObjectStore(ABC):
    """Key/value store for run state, uploads and artifacts.

    Implementations must be safe to call from a worker thread. They are NOT
    required to be safe against two *processes* writing the same key: the
    architecture guarantees a single writer per run (one job run owns a run's
    events; the app owns uploads), and that invariant is what makes
    ``put_if_absent`` sufficient.
    """

    #: Short identifier reported by /api/health and written into run metadata.
    backend: str = "abstract"

    # -- required operations ------------------------------------------------ #

    @abstractmethod
    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None
    ) -> None:
        """Write ``data`` at ``key``, atomically, overwriting any previous value."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Read the object at ``key``.

        Raises:
            StorageError: with code ``object_not_found`` when the key is absent.
        """

    @abstractmethod
    def exists(self, key: str) -> bool:
        """True when ``key`` holds an object."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove ``key``. Deleting an absent key is a no-op, never an error."""

    @abstractmethod
    def list_keys(
        self,
        prefix: str,
        *,
        start_after: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Keys under ``prefix``, in lexicographic order.

        ``start_after`` is exclusive, mirroring S3's ``StartAfter``. This is the
        whole reason event objects are named ``{seq:08d}.json``: resuming a
        stream after event 41 is ``start_after=".../00000041.json"`` and needs no
        index, no cursor table and no scan of what came before.
        """

    @abstractmethod
    def local_path(self, key: str) -> Path | None:
        """Real filesystem path for ``key``, or ``None`` if there is not one.

        ``None`` is not a failure. It is the signal that a subprocess cannot be
        handed this object directly and that the caller must materialize it.
        """

    @abstractmethod
    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Write only if ``key`` is free. True when this call created it.

        Local: ``O_CREAT|O_EXCL`` — genuinely atomic.
        COS: check-then-put, which is **not** atomic and is documented as such
        on the implementation. Two writers racing on the same key is outside the
        single-writer invariant this system maintains.
        """

    @abstractmethod
    def probe(self) -> ObjectInfo | None:
        """Prove the store is reachable and writable, or raise ``StorageError``.

        Called by the health probe and by ``deploy.sh``'s smoke test. Must do a
        real round trip — a constructor that only records configuration proves
        nothing about credentials, network or bucket policy.
        """

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Non-secret description for the health report and the startup log.

        Never returns a credential. Endpoint, bucket and prefix are location,
        not authorization, and the health panel is useless without them.
        """

    # -- convenience, implemented once for every backend --------------------- #

    def put_text(self, key: str, text: str, *, content_type: str = "text/plain") -> None:
        self.put_bytes(key, text.encode("utf-8"), content_type=f"{content_type}; charset=utf-8")

    def get_text(self, key: str) -> str:
        return self.get_bytes(key).decode("utf-8", errors="replace")

    def put_json(self, key: str, payload: Any) -> None:
        """Write pretty JSON. Never raises on an odd value: ``default=str``.

        Narration and metadata must not be able to kill a run, which is the same
        rule :func:`app.eventlog._encode` follows.
        """
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        self.put_bytes(key, text.encode("utf-8"), content_type="application/json; charset=utf-8")

    def get_json(self, key: str) -> Any:
        raw = self.get_bytes(key)
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise StorageError(
                "object_not_json",
                "A stored document is corrupt",
                f"{key} is not valid JSON: {exc}",
                remedy=(
                    "Delete that object and re-run the stage that writes it. If it "
                    "recurs, the writer is producing partial objects and that is a bug."
                ),
                key=key,
            ) from exc

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> None:
        """Upload one local file. Small-file path — the whole body is buffered."""
        self.put_bytes(key, Path(path).read_bytes(), content_type=content_type)

    def get_file(self, key: str, path: Path) -> Path:
        """Materialize ``key`` at ``path`` and return it."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.get_bytes(key))
        return target

    def iter_prefix(self, prefix: str, *, page: int = 1000) -> Iterable[str]:
        """Every key under ``prefix``, paging so a large run does not blow memory."""
        cursor: str | None = None
        while True:
            batch = self.list_keys(prefix, start_after=cursor, limit=page)
            if not batch:
                return
            yield from batch
            cursor = batch[-1]
            if len(batch) < page:
                return

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        details = ", ".join(f"{k}={v!r}" for k, v in sorted(self.describe().items()))
        return f"<{type(self).__name__} {details}>"


def redact(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Drop anything that looks like a credential from a describe() payload."""
    banned = ("key", "secret", "password", "token", "apikey", "credential")
    return {
        k: v
        for k, v in mapping.items()
        if not any(word in k.lower() for word in banned)
    }
