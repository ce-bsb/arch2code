"""Tolerant parser for Bob's ``--output-format stream-json`` NDJSON stream.

Design constraint, stated once so it is never forgotten while editing this file:
the exact shapes of Bob's stream-json objects were read out of the distributed
bundle, not observed at runtime. Therefore nothing in this module is allowed to
raise. Every read is a ``.get()``, the raw line is always preserved verbatim, an
unrecognised ``type`` degrades to ``bob.unknown`` and a line that is not JSON at
all degrades to ``bob.unknown`` with ``parse_error`` set.

If Bob's shapes drift, the timeline degrades to raw lines. It never breaks a run.
The NDJSON is narration; the process exit code is the source of truth.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .models import StageStats

__all__ = [
    "KNOWN_TYPES",
    "StreamEvent",
    "LineAccumulator",
    "MessageCoalescer",
    "normalize_line",
    "to_event_payload",
    "extract_stats",
    "preview",
]

#: The event types documented in the bundle. Anything else becomes "unknown".
KNOWN_TYPES: frozenset[str] = frozenset(
    {"init", "message", "tool_use", "tool_result", "error", "result"}
)

#: Mapping from a known stream-json type to the run event vocabulary name.
_EVENT_NAME_BY_TYPE: Mapping[str, str] = {
    "init": "bob.init",
    "message": "bob.message",
    "tool_use": "bob.tool_use",
    "tool_result": "bob.tool_result",
    "error": "bob.error",
    "result": "bob.result",
}

_UNKNOWN_EVENT_NAME = "bob.unknown"

_DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class StreamEvent:
    """One normalized line of the stream.

    Attributes:
        type: A member of :data:`KNOWN_TYPES`, or ``"unknown"``.
        observed_type: Whatever the line actually carried in ``type`` (may be
            ``None`` when the line had no ``type`` key or was not JSON).
        known: ``True`` when ``type`` is a member of :data:`KNOWN_TYPES`.
        raw: The original line, always preserved, never trimmed of content.
        payload: The parsed JSON object, or ``None`` when the line was not a
            JSON object.
        parse_error: Why parsing failed, when it did. ``None`` on success.
    """

    type: str
    observed_type: str | None
    known: bool
    raw: str
    payload: dict[str, Any] | None
    parse_error: str | None


class LineAccumulator:
    """Turns arbitrary byte chunks from a pipe or a pty into whole lines.

    A subprocess pipe hands us chunk boundaries that have nothing to do with
    line boundaries, so a partial tail is buffered until its newline arrives.
    ``\\r\\n`` is tolerated (a pty in cooked mode translates ``\\n`` to
    ``\\r\\n`` on output). Decoding uses ``errors="replace"`` so a malformed
    byte can never abort a run.

    An over-long line (a stage writing a whole file into one JSON blob) is
    truncated at ``max_line_bytes`` and the remainder up to the next newline is
    discarded, so a pathological stream cannot exhaust memory.
    """

    def __init__(self, max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES) -> None:
        self.max_line_bytes = max(1024, int(max_line_bytes))
        self._buf = bytearray()
        self._overflowed = False
        self.truncated_lines = 0

    def feed(self, chunk: bytes) -> list[str]:
        """Append ``chunk`` and return every complete line it closed."""
        if not chunk:
            return []
        self._buf.extend(chunk)
        lines: list[str] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if self._overflowed:
                # Tail of a line we already emitted truncated: drop it.
                self._overflowed = False
                continue
            lines.append(self._decode(raw))
        if len(self._buf) > self.max_line_bytes:
            raw = bytes(self._buf[: self.max_line_bytes])
            del self._buf[:]
            self._overflowed = True
            self.truncated_lines += 1
            lines.append(self._decode(raw))
        return lines

    def flush(self) -> list[str]:
        """Return whatever is left when the stream closes without a newline."""
        if self._overflowed:
            self._buf.clear()
            self._overflowed = False
            return []
        if not self._buf:
            return []
        raw = bytes(self._buf)
        self._buf.clear()
        line = self._decode(raw)
        return [line] if line else []

    @staticmethod
    def _decode(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace").rstrip("\r")


def normalize_line(line: str) -> StreamEvent | None:
    """Normalize one raw line. Returns ``None`` for a blank line.

    Never raises and never indexes into a dict. A line that is not a JSON
    object -- truncated JSON, a stray banner, a progress bar -- comes back as an
    unknown event carrying the original text.
    """
    if line is None:
        return None
    stripped = line.strip()
    if not stripped:
        return None

    try:
        parsed = json.loads(stripped)
    except Exception as exc:  # noqa: BLE001 - any decoding failure is tolerated
        return StreamEvent(
            type="unknown",
            observed_type=None,
            known=False,
            raw=line,
            payload=None,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(parsed, dict):
        return StreamEvent(
            type="unknown",
            observed_type=None,
            known=False,
            raw=line,
            payload=None,
            parse_error=f"expected a JSON object, got {type(parsed).__name__}",
        )

    observed = parsed.get("type")
    observed_type = observed if isinstance(observed, str) else None
    if observed_type in KNOWN_TYPES:
        return StreamEvent(
            type=observed_type,
            observed_type=observed_type,
            known=True,
            raw=line,
            payload=parsed,
            parse_error=None,
        )
    return StreamEvent(
        type="unknown",
        observed_type=observed_type,
        known=False,
        raw=line,
        payload=parsed,
        parse_error=None,
    )


def to_event_payload(ev: StreamEvent) -> tuple[str, dict[str, Any]]:
    """Map a :class:`StreamEvent` onto ``(event_name, event_data)``.

    The caller adds ``stage``; this function only knows about the line. Every
    field is best-effort: the client is required to render ``raw`` whenever a
    convenience field comes back ``None``.
    """
    payload: dict[str, Any] = ev.payload if isinstance(ev.payload, dict) else {}

    if not ev.known:
        return (
            _UNKNOWN_EVENT_NAME,
            {
                "observed_type": ev.observed_type,
                "raw": ev.raw,
                "payload": ev.payload,
                "parse_error": ev.parse_error,
            },
        )

    name = _EVENT_NAME_BY_TYPE.get(ev.type, _UNKNOWN_EVENT_NAME)
    data: dict[str, Any] = {"raw": ev.raw, "payload": payload}

    if ev.type == "message":
        data["role"] = _first_str(payload, ("role",), ("message", "role"))
        data["text"] = _message_text(payload)
        # A streaming fragment, not a whole turn. Bob emits one of these per
        # token, so the flag is what lets MessageCoalescer put the turn back
        # together instead of leaving the reader four thousand events to read.
        data["delta"] = _is_delta(payload)
        data["ts"] = _first_str(payload, ("timestamp",), ("ts",))
    elif ev.type == "tool_use":
        data["tool"] = _first_str(
            payload, ("tool",), ("name",), ("tool_name",), ("tool_use", "name")
        )
        data["tool_use_id"] = _first_str(
            payload, ("tool_use_id",), ("id",), ("tool_use", "id")
        )
        raw_input = _first_present(
            payload, ("input",), ("params",), ("arguments",), ("tool_use", "input")
        )
        data["input_preview"] = preview(raw_input) if raw_input is not None else None
    elif ev.type == "tool_result":
        data["tool_use_id"] = _first_str(
            payload, ("tool_use_id",), ("id",), ("tool_result", "tool_use_id")
        )
        is_error = _first_present(
            payload,
            ("is_error",),
            ("isError",),
            ("error",),
            ("tool_result", "is_error"),
        )
        data["is_error"] = bool(is_error) if is_error is not None else None
        raw_output = _first_present(
            payload,
            ("output",),
            ("content",),
            ("result",),
            ("tool_result", "content"),
        )
        data["output_preview"] = preview(raw_output) if raw_output is not None else None
    elif ev.type == "error":
        data["message"] = _first_str(
            payload, ("message",), ("error",), ("detail",), ("error", "message")
        )
    elif ev.type == "result":
        data["stats"] = extract_stats(payload).model_dump(mode="json")

    return name, data


class MessageCoalescer:
    """Rebuild an assistant turn out of the token-sized deltas it arrives in.

    Bob streams ``{"type":"message","role":"assistant","content":"the","delta":true}``
    once per token. One real run of this pipeline produced 3 858 of them against
    27 tool calls — a 143:1 ratio of noise to substance in ``events.jsonl``, in
    the replay endpoint, over SSE and in every consumer downstream. Nobody can
    audit that, and the front end was left to concatenate the fragments itself
    on every single render.

    So consecutive assistant deltas are merged here, before the event log, into
    one event per readable block. The merge is deliberately **not** "one event
    per turn": a turn can run for minutes, and a reader watching a live run has
    to see the model think. A block is closed when any of these is true:

    * a non-delta or non-assistant line arrives (a tool call, a result, the end
      of the stage) — the turn is genuinely over, so is the block;
    * ``max_chars`` of text have accumulated;
    * ``max_age_s`` has passed since the block opened;
    * ``max_parts`` fragments have been merged.

    The last three are what keep the stream live. The first is what makes the
    boundaries meaningful.

    Nothing is discarded. The merged event carries ``text`` (the concatenation,
    which is what a human reads), and ``raw`` (every original NDJSON line,
    newline-joined, in arrival order) so the "Everything" view still shows
    exactly what Bob sent. ``aggregated`` states how many lines were folded in,
    so a reader can tell a real single message from a reconstructed one.

    Not thread-safe and not re-entrant: it is fed from one stdout pump that
    awaits each callback in turn.
    """

    #: Roughly a paragraph. Small enough that the caret keeps moving, large
    #: enough to cut the event count by two orders of magnitude.
    DEFAULT_MAX_CHARS = 1200
    #: A model that pauses mid-turn must not leave text stuck in the buffer for
    #: longer than a beat.
    DEFAULT_MAX_AGE_S = 0.75
    DEFAULT_MAX_PARTS = 400

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        max_parts: int = DEFAULT_MAX_PARTS,
        clock: Any = None,
    ) -> None:
        self.max_chars = max(1, int(max_chars))
        self.max_age_s = max(0.0, float(max_age_s))
        self.max_parts = max(1, int(max_parts))
        self._clock = clock or time.monotonic
        self._parts: list[str] = []
        self._raw: list[str] = []
        self._opened_at: float = 0.0
        self._chars = 0
        self._first_ts: str | None = None
        self._last_ts: str | None = None
        self._turn = 0
        self._chunk = 0
        self._open = False

    # -- public API --------------------------------------------------------- #

    def feed(self, name: str, data: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Absorb one normalized event; return the events to emit, in order.

        The return value is a list because a foreign event has to be preceded by
        the block it interrupted: emitting the tool call first and the reasoning
        that led to it afterwards would misrepresent the order the model worked
        in, which is the one thing this whole trail exists to record.
        """
        if not self._is_assistant_delta(name, data):
            pending = self.flush()
            pending.append((name, dict(data)))
            return pending

        text = data.get("text")
        if not isinstance(text, str) or text == "":
            # A delta with no text carries nothing a reader can use and nothing
            # the raw view would miss; the raw line is kept in the block.
            text = ""

        if not self._open:
            self._open = True
            self._opened_at = self._clock()
            self._turn += 1
            self._chunk = 0
            self._first_ts = _as_str(data.get("ts"))

        self._parts.append(text)
        self._chars += len(text)
        raw = data.get("raw")
        if isinstance(raw, str) and raw:
            self._raw.append(raw)
        ts = _as_str(data.get("ts"))
        if ts:
            self._last_ts = ts

        if (
            self._chars >= self.max_chars
            or len(self._parts) >= self.max_parts
            or (self._clock() - self._opened_at) >= self.max_age_s
        ):
            return self.flush(final=False)
        return []

    def flush(self, *, final: bool = True) -> list[tuple[str, dict[str, Any]]]:
        """Close the open block, if any, and return it as a single event."""
        if not self._open:
            return []
        last_ts = self._last_ts
        event = (
            "bob.message",
            {
                "role": "assistant",
                "text": "".join(self._parts),
                "delta": True,
                "aggregated": len(self._parts),
                "turn": self._turn,
                "chunk": self._chunk,
                #: False means the model was still speaking when this block was
                #: cut for length or age, so the next block continues the same
                #: thought. True means something else took the floor.
                "final": bool(final),
                "first_ts": self._first_ts,
                "last_ts": last_ts,
                "raw": "\n".join(self._raw),
            },
        )
        self._parts.clear()
        self._raw.clear()
        self._chars = 0
        self._last_ts = None
        if final:
            # Something else took the floor. The next delta opens a new turn and
            # restarts the chunk numbering inside it.
            self._open = False
            self._chunk = 0
            self._first_ts = None
        else:
            # Same turn, next block: keep the clock and the numbering running so
            # a reader can see that chunk 3 continues chunk 2.
            self._chunk += 1
            self._opened_at = self._clock()
            self._first_ts = last_ts
        return [event]

    # -- internals ---------------------------------------------------------- #

    @staticmethod
    def _is_assistant_delta(name: str, data: Mapping[str, Any]) -> bool:
        if name != "bob.message" or not data.get("delta"):
            return False
        role = data.get("role")
        # A delta with no role at all is Bob's own shape drift, not somebody
        # else's turn; treat it as assistant output rather than dropping it out
        # of the block and back into the flood.
        return role is None or role == "assistant"


