"""FastAPI application construction.

Startup order matters and is deliberate:

  1. load_settings() and create the directories the app owns.
  2. Build the stores, the vision client, the health cache and the runner onto
     app.state, where the dependency getters and every router find them.
  3. Run the health probes ONCE and log a readable summary.

A failing probe NEVER prevents startup. The UI has to be reachable in order to
show what is broken — an app that refuses to start because Bob is missing is an
app that cannot tell you Bob is missing.

No CORS middleware and no authentication: this binds to 127.0.0.1 and is a local
tool. Bob's licence (clause 53d) permits use by the licensee, its employees and
its contractors and forbids providing hosting or a commercial service to third
parties, so the absence of auth is a consequence of the positioning rather than
an oversight to be fixed with a reverse proxy.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api
from .config import Settings, load_settings
from .errors import AppError, install_handlers
from .eventlog import EventLogRegistry
from .health import HealthCache

log = logging.getLogger("arch2code")

__all__ = ["create_app", "app", "lifespan", "get_settings", "get_store",
           "get_uploads", "get_health", "get_runner", "get_vision"]


def _configure_logging() -> None:
    level = os.environ.get("ARCH2CODE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_components(app: FastAPI, settings: Settings) -> list[str]:
    """Instantiate the collaborators onto app.state.

    Each is wired independently and failures are collected rather than raised:
    during integration one module may be absent, and the app must still come up
    far enough to serve /api/health and say so.
    """
    problems: list[str] = []
    app.state.settings = settings
    app.state.events = EventLogRegistry(settings.runs_root)
    app.state.health = HealthCache(settings)
    app.state.uploads = None
    app.state.store = None
    app.state.vision = None
    app.state.runner = None

    try:
        from .store import RunStore, UploadStore

        app.state.uploads = UploadStore(settings)
        app.state.store = RunStore(settings, app.state.events)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"store: {type(exc).__name__}: {exc}")
        log.error("could not build the run/upload stores: %s", exc)

    try:
        from .vision import ArchVisionClient

        app.state.vision = ArchVisionClient(settings)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"vision: {type(exc).__name__}: {exc}")
        log.error("could not build the vision client: %s", exc)

    if app.state.store is not None:
        try:
            from .pipeline import PipelineRunner

            app.state.runner = PipelineRunner(
                settings, app.state.store, app.state.health, app.state.vision
            )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"pipeline: {type(exc).__name__}: {exc}")
            log.error("could not build the pipeline runner: %s", exc)

    problems.extend(api.MISSING_ROUTERS)
    app.state.wiring_problems = problems
    return problems


def _log_health_summary(report: Any) -> None:
    markers = {"ok": " ok ", "warn": "warn", "error": "FAIL"}
    for probe in getattr(report, "probes", []):
        log.info("[%s] %-22s %s",
                 markers.get(probe.level, " ?? "), probe.id, probe.title)
        if probe.level != "ok" and probe.remedy:
            log.info("        remedy: %s", probe.remedy)
    if report.blocking_failures:
        log.warning(
            "%d probe(s) block a mode. The UI will say which, and the affected "
            "mode is disabled until you press Retry in the health banner.",
            report.blocking_failures,
        )
    else:
        log.info("health: everything the app needs is present.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.ensure_dirs()

    problems = _build_components(app, settings)
    if problems:
        log.error("wiring problems: %s", "; ".join(problems))

    log.info("arch2code webapp %s", __version__)
    log.info("  project root : %s", settings.project_root)
    log.info("  bob cwd      : %s", settings.bob_cwd)
    log.info("  bob binary   : %s",
             " ".join(settings.bob_bin) if settings.bob_bin else "<not configured>")
    log.info("  interpreter  : %s", settings.python_bin)
    log.info("  runs         : %s", settings.runs_root)

    try:
        report = await app.state.health.refresh()
        _log_health_summary(report)
    except Exception:  # noqa: BLE001 - startup must not die on a probe
        log.exception("the health check itself failed; continuing anyway")

    if settings.bob_cwd == settings.project_root:
        log.info(
            "Mode B writes into %s/.arch/ — that is the pipeline's audit trail, "
            "not the app editing your repo. Set ARCH2CODE_BOB_CWD to point a "
            "demo at a scratch clone.", settings.project_root,
        )

    try:
        yield
    finally:
        runner = getattr(app.state, "runner", None)
        if runner is not None:
            try:
                await runner.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("pipeline runner shutdown failed")
        events = getattr(app.state, "events", None)
        if events is not None:
            events.detach_all()
        log.info("arch2code webapp stopped.")


# ----------------------------------------------------------------------
# Dependency getters
# ----------------------------------------------------------------------


def _from_state(request: Request, name: str, missing_hint: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise AppError(
            "component_unavailable",
            "A part of the app failed to start",
            f"app.state.{name} is not available. {missing_hint}",
            remedy=(
                "Read the startup log in the terminal running ./run.sh: it "
                "names the module that failed to load and why."
            ),
            status=500,
        )
    return value


def get_settings(request: Request) -> Settings:
    return _from_state(request, "settings", "Configuration did not load.")


def get_store(request: Request):
    return _from_state(request, "store", "app/store.py did not import.")


def get_uploads(request: Request):
    return _from_state(request, "uploads", "app/store.py did not import.")


def get_health(request: Request) -> HealthCache:
    return _from_state(request, "health", "The health cache was not built.")


def get_runner(request: Request):
    return _from_state(request, "runner", "app/pipeline.py did not import.")


def get_vision(request: Request):
    return _from_state(request, "vision", "app/vision.py did not import.")


# ----------------------------------------------------------------------
# Application factory
# ----------------------------------------------------------------------

_PLACEHOLDER = """<!doctype html>
<meta charset="utf-8">
<title>arch2code — front end not built</title>
<style>
  body { font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
         background:#12141a; color:#e6e8ee; margin:0; padding:3rem; }
  code { background:#1c2029; padding:.15em .4em; border-radius:4px; }
  a { color:#7fb2ff; }
</style>
<h1>arch2code webapp is running</h1>
<p>The API is up, but <code>webapp/static/index.html</code> does not exist yet,
   so there is no interface to serve.</p>
<p>The backend is reachable regardless:</p>
<ul>
  <li><a href="/api/health">/api/health</a> — every environment probe, with a
      remedy for each failure</li>
  <li><a href="/docs">/docs</a> — the generated OpenAPI surface</li>
</ul>
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    _configure_logging()
    resolved = settings or load_settings()
    resolved.ensure_dirs()

    app = FastAPI(
        title="arch2code",
        version=__version__,
        summary="Local tool that turns an architecture diagram into a reviewed, "
                "scaffolded implementation by driving the Bob arch2code pipeline.",
        lifespan=lifespan,
    )
    # Set before lifespan runs so create_app(settings) is honoured.
    app.state.settings = resolved

    install_handlers(app)
    app.include_router(api.router)

    index = resolved.static_root / "index.html"
    if not index.exists():
        # Registered BEFORE the mount, so it wins the "/" match. Without it the
        # user gets a bare 404 and no clue that the API is fine.
        log.warning(
            "%s does not exist; serving a placeholder at / until the front end "
            "is built.", index,
        )

        @app.get("/", include_in_schema=False)
        async def _placeholder() -> HTMLResponse:  # pragma: no cover - trivial
            return HTMLResponse(_PLACEHOLDER)

    # Mounted LAST at "/" so it can never shadow /api.
    app.mount(
        "/",
        StaticFiles(directory=str(resolved.static_root), html=True),
        name="static",
    )
    return app


app = create_app()
