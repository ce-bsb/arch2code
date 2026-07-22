"""Error hierarchy and the handlers that render it.

The rule this project runs on: no failure reaches the user without a stated next
action. Every AppError therefore carries four things —

    code    a stable machine identifier the front end can branch on
    title   one short human line
    detail  what actually happened
    remedy  what to do about it

— and the front end renders `remedy` as the primary text. A condition that ends
in a spinner or a blank panel is a bug in this file.
"""

from __future__ import annotations

import logging
import re
import traceback
from dataclasses import dataclass
from typing import Any, Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import ConfigError as StartupConfigError
from .models import ErrorBody

log = logging.getLogger("arch2code.errors")

__all__ = [
    "AppError", "NotFound", "Conflict", "PreconditionFailed", "UpstreamError",
    "ConfigError", "BadRequest", "PayloadTooLarge", "UnsupportedMedia",
    "install_handlers", "bob_preflight_error",
    "PreflightVerdict", "classify_preflight_failure",
]


class AppError(Exception):
    """Base for every failure this app reports."""

    status: int = 400

    def __init__(
        self,
        code: str,
        title: str,
        detail: str,
        *,
        remedy: str | None = None,
        status: int | None = None,
        **context: Any,
    ) -> None:
        super().__init__(f"{code}: {title} — {detail}")
        self.code = code
        self.title = title
        self.detail = detail
        self.remedy = remedy
        if status is not None:
            self.status = status
        self.context: dict[str, Any] = context

    def to_body(self) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            title=self.title,
            detail=self.detail,
            remedy=self.remedy,
            context=_jsonable(self.context),
        )

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content=self.to_body().model_dump(mode="json"),
        )


class BadRequest(AppError):
    status = 400


class NotFound(AppError):
    status = 404


class Conflict(AppError):
    status = 409


class PayloadTooLarge(AppError):
    status = 413


class UnsupportedMedia(AppError):
    status = 415


class PreconditionFailed(AppError):
    """A health probe blocks the requested mode (HTTP 424)."""

    status = 424


class UpstreamError(AppError):
    """Bob or the MCP server misbehaved (HTTP 502)."""

    status = 502


class ConfigError(AppError):
    """The app is misconfigured (HTTP 500)."""

    status = 500


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of arbitrary context into JSON-safe data.

    Context is diagnostic, so it must never be the reason a request fails: a
    value that cannot be serialized degrades to its repr.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # pragma: no cover - defensive
            return repr(value)
    return str(value)


# ----------------------------------------------------------------------
# Bob pre-flight classification
# ----------------------------------------------------------------------

#: (needle in lowercased stderr, code, title, remedy)
_PREFLIGHT_SIGNATURES: tuple[tuple[str, str, str, str], ...] = (
    (
        "license",
        "bob_license_not_accepted",
        "Bob refused to start: licence not accepted",
        "The app passes --accept-license by default. If it was disabled, set "
        "ARCH2CODE_BOB_ACCEPT_LICENSE=1. To review the terms first, run "
        "`bob --show-license` in a terminal.",
    ),
    (
        "licence",
        "bob_license_not_accepted",
        "Bob refused to start: licence not accepted",
        "Set ARCH2CODE_BOB_ACCEPT_LICENSE=1, or run `bob --show-license` to "
        "review the terms first.",
    ),
    (
        "unauthor",
        "bob_auth_failed",
        "Bob could not authenticate",
        "Log in with `bob` interactively once, or check the API key in the "
        "environment. The app never stores or reads Bob credentials itself.",
    ),
    (
        "authentication",
        "bob_auth_failed",
        "Bob could not authenticate",
        "Log in with `bob` interactively once, then retry. Verify with "
        "`bob --list-sessions` in a terminal.",
    ),
    (
        "api key",
        "bob_auth_failed",
        "Bob could not authenticate",
        "Check the API key in the environment, or run `bob` interactively once "
        "to log in.",
    ),
    (
        "credential",
        "bob_auth_failed",
        "Bob could not authenticate",
        "Run `bob` interactively once to store credentials, then retry.",
    ),
    (
        "invalid values",
        "bob_unknown_chat_mode",
        "Bob rejected the --chat-mode value",
        "The arch2code modes come from .bob/custom_modes.yaml of Bob's working "
        "directory. Confirm ARCH2CODE_BOB_CWD points at the repository root — "
        "`bob --help` lists 10 chat modes there and only 4 anywhere else.",
    ),
    (
        "chat-mode",
        "bob_unknown_chat_mode",
        "Bob rejected the --chat-mode value",
        "The arch2code modes come from .bob/custom_modes.yaml of Bob's working "
        "directory. Confirm ARCH2CODE_BOB_CWD points at the repository root.",
    ),
    (
        "max-coins",
        "bob_max_coins_exceeded",
        "Bob stopped: the Bobcoin ceiling was exceeded",
        "Raise ARCH2CODE_BOB_MAX_COINS or clear it to remove the ceiling, then "
        "re-run the stage.",
    ),
    (
        "not found",
        "bob_binary_not_found",
        "Bob could not be executed",
        "Set ARCH2CODE_BOB_BIN to `bob` (on PATH) or to "
        "\"node /path/to/bundle/bob.js\".",
    ),
    (
        "enoent",
        "bob_binary_not_found",
        "Bob could not be executed",
        "Set ARCH2CODE_BOB_BIN to `bob` (on PATH) or to "
        "\"node /path/to/bundle/bob.js\".",
    ),
)


