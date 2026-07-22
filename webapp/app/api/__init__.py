"""Router aggregation.

CONVENTION, and every router in this package must follow it: each sub-router
declares `prefix="/api"` itself, and this module aggregates them WITHOUT adding
another prefix. main.py then includes `api.router` plainly. Getting this wrong
in either direction produces /api/api/... routes that 404 with no explanation.

Routers are included defensively. This app is built by several people at once,
and a half-integrated tree must still start: a missing router module is recorded
in MISSING_ROUTERS and surfaced at startup and in the logs, rather than taking
the whole server down and hiding the health page that would have explained it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

log = logging.getLogger("arch2code.api")

__all__ = ["router", "MISSING_ROUTERS", "INCLUDED_ROUTERS"]

router = APIRouter()

#: Sub-modules of this package, in the order their routes are registered.
#: Order matters only where paths could shadow one another; today they do not.
_MODULES = ("health", "uploads", "runs", "stream", "vision", "export")

MISSING_ROUTERS: list[str] = []
INCLUDED_ROUTERS: list[str] = []

for _name in _MODULES:
    try:
        _module = __import__(f"{__name__}.{_name}", fromlist=["router"])
        router.include_router(getattr(_module, "router"))
    except Exception as exc:  # noqa: BLE001 - integration-time resilience
        MISSING_ROUTERS.append(f"{_name}: {type(exc).__name__}: {exc}")
        log.error(
            "API router %r could not be loaded (%s: %s). Its endpoints will "
            "return 404 until it is fixed.", _name, type(exc).__name__, exc,
        )
    else:
        INCLUDED_ROUTERS.append(_name)
