"""Every request/response model and enum in the app.

This module is the single source of truth for the JSON shapes shared by the
backend and the front end. If a shape is not described here, no endpoint should
be returning it.

Design rules that apply throughout:
  * Nothing here carries a secret. StageDetail exposes env_keys (names only).
  * Optional data is optional, not fabricated: a stage that produced no stats
    reports stats=None rather than zeros, and an artifact that was never
    written is returned with exists=false and the path that was expected.
  * Tolerance is structural. Payloads passed through from Bob or from the MCP
    server land in dict[str, Any] fields so that a shape drift degrades the UI
    rather than raising a 500.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# app/ingest imports nothing from app, which is what makes this direction safe.
# See app/ingest/errors.py for why the subpackage is kept free of app imports.
from .ingest.models import IngestSummary, PageRef

__all__ = [
    "IngestSummary", "PageRef",
    "RunMode", "RunStatus", "StageStatus", "StageId", "SourceKind",
    "GateVerdict", "GateChoice", "ProbeLevel", "VerifyVerdict", "ArtifactKind",
    "ErrorBody", "Routing", "UploadRef", "RunOptions", "CreateRunRequest",
    "ArtifactRef", "StageStats", "StageState", "GateState", "RunTotals",
    "RunState", "RunSummary", "GateDecision", "Event", "ProbeResult",
    "HealthReport", "CaptureManifest", "QualitySummary", "VisionPreview",
    "VerifyRequest", "VerifyRecord", "StageDetail", "EventPage",
    "RunListResponse", "UploadListResponse", "ArtifactListResponse",
    "VerificationListResponse", "TERMINAL_RUN_STATUSES",
]

# ----------------------------------------------------------------------
# Vocabularies
# ----------------------------------------------------------------------

RunMode = Literal["vision", "pipeline"]
RunStatus = Literal[
    "created", "running", "awaiting_input", "blocked",
    "succeeded", "failed", "cancelled",
]
StageStatus = Literal["pending", "running", "succeeded", "failed", "skipped", "blocked"]
StageId = Literal[
    "capture", "extract", "intake", "analyst", "critic", "scaffold", "validator"
]
SourceKind = Literal["napkin", "whiteboard", "screenshot", "pdf"]
GateVerdict = Literal["approved", "blocked", "absent"]
GateChoice = Literal["approve", "block", "send_back"]
ProbeLevel = Literal["ok", "warn", "error"]
VerifyVerdict = Literal["true", "false", "uncertain", "error"]
ArtifactKind = Literal[
    "extraction", "air", "verdict", "manifest",
    "pipeline_log", "validation", "code", "other",
]

#: A run in one of these states will never move again on its own, so the SSE
#: stream may close and the run picker may stop polling it.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"blocked", "succeeded", "failed", "cancelled"}
)


class _Model(BaseModel):
    """Base with the serialization behaviour every model in this app wants."""

    model_config = ConfigDict(populate_by_name=True, ser_json_timedelta="float")


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


class ErrorBody(_Model):
    """The uniform error envelope. Every failure in this app is one of these.

    `remedy` is not decoration: the front end renders it as the primary text,
    because a failure without a stated next action is a failure the user cannot
    act on.
    """

    code: str
    title: str
    detail: str
    remedy: str | None = None
    context: dict[str, Any] = {}


# ----------------------------------------------------------------------
# Uploads
# ----------------------------------------------------------------------


class Routing(_Model):
    """Where an artifact should be extracted from.

    Mirrors capture_diagram.route(). extraction_path is one of
    vision | deterministic | hybrid | unknown. If this table ever diverges from
    the script's, the UI lies to the user about which mode is legal.
    """

    extraction_path: str
    source_kind: str
    recommended_tool: str


class UploadRef(_Model):
    upload_id: str
    filename: str
    content_type: str
    bytes: int
    sha256: str
    stored_path: str
    routing: Routing
    #: What the file actually is, decided from its bytes rather than its name.
    #: Optional so that upload records written before app/ingest existed still
    #: load; every new upload has one.
    ingest: IngestSummary | None = None
    #: One entry per page/tab/frame. Present so the UI can ask *which page* for a
    #: multi-page PDF instead of guessing — each page it guesses wrong is a
    #: wasted vision call.
    pages: list[PageRef] = []
    #: Structured sources for the same drawing (e.g. sketch.drawio next to
    #: sketch.png). Their presence makes the vision path a waste of tokens.
    structured_siblings: list[str] = []
    warnings: list[str] = []
    created_at: datetime


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------


class RunOptions(_Model):
    use_pty: bool | None = None
    max_coins: int | None = None
    stage_timeout_s: float | None = None
    #: Typed as the literal False on purpose. The stage-3 gate is always a human
    #: decision; there is no configuration that turns it into an auto-approve.
    auto_advance_gate: Literal[False] = False


class CreateRunRequest(_Model):
    #: Defaults to the full pipeline. "vision" is still a legal value and still
    #: executes VISION_STAGES, because runs created before this default exists
    #: are on disk with mode="vision" and must keep loading and exporting. What
    #: changed is that the UI no longer offers the choice: reading a drawing is
    #: a STAGE of the pipeline, not a product anybody asked for.
    mode: RunMode = "pipeline"
    upload_id: str
    slug: str | None = Field(None, max_length=24, pattern=r"^[a-z0-9][a-z0-9-]*$")
    source_kind: SourceKind = "screenshot"
    hint: str | None = Field(None, max_length=500)
    options: RunOptions = RunOptions()


class ArtifactRef(_Model):
    artifact_id: str
    stage: StageId | None = None
    kind: str
    path: str
    rel_path: str
    bytes: int = 0
    mtime: datetime | None = None
    media_type: str = "application/octet-stream"
    #: False is a legitimate and highly informative result: it is exactly what a
    #: stage that exited 0 without writing its contracted artifact looks like.
    exists: bool = False


class StageStats(_Model):
    """Usage reported by a stream-json `result` line.

    Every field is optional because the shapes were read out of the Bob bundle
    rather than observed at runtime. All-None is a valid, non-exceptional value.
    """

    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    #: ACCOUNT-level running total, not this run's. Bob reports it on every
    #: result line, and it is the single most useful number here: when spend
    #: approaches max_budget the backend stops answering and the stage hangs
    #: with no error, no stderr and no exit. That failure is indistinguishable
    #: from a slow model unless this is on screen.
    budget_spend: float | None = None
    max_budget: float | None = None
    session_costs: Any | None = None


class StageState(_Model):
    id: StageId
    index: int
    title: str
    slug: str | None = None
    status: StageStatus = "pending"
    approval_mode: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    #: The source of truth for stage outcome. Never inferred from the NDJSON.
    exit_code: int | None = None
    used_pty: bool = False
    #: Exit 0 with zero bytes on stdout. Reported regardless of exit code,
    #: because it is the signature of the TTY-conditioned output path.
    empty_stdout: bool = False
    stdout_lines: int = 0
    #: How many times the subprocess was started for this stage. Always 1 unless
    #: a transient pre-flight failure earned the single allowed retry, and kept
    #: on disk so a reloaded page still shows that two attempts were paid for.
    attempts: int = 1
    stderr_tail: str = ""
    artifacts: list[ArtifactRef] = []
    stats: StageStats | None = None
    error: ErrorBody | None = None


class GateState(_Model):
    """The stage-3 gate, as read from verdict.md and as decided by a human.

    verdict="absent" means verdict.md never contained the gate string at all.
    That is a real and common case — neither historical run in .arch/ contains
    it — and it must reach the human as a defect of the run, never as a
    default-approve.
    """

    verdict: GateVerdict
    gate_line: str | None = None
    verdict_artifact_id: str | None = None
    verdict_excerpt: str = ""
    decided: bool = False
    decision: GateChoice | None = None
    #: True when the human decision contradicts the parsed verdict. This is the
    #: flag an auditor looks for.
    override: bool = False
    reason: str | None = None
    resume_from: StageId | None = None
    decided_at: datetime | None = None


class RunTotals(_Model):
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    coins: float | None = None


class RunState(_Model):
    run_id: str
    mode: RunMode
    status: RunStatus
    slug: str
    created_at: datetime
    updated_at: datetime
    upload: UploadRef
    source_kind: SourceKind
    hint: str | None = None
    options: RunOptions = RunOptions()
    project_root: str
    #: The CWD every Bob subprocess of this run used. Part of the contract:
    #: Bob resolves --chat-mode from the .bob/custom_modes.yaml of this path.
    bob_cwd: str
    last_event_id: int = 0
    stages: list[StageState] = []
    gate: GateState | None = None
    totals: RunTotals = RunTotals()
    error: ErrorBody | None = None


class RunSummary(_Model):
    run_id: str
    mode: RunMode
    status: RunStatus
    slug: str
    created_at: datetime
    updated_at: datetime
    source_filename: str
    current_stage: StageId | None = None
    stages_done: int = 0
    stages_total: int = 0
    last_event_id: int = 0


class GateDecision(_Model):
    decision: GateChoice
    #: Required when the decision contradicts the parsed verdict. Enforced in
    #: the endpoint, where the parsed verdict is known.
    reason: str | None = Field(None, max_length=2000)
    resume_from: StageId | None = None


# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------


class Event(_Model):
    """One line of webapp/runs/<run_id>/events.jsonl.

    The same envelope is used by SSE frames and by the JSON replay endpoint, so
    the client reducer is shared between the live and the replay paths.
    """

    id: int
    ts: datetime
    run_id: str
    stage: StageId | None = None
    type: str
    data: dict[str, Any] = {}


class EventPage(_Model):
    events: list[Event] = []
    next_after: int = 0
    #: True when the run is terminal and every event has been delivered.
    complete: bool = False


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


class ProbeResult(_Model):
    id: str
    level: ProbeLevel
    title: str
    detail: str
    remedy: str | None = None
    #: Which modes this probe makes impossible. A broken Bob install blocks
    #: "pipeline" but leaves "vision" entirely usable, and vice versa.
    blocks: list[RunMode] = []
    data: dict[str, Any] = {}


class HealthReport(_Model):
    ok: bool
    checked_at: datetime
    blocking_failures: int
    probes: list[ProbeResult] = []

    def blocks(self, mode: RunMode) -> list[ProbeResult]:
        """Probes that make `mode` impossible right now."""
        return [p for p in self.probes if p.level == "error" and mode in p.blocks]


# ----------------------------------------------------------------------
# Vision (Mode A)
# ----------------------------------------------------------------------


class CaptureManifest(_Model):
    """capture-manifest.json as written by capture_diagram.py.

    Extra keys are tolerated on purpose: the script is owned by the harness, not
    by this app, and gaining a field there must not break a run here.
    """

    model_config = ConfigDict(extra="allow")

    run_id: str
    source_artifact: str
    source_sha256: str
    source_kind: str
    extraction_path: str
    next_tool: str
    captured_at: str
    bytes: int
    warnings: list[str] = []
    normalized_artifact: str | None = None
    normalization: dict[str, Any] | None = None
    working_copy: str | None = None
    structured_sibling: list[str] = []


class QualitySummary(_Model):
    """_quality from the extraction, plus two client-facing derivations.

    These are the point of Mode A, not a footnote: a connection pointing at a
    component that does not exist, or a connection the model itself is unsure
    about, is exactly what a human reviewer needs surfaced above the fold.
    """

    broken_refs: list[str] = []
    connections_needing_verification: list[str] = []
    action_required: str | None = None
    #: Derived: present in components[] but referenced by no connection.
    orphan_components: list[str] = []
    #: Derived: component confidence < 0.85.
    low_confidence_components: list[str] = []


class VerifyRequest(_Model):
    target_kind: Literal["connection", "component", "free"]
    target_id: str | None = None
    #: Auto-composed from the target when omitted, using the literal label_text
    #: read off the drawing.
    claim: str | None = Field(None, min_length=10, max_length=500)


class VerifyRecord(_Model):
    """One second-pass check: a different prompt, a different pass, same image.

    verdict "false" and "uncertain" are legitimate answers and are rendered as
    findings. The UI must never soften them into a pass.
    """

    verification_id: str
    target_kind: str
    target_id: str | None = None
    claim: str
    verdict: VerifyVerdict
    confidence: float | None = None
    observed: str | None = None
    contradiction: str | None = None
    action: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    duration_ms: int = 0
    created_at: datetime
    #: The original extraction entry for this id, so the UI can put extract and
    #: verify side by side.
    extract_side: dict[str, Any] | None = None
    raw: dict[str, Any] = {}


class VisionPreview(_Model):
    run_id: str
    status: RunStatus
    image: dict[str, Any] = {}
    capture: CaptureManifest | None = None
    #: Passed through with tolerant defaults: a missing key becomes an empty
    #: list, never a 500.
    extraction: dict[str, Any] = {}
    provenance: dict[str, Any] | None = None
    quality: QualitySummary = QualitySummary()
    verifications: list[VerifyRecord] = []
    raw_available: bool = False
    error: ErrorBody | None = None


# ----------------------------------------------------------------------
# Debug / list envelopes
# ----------------------------------------------------------------------


class StageDetail(_Model):
    """The debugging pane.

    argv is exactly what was executed, so a failure can be reproduced by hand in
    a terminal — the first thing anyone asks when Bob exits 1 with an empty
    stdout. env_keys lists variable NAMES only and never values.
    """

    stage: StageState
    argv: list[str] = []
    cwd: str = ""
    env_keys: list[str] = []
    stdout_tail: list[str] = []
    stderr_tail: str = ""
    ndjson_path: str = ""
    stderr_path: str = ""


class RunListResponse(_Model):
    runs: list[RunSummary] = []


class UploadListResponse(_Model):
    uploads: list[UploadRef] = []


class ArtifactListResponse(_Model):
    artifacts: list[ArtifactRef] = []


class VerificationListResponse(_Model):
    verifications: list[VerifyRecord] = []