def bob_preflight_error(
    exit_code: int, stderr: str, argv: Sequence[str]
) -> AppError:
    """Classify a Bob failure into a coded error with a specific remedy.

    Why this exists: a pre-flight failure — invalid auth, an unaccepted licence,
    an unknown chat-mode slug — writes ZERO bytes to stdout, plain text to
    stderr and exits 1. The NDJSON stream is not an error channel, so without
    this classification "exit 1, no output" reaches the user as a blank panel.
    """
    text = (stderr or "").strip()
    haystack = text.lower()
    tail = text[-2000:]

    for needle, code, title, remedy in _PREFLIGHT_SIGNATURES:
        if needle in haystack:
            return UpstreamError(
                code,
                title,
                tail or f"Bob exited {exit_code} with no diagnostic output.",
                remedy=remedy,
                exit_code=exit_code,
                argv=list(argv),
                stderr_tail=tail,
            )

    if not text:
        return UpstreamError(
            "bob_failed_silently",
            f"Bob exited {exit_code} without writing anything",
            "The process produced no output on either stream, so there is no "
            "diagnostic to show.",
            remedy=(
                "Reproduce it by hand: copy the argv from the stage detail pane "
                "and run it in a terminal from Bob's working directory. If it "
                "works there but not here, retry with ARCH2CODE_BOB_PTY=1 — at "
                "least one Bob output path is conditioned on a TTY."
            ),
            exit_code=exit_code,
            argv=list(argv),
        )

    return UpstreamError(
        "bob_failed",
        f"Bob exited {exit_code}",
        tail,
        remedy=(
            "Read the stderr text above; it is Bob's own message. The exact "
            "command line is in the stage detail pane and can be re-run by hand "
            "from Bob's working directory."
        ),
        exit_code=exit_code,
        argv=list(argv),
        stderr_tail=tail,
    )


# ----------------------------------------------------------------------
# transient vs permanent pre-flight failure
# ----------------------------------------------------------------------

#: Observed in production, run 20260722-1526-e2e, stage 2 (arch-analyst), the
#: whole of stderr::
#:
#:     YOLO mode is enabled. All tool calls will be automatically approved.
#:     Failed to fetch team user budget - HTTP 401:  - {"message":"API Key
#:     verification failed: Authz service returned status 504 for API Key
#:     validation","error":"unauthorized"}
#:
#: Stage 1 had just finished with exit 0 and stage 3 ran afterwards with the
#: same key, so the key was valid the whole time. The 401 Bob reports is the
#: shape of the answer; the 504 inside it is the cause — IBM's authorization
#: service was unavailable for a few seconds. That distinction is the entire
#: reason this function exists: without it a few seconds of upstream downtime
#: costs a whole stage of reasoning.


