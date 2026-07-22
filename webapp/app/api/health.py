"""Health endpoints.

The front end calls GET /api/health on load and renders the banner from it,
before offering either mode. POST /api/health/recheck exists so that fixing an
environment variable or installing an interpreter never requires restarting the
app — the demo machine gets fixed while the page stays open.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..errors import AppError
from ..health import HealthCache
from ..models import HealthReport

router = APIRouter(prefix="/api", tags=["health"])


def get_health(request: Request) -> HealthCache:
    cache = getattr(request.app.state, "health", None)
    if cache is None:  # pragma: no cover - only if the lifespan did not run
        raise AppError(
            "app_not_initialised",
            "The application did not finish starting",
            "app.state.health is missing.",
            remedy="Restart the server with ./run.sh and read the startup log.",
            status=500,
        )
    return cache


@router.get("/health", response_model=HealthReport)
async def get_health_report(
    health: HealthCache = Depends(get_health),
) -> HealthReport:
    """The report produced at startup, served from cache.

    Cheap on purpose: this is polled by the UI and must not spawn a Bob process
    on every call. Use /api/health/recheck to actually re-probe.
    """
    return await health.get()


@router.post("/health/recheck", response_model=HealthReport)
async def recheck_health(
    health: HealthCache = Depends(get_health),
) -> HealthReport:
    """Re-run every probe and replace the cached report."""
    return await health.refresh()
