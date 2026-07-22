"""Mirroring a directory tree to and from an object store.

Bob is a subprocess. It resolves ``--chat-mode`` from the ``.bob/`` of its
working directory, reads the drawing from a real path and writes ``.arch/`` to a
real path. None of that changes because the deployment target changed, so the
job's shape is:

    download what the run needs  ->  run the pipeline on ephemeral disk  ->
    upload everything it produced

These two functions are that first and third step. They are deliberately dumb:
no delta detection, no manifest, no parallel transfer. A run produces tens of
files, not tens of thousands, and a clever sync that skips a changed file is a
lost audit trail.

One rule is enforced rather than documented: :func:`upload_tree` refuses to
upload a file whose name matches a credential file. A job that syncs its CWD
after a stage must not be one ``cp`` away from putting ``.env`` in a bucket.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Callable, Iterable

from .base import ObjectStore, StorageError

log = logging.getLogger("arch2code.storage.sync")

__all__ = ["upload_tree", "download_prefix", "SKIP_NAMES", "SKIP_SUFFIXES"]

#: Never uploaded, whatever the caller asks for. webapp/.env and
#: mcp/arch_vision/.env hold real watsonx credentials.
SKIP_NAMES: frozenset[str] = frozenset(
    {".env", ".env.local", ".netrc", "id_rsa", "credentials", ".git"}
)

#: Noise that would otherwise triple the object count of a run.
SKIP_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".tmp", ".swp", ".DS_Store")


def _skip(path: Path) -> bool:
    name = path.name
    if name in SKIP_NAMES or name.endswith(".env"):
        return True
    if any(part in SKIP_NAMES for part in path.parts):
        return True
    return name.endswith(SKIP_SUFFIXES)


def _content_type(path: Path) -> str | None:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    if path.suffix in (".ndjson", ".jsonl"):
        return "application/x-ndjson"
    if path.suffix in (".md", ".txt"):
        return "text/plain; charset=utf-8"
    return None


def upload_tree(
    store: ObjectStore,
    local_dir: Path,
    prefix: str,
    *,
    max_bytes: int = 64 * 1024 * 1024,
    on_file: Callable[[str, int], None] | None = None,
) -> list[str]:
    """Upload every file under ``local_dir`` to ``prefix``. Returns the keys.

    A missing directory is not an error: a stage that legitimately wrote nothing
    must not fail the run at sync time. It returns an empty list, and the caller
    that cares about a specific artifact checks for that artifact.

    ``max_bytes`` guards against a single pathological file (a core dump, a
    LibreOffice profile) turning one run into a large storage bill. Anything
    larger is skipped with a warning rather than silently truncated.
    """
    root = Path(local_dir)
    if not root.is_dir():
        return []
    if not prefix.endswith("/"):
        prefix += "/"

    written: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or _skip(path):
            continue
        size = path.stat().st_size
        if size > max_bytes:
            log.warning(
                "skipping %s: %d bytes exceeds the %d byte per-object ceiling",
                path, size, max_bytes,
            )
            continue
        key = f"{prefix}{path.relative_to(root).as_posix()}"
        store.put_file(key, path, content_type=_content_type(path))
        written.append(key)
        if on_file is not None:
            on_file(key, size)
    return written


def download_prefix(
    store: ObjectStore,
    prefix: str,
    local_dir: Path,
    *,
    required: Iterable[str] = (),
) -> list[Path]:
    """Materialize everything under ``prefix`` into ``local_dir``.

    ``required`` names keys (relative to ``prefix``) that must exist. Missing one
    raises immediately with the key named, because the alternative is a Bob stage
    that starts, cannot find its input, and fails 90 seconds and 37 000 tokens
    later with a message about a file path.
    """
    if not prefix.endswith("/"):
        prefix += "/"
    target = Path(local_dir)
    target.mkdir(parents=True, exist_ok=True)

    keys = list(store.iter_prefix(prefix))
    missing = [name for name in required if f"{prefix}{name}" not in keys]
    if missing:
        raise StorageError(
            "run_input_missing",
            "The run's input is not in the store",
            (
                f"{', '.join(prefix + name for name in missing)} was not found among "
                f"the {len(keys)} object(s) under {prefix}."
            ),
            remedy=(
                "The app writes these when the run is created. If this is a job run "
                "started by hand, create the run through the API first, or check that "
                "the job and the app point at the same ARCH2CODE_COS_BUCKET and "
                "ARCH2CODE_COS_PREFIX."
            ),
            status=409,
            prefix=prefix,
        )

    out: list[Path] = []
    for key in keys:
        relative = key[len(prefix):]
        if not relative:
            continue
        out.append(store.get_file(key, target / relative))
    return out