@dataclass(frozen=True)
class PreflightVerdict:
    """Whether a pre-flight failure is worth attempting again.

    ``transient`` means the *server* was unavailable, not that it said no.
    ``marker`` names the evidence that decided it (a status code, an errno, a
    phrase) and ``reason`` is the sentence carried into the timeline, so a
    retry is never an unexplained second attempt.
    """

    transient: bool
    marker: str | None
    reason: str


#: A 3-digit status attached to a word that introduces one. The keyword prefix
#: is what keeps a run id, a byte count or a port number from being read as a
#: status. ``\D{0,12}?`` spans the punctuation and the word "status" that sit
#: between "returned" and the number in the message above.
_STATUS_RE = re.compile(
    r"\b(?:http|https|status|statuscode|status_code|code|returned|response|error)"
    r"\D{0,12}?(\d{3})\b",
    re.IGNORECASE,
)

#: A bare ``502 Bad Gateway`` with no introducing keyword at all.
_SERVER_PHRASE_RE = re.compile(
    r"\b(5\d{2})\s*[-:]?\s*"
    r"(bad gateway|service unavailable|gateway time-?out|internal server error)",
    re.IGNORECASE,
)

#: Rejections. Repeating one of these burns quota and delays the diagnosis, so
#: they are checked BEFORE the network vocabulary below: a genuine rejection
#: that happens to mention a timeout is still a rejection. It is checked AFTER
#: the 5xx scan, because a 5xx wrapped in a 401 is the case this file is for.
_PERMANENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("license", "licence not accepted"),
    ("licence", "licence not accepted"),
    ("invalid values", "invalid --chat-mode slug"),
    ("chat-mode", "invalid --chat-mode slug"),
    ("unknown mode", "invalid --chat-mode slug"),
    ("api key not found", "API key missing"),
    ("no api key", "API key missing"),
    ("missing api key", "API key missing"),
    ("api key is missing", "API key missing"),
    ("invalid api key", "API key rejected"),
    ("api key is invalid", "API key rejected"),
    ("malformed", "malformed credential"),
    ("expired", "credential expired"),
    ("revoked", "credential revoked"),
    ("quota", "quota exhausted"),
    ("insufficient funds", "quota exhausted"),
    ("insufficient credit", "quota exhausted"),
    ("out of credit", "quota exhausted"),
    ("budget exceeded", "quota exhausted"),
    ("max-coins", "Bobcoin ceiling exceeded"),
    ("forbidden", "access forbidden"),
    ("not entitled", "access forbidden"),
    ("permission denied", "access forbidden"),
    ("enoent", "binary not found"),
)

#: Server-side unavailability that carries no HTTP status: the transport gave
#: up before an answer existed. Every entry names a condition that is over in
#: seconds, or is not, and either way is not a decision about this request.
_TRANSIENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("eai_again", "EAI_AGAIN (DNS temporarily unavailable)"),
    ("econnreset", "ECONNRESET"),
    ("connection reset", "connection reset"),
    ("socket hang up", "socket hang up"),
    ("etimedout", "ETIMEDOUT"),
    ("esockettimedout", "ESOCKETTIMEDOUT"),
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("service unavailable", "service unavailable"),
    ("bad gateway", "bad gateway"),
    ("temporarily unavailable", "temporarily unavailable"),
    ("try again later", "upstream asked for a retry"),
)


