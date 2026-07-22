"""Filesystem backend — the one that runs on the laptop today.

This is not a new persistence layer. It is the semantics :mod:`app.store`
already implements, restated behind :class:`~app.storage.base.ObjectStore` so
the COS backend has something concrete to satisfy:

* writes go through a temp file plus :func:`os.replace`, which is the same
  atomic-write helper ``store._atomic_write_text`` uses, so a reader never sees
  a half-written ``run.json``;
* ``fsync`` before the rename, because the durability boundary of the event log
  is the point at which a subscriber is allowed to learn the event exists;
* ``list_keys`` sorts, because the POSIX directory order is arbitrary and the
  contract of the interface is lexicographic order.

Local mode is the default and it is the demo path. Nothing in this file talks to
the network, imports an SDK, or needs a credential.
"""

from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import ObjectInfo, ObjectStore, StorageError, validate_key

__all__ = ["LocalObjectStore"]


class LocalObjectStore(ObjectStore):
    """Objects as files under ``root``. Keys map 1:1 onto relative paths."""

    backend = "local"

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # -- paths --------------------------------------------------------------- #

    def _path(self, key: str) -> Path:
        path = (self._root / validate_key(key)).resolve()
        # Belt and braces. validate_key already rejects '..', but a symlinked
        # directory inside the root could still point out of it, and this store
        # writes files whose names come from user uploads.
        if not str(path).startswith(str(self._root) + os.sep):
            raise StorageError(
                "object_key_escapes_root",
                "That storage key points outside the store",
                f"{key!r} resolves to {path}, which is not under {self._root}.",
                remedy=(
                    "This is a bug or an attack, not a configuration problem. Report "
                    "the key. Do not work around it by widening the root."
                ),
                key=key,
            )
        return path

    def local_path(self, key: str) -> Path | None:
        """Always a real path — which is exactly why localhost needs no staging."""
        return self._path(key)

    # -- operations ---------------------------------------------------------- #

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None
    ) -> None:
        del content_type  # a filesystem has no content type; the key's suffix is it
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:  # pragma: no cover - fsync unsupported (some FUSE)
                    pass
            os.replace(tmp, path)
        except OSError as exc:
            raise StorageError(
                "object_write_failed",
                "Could not write to local storage",
                f"{path}: {exc}",
                remedy=(
                    f"Check that {self._root} exists and is writable by this user. "
                    "In a container, the runs directory must be chowned to the "
                    "non-root UID the image runs as."
                ),
                key=key,
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """``O_CREAT|O_EXCL``: genuinely atomic, unlike the COS equivalent."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise StorageError(
                "object_write_failed",
                "Could not create the object",
                f"{path}: {exc}",
                remedy=f"Check that {self._root} is writable by this user.",
                key=key,
            ) from exc
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:  # pragma: no cover
                    pass
        except OSError as exc:  # pragma: no cover - disk full mid-write
            path.unlink(missing_ok=True)
            raise StorageError(
                "object_write_failed",
                "Could not finish writing the object",
                f"{path}: {exc}",
                remedy="Check free space and permissions on the runs directory.",
                key=key,
            ) from exc
        return True

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(
                "object_not_found",
                "No such stored object",
                f"{path} does not exist.",
                remedy=(
                    "The run or upload may have been deleted. Reload the list and "
                    "pick an existing one."
                ),
                status=404,
                key=key,
            ) from exc
        except OSError as exc:
            raise StorageError(
                "object_read_failed",
                "Could not read from local storage",
                f"{path}: {exc}",
                remedy=f"Check permissions on {self._root}.",
                key=key,
            ) from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_keys(
        self,
        prefix: str,
        *,
        start_after: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        base = self._root / prefix
        # A prefix may be a partial name ("runs/2026") rather than a directory,
        # so walk the nearest existing directory and filter by string prefix.
        walk_root = base if base.is_dir() else base.parent
        if not walk_root.is_dir():
            return []

        found: list[str] = []
        for path in sorted(walk_root.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            key = path.relative_to(self._root).as_posix()
            if not key.startswith(prefix):
                continue
            if start_after is not None and key <= start_after:
                continue
            found.append(key)
        found.sort()
        if limit is not None:
            return found[: max(0, limit)]
        return found

    # -- diagnostics --------------------------------------------------------- #

    def probe(self) -> ObjectInfo | None:
        """Round-trip a marker object, so the probe proves writability."""
        key = ".arch2code-probe"
        stamp = datetime.now(timezone.utc).isoformat()
        self.put_bytes(key, stamp.encode("utf-8"))
        echoed = self.get_bytes(key).decode("utf-8")
        if echoed != stamp:  # pragma: no cover - would mean a broken filesystem
            raise StorageError(
                "storage_roundtrip_mismatch",
                "Local storage returned different bytes than were written",
                f"wrote {stamp!r}, read {echoed!r} at {self._path(key)}.",
                remedy="The filesystem under the runs root is not behaving. Do not "
                       "run the pipeline against it.",
            )
        self.delete(key)
        return ObjectInfo(key=key, size=len(stamp), last_modified=stamp)

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend, "root": str(self._root)}
