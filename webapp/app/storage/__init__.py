"""Storage backends, selected by one environment variable.

    ARCH2CODE_STORAGE_BACKEND = local | cos          (default: local)

**Local is the default and it stays the default.** Everything that runs on a
laptop today keeps running exactly as it does today: :mod:`app.store` and
:mod:`app.eventlog` still own ``webapp/runs`` and ``webapp/uploads``, still write
through ``os.replace``, still fsync before notifying a subscriber. Nothing in
this package is on the localhost path unless something asks for it.

The COS backend exists because IBM Code Engine has no persistent disk. It is
switched on by the deployment, not by the code:

    ARCH2CODE_STORAGE_BACKEND=cos
    ARCH2CODE_COS_BUCKET=arch2code-runs
    ARCH2CODE_COS_ENDPOINT=https://s3.us-south.cloud-object-storage.appdomain.cloud
    ARCH2CODE_COS_PREFIX=            # optional, for sharing one bucket

Usage::

    from app.storage import open_object_store

    store = open_object_store(settings)      # local unless the env says otherwise
    store.put_json("runs/20260721-2130-x/run.json", state)

Selection is at the boundary and never inside a helper: one call at startup, one
object passed around. A module that decides its own backend per call is a module
that will read from one place and write to another.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from .base import ObjectInfo, ObjectStore, StorageError
from .local import LocalObjectStore
from .publisher import (
    DEFAULT_INTERVAL_S,
    EventPublisher,
    EventReader,
    rebuild_local_log,
)
from .sync import download_prefix, upload_tree

log = logging.getLogger("arch2code.storage")

__all__ = [
    "ObjectStore",
    "ObjectInfo",
    "StorageError",
    "LocalObjectStore",
    "EventPublisher",
    "EventReader",
    "rebuild_local_log",
    "DEFAULT_INTERVAL_S",
    "upload_tree",
    "download_prefix",
    "open_object_store",
    "backend_name",
    "BACKENDS",
]

BACKENDS: tuple[str, ...] = ("local", "cos")

_ENV_VAR = "ARCH2CODE_STORAGE_BACKEND"


def backend_name(env: Mapping[str, str] | None = None) -> str:
    """The configured backend name, validated.

    An unknown value is a hard failure rather than a fallback to local: silently
    running a cloud deployment on ephemeral disk is the exact bug this package
    exists to prevent, and it would only show up when an instance restarts.
    """
    source = os.environ if env is None else env
    raw = (source.get(_ENV_VAR) or "local").strip().lower()
    if raw not in BACKENDS:
        raise StorageError(
            "unknown_storage_backend",
            "That storage backend does not exist",
            f"{_ENV_VAR}={raw!r} is not one of {', '.join(BACKENDS)}.",
            remedy=(
                f"Set {_ENV_VAR}=local for the laptop and the Plan B single-app "
                "deployment, or =cos for the split app+job deployment on Code "
                "Engine. Leave it unset to get 'local'."
            ),
        )
    return raw


def open_object_store(
    settings: Any = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path | str | None = None,
) -> ObjectStore:
    """Build the configured store.

    Args:
        settings: an :class:`app.config.Settings`, used only to locate the local
            root (``settings.webapp_root``). Optional so the job entrypoint and
            standalone tooling can call this before settings exist.
        env: environment override, for tests.
        root: explicit local root, wins over ``settings``.

    Returns:
        A ready store. The COS backend does **not** make a network call here;
        call :meth:`ObjectStore.probe` for that, which is what the health check
        and ``deploy.sh`` do.
    """
    source = os.environ if env is None else env
    name = backend_name(source)

    if name == "local":
        if root is not None:
            base = Path(root)
        elif settings is not None and getattr(settings, "webapp_root", None):
            # runs/ and uploads/ are siblings under webapp/, and the keys
            # ("runs/…", "uploads/…") reproduce that layout exactly, so an
            # existing tree is readable through this store with no migration.
            base = Path(settings.webapp_root)
        else:
            base = Path(__file__).resolve().parent.parent.parent
        store: ObjectStore = LocalObjectStore(base)
        log.debug("storage backend: local at %s", base)
        return store

    from .cos import CosConfig, CosObjectStore

    config = CosConfig.from_env(source)
    log.info(
        "storage backend: cos bucket=%s endpoint=%s auth=%s",
        config.bucket, config.endpoint, config.auth_mode,
    )
    return CosObjectStore(config)