def classify_preflight_failure(stderr: str) -> PreflightVerdict:
    """Decide whether a failed stage's stderr describes a retryable outage.

    The rule, in order:

    1. **Any 5xx anywhere wins**, even wrapped in a 401. A 5xx is the server
       saying it could not answer, which is not the same as saying no.
    2. Otherwise a **rejection marker** — no key, bad key, licence, unknown
       mode slug, quota — is permanent. 401/403 with no 5xx inside it lands
       here, and so does an unrecognized message.
    3. Otherwise a **transport marker** — EAI_AGAIN, ECONNRESET, socket hang
       up, a timeout — is transient.
    4. **In doubt, permanent.** Retrying a permanent failure spends quota and
       delays the diagnosis; not retrying a transient one only loses a stage
       that was already lost.

    Pure by construction: text in, verdict out, so the production string above
    can be asserted directly in a test.
    """
    haystack = (stderr or "").lower()
    if not haystack.strip():
        return PreflightVerdict(
            transient=False,
            marker=None,
            reason=(
                "Bob wrote nothing to stderr, so there is no evidence that the "
                "failure came from the server rather than from the request."
            ),
        )

    status = _server_side_status(haystack)
    if status is not None:
        return PreflightVerdict(
            transient=True,
            marker=f"HTTP {status}",
            reason=(
                f"The upstream service answered {status}, which is the server "
                "reporting it could not serve the request — not a rejection of "
                "it. A 5xx counts even when it arrives wrapped in a 401, which "
                "is exactly how the IBM authorization service reports being "
                "unavailable while validating an otherwise valid API key."
            ),
        )

    for needle, label in _PERMANENT_MARKERS:
        if needle in haystack:
            return PreflightVerdict(
                transient=False,
                marker=label,
                reason=(
                    f"stderr reports {label}, which is a rejection of the "
                    "request. Repeating it would fail identically and spend "
                    "quota doing so."
                ),
            )

    for needle, label in _TRANSIENT_MARKERS:
        if needle in haystack:
            return PreflightVerdict(
                transient=True,
                marker=label,
                reason=(
                    f"stderr reports {label}: the transport failed before any "
                    "answer existed, so nothing about this request was refused."
                ),
            )

    return PreflightVerdict(
        transient=False,
        marker=None,
        reason=(
            "stderr carries no sign of server-side unavailability, so the "
            "failure is treated as permanent. An unrecognized failure is not "
            "retried: repeating it costs quota and hides the real diagnosis."
        ),
    )


def _server_side_status(haystack: str) -> int | None:
    """The first 5xx status mentioned anywhere in the text, if any."""
    for match in _STATUS_RE.finditer(haystack):
        code = int(match.group(1))
        if 500 <= code <= 599:
            return code
    match = _SERVER_PHRASE_RE.search(haystack)
    if match:
        return int(match.group(1))
    return None


# ----------------------------------------------------------------------
# FastAPI wiring
# ----------------------------------------------------------------------


def install_handlers(app: FastAPI) -> None:
    """Map every exception class to the uniform ErrorBody envelope.

    Nothing may escape as an HTML traceback or an unlabelled 500: the front end
    parses ErrorBody and has no other error path.
    """

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log.warning("AppError %s: %s", exc.code, exc.detail)
        return exc.to_response()

    @app.exception_handler(StartupConfigError)
    async def _startup_config_error(
        _: Request, exc: StartupConfigError
    ) -> JSONResponse:
        return ConfigError(
            "config_invalid",
            "The app is misconfigured",
            str(exc),
            remedy=(
                "Fix the offending ARCH2CODE_* variable and restart with "
                "./run.sh. webapp/.env.example documents every variable."
            ),
        ).to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problems = []
        for err in exc.errors():
            location = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            problems.append(f"{location or '<body>'}: {err.get('msg', 'invalid')}")
        return JSONResponse(
            status_code=422,
            content=ErrorBody(
                code="request_invalid",
                title="The request body was rejected",
                detail="; ".join(problems) or "The payload did not match the schema.",
                remedy=(
                    "Correct the highlighted fields and resubmit. The accepted "
                    "shapes are in app/models.py, which both sides share."
                ),
                context={"problems": problems},
            ).model_dump(mode="json"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorBody(
                code=f"http_{exc.status_code}",
                title=detail,
                detail=detail,
                remedy=(
                    "Check the URL. Every API route lives under /api; anything "
                    "else is served from webapp/static."
                    if exc.status_code == 404
                    else None
                ),
            ).model_dump(mode="json"),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("Unhandled %s: %s\n%s", type(exc).__name__, exc,
                  traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content=ErrorBody(
                code="internal_error",
                title="The app hit an unhandled error",
                detail=f"{type(exc).__name__}: {exc}",
                remedy=(
                    "This is a defect. The full traceback is in the terminal "
                    "running ./run.sh — copy it into the bug report."
                ),
            ).model_dump(mode="json"),
        )