def _is_delta(payload: Mapping[str, Any]) -> bool:
    """True when a message line is one streaming fragment of a longer turn."""
    for path in (("delta",), ("message", "delta"), ("is_delta",), ("partial",)):
        value = _first_present(payload, path)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes"}
    return False


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def extract_stats(payload: Mapping[str, Any] | None) -> StageStats:
    """Pull token and duration statistics out of a ``result`` payload.

    Every field is optional and an all-``None`` result is perfectly valid: the
    totals row simply omits what Bob did not report. Nested ``stats`` / ``usage``
    containers are searched, and the common alternative spellings
    (``prompt_tokens`` / ``completion_tokens``) are accepted.
    """
    if not isinstance(payload, Mapping):
        return StageStats()

    scopes: list[Mapping[str, Any]] = [payload]
    for key in ("stats", "usage", "result", "totals", "session"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            scopes.append(nested)
            for inner_key in ("stats", "usage"):
                deeper = nested.get(inner_key)
                if isinstance(deeper, Mapping):
                    scopes.append(deeper)

    def pick_int(*names: str) -> int | None:
        for scope in scopes:
            for name in names:
                value = scope.get(name)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return int(value)
                if isinstance(value, str):
                    try:
                        return int(float(value))
                    except ValueError:
                        continue
        return None

    def pick_float(*names: str) -> float | None:
        for scope in scopes:
            for name in names:
                value = scope.get(name)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return float(value)
                if isinstance(value, str):
                    try:
                        return float(value)
                    except ValueError:
                        continue
        return None

    def pick_any(*names: str) -> Any | None:
        for scope in scopes:
            for name in names:
                if name in scope and scope.get(name) is not None:
                    return scope.get(name)
        return None

    return StageStats(
        total_tokens=pick_int("total_tokens", "totalTokens", "tokens"),
        input_tokens=pick_int("input_tokens", "inputTokens", "prompt_tokens"),
        output_tokens=pick_int("output_tokens", "outputTokens", "completion_tokens"),
        duration_ms=pick_int("duration_ms", "durationMs", "elapsed_ms"),
        session_costs=pick_any("session_costs", "sessionCosts", "cost", "coins"),
        budget_spend=pick_float("budget_spend", "budgetSpend"),
        max_budget=pick_float("max_budget", "maxBudget"),
    )


def preview(value: Any, limit: int = 400) -> str:
    """Render any value as a short single-line string for the timeline.

    A whole file being written through ``write_to_file`` must not turn the
    timeline into a wall of text, so the dump is hard-truncated and marked.
    """
    if limit <= 0:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - previews never fail
            text = repr(value)
    text = text.replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _first_present(payload: Mapping[str, Any], *paths: tuple[str, ...]) -> Any | None:
    """Return the first non-``None`` value found at any of ``paths``."""
    for path in paths:
        cursor: Any = payload
        for key in path:
            if not isinstance(cursor, Mapping):
                cursor = None
                break
            cursor = cursor.get(key)
        if cursor is not None:
            return cursor
    return None


def _first_str(payload: Mapping[str, Any], *paths: tuple[str, ...]) -> str | None:
    value = _first_present(payload, *paths)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _message_text(payload: Mapping[str, Any]) -> str | None:
    """Best-effort extraction of the human-readable text of a message.

    Handles a plain ``text`` field, a string ``content`` and the block-list
    ``content: [{"type": "text", "text": ...}]`` shape. Returns ``None`` when
    nothing text-like is present, which obliges the client to show ``raw``.
    """
    direct = _first_present(payload, ("text",), ("message", "text"))
    if isinstance(direct, str):
        return direct

    content = _first_present(payload, ("content",), ("message", "content"))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return None
