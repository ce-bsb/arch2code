"""Async MCP stdio client for ``mcp/arch_vision/server.py`` and the Mode A flow.

Three responsibilities live here, in this order:

1. **The client.** One short-lived stdio session per call, spawning
   ``<ARCH2CODE_PYTHON> mcp/arch_vision/server.py`` with ``cwd=project_root``.
   Short-lived because the server keeps nothing but an IAM token cache, and a hung
   inference must not poison every later call. Every tool returns a JSON *string*
   and reports failure as ``{"error": ...}`` inside that JSON rather than by
   raising, so we check for the key explicitly. Those messages are already
   actionable (missing config, corporate TLS interception, VPN, a 404 model id, a
   timeout) and are surfaced verbatim — rewriting them would destroy the
   specificity that makes them useful.

2. **Bounding-box normalization.** The front end must never learn anything about
   the vision model's output shape. Everything it receives is ``{x, y, w, h}`` in
   0..1 with the origin at the top-left corner. The extraction prompt *describes*
   that range but nothing validates it, so a box outside 0..1 is a real, observed
   possibility: we clamp it into the image and attach a warning to the element,
   rather than drawing a rectangle off the edge of the picture and letting the
   user believe the model saw something there.

   There is a second, larger correction. **Verified against the live model on
   2026-07-21**: ``llama-4-maverick`` answers this prompt with
   ``[x1, y1, x2, y2]`` corner coordinates, not ``[x, y, width, height]``, even
   though ``EXTRACT_PROMPT`` asks for width and height. On a real diagram 9 of 12
   components were impossible to read as ``x/y/w/h`` (``x + w`` past 1.5) and every
   one of them was consistent and pixel-accurate read as corners. Taking the schema
   at its word would put every box in the wrong place and make the overlay — the
   one artefact a reviewer checks against the drawing — silently lie.
   :func:`detect_bbox_convention` therefore votes across the whole extraction and
   applies one convention to all of it, records which one it chose in
   ``_bbox_convention``, and warns when that is not the documented shape. It is a
   measurement, not a guess: the vote only counts elements where one reading is
   geometrically impossible and the other is not.

3. **The Mode A flow.** capture -> extract -> persist, with every event emitted
   onto the run's log. It spends no Bobcoin and does not need the Bob binary at
   all; it is the demo that still works on a machine with a broken Bob install.
"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .config import Settings, subprocess_env
from .errors import AppError, Conflict, NotFound, UpstreamError
from .models import (
    CaptureManifest,
    QualitySummary,
    SourceKind,
    VerifyRecord,
)
from .scripts import ScriptResult, capture_diagram

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .store import RunStore

TOOLS: tuple[str, ...] = (
    "arch_vision_list_intake",
    "arch_vision_describe_diagram",
    "arch_vision_extract_architecture",
    "arch_vision_verify_element",
)

#: Emitter signature shared with the pipeline runner: ``(type, data, stage)``.
Emit = Callable[..., Awaitable[Any]]

_MCP_IMPORT_REMEDY = (
    "Install the MCP SDK into the interpreter named by ARCH2CODE_PYTHON "
    "(default /opt/anaconda3/bin/python): `<python> -m pip install mcp httpx pydantic pillow`. "
    "The system python3 on macOS is 3.9.6 and has none of them."
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class VisionToolError(AppError):
    """An arch_vision tool answered with ``{"error": ...}``.

    The server's message is already written for a human who has to fix something,
    so it becomes ``detail`` untouched. We only add a remedy when the server did
    not effectively supply one.
    """

    def __init__(
        self,
        tool: str,
        message: str,
        *,
        raw: Mapping[str, Any] | None = None,
        remedy: str | None = None,
        status: int = 502,
    ) -> None:
        super().__init__(
            "vision_tool_error",
            f"{tool} could not read the diagram",
            message,
            remedy=remedy
            or (
                "The message above comes from mcp/arch_vision/server.py and names the "
                "exact fix. If it mentions configuration, check mcp/arch_vision/.env; "
                "run `python mcp/arch_vision/preflight.py` to isolate the cause."
            ),
            status=status,
            tool=tool,
        )
        self.tool = tool
        self.raw: dict[str, Any] = dict(raw or {})


# --------------------------------------------------------------------------- #
# Bounding boxes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NormalizedBox:
    """A bbox in 0..1 with the origin at the top-left, ready for the front end."""

    x: float
    y: float
    w: float
    h: float
    clamped: bool = False
    original: tuple[float, float, float, float] | None = None

    def as_list(self) -> list[float]:
        """``[x, y, w, h]`` — the shape ``overlay.js`` denormalizes."""
        return [self.x, self.y, self.w, self.h]

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class BboxWarning:
    """One element whose bbox had to be corrected before it could be drawn."""

    element_id: str
    kind: str  # "component" | "connection" | "boundary"
    message: str
    original: list[float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.element_id,
            "kind": self.kind,
            "message": self.message,
            "original": self.original,
        }


@dataclass(frozen=True)
class BboxIndex:
    """Every drawable box for a run, plus the warnings collected while building it."""

    boxes: dict[str, NormalizedBox] = field(default_factory=dict)
    warnings: list[BboxWarning] = field(default_factory=list)


def _as_float(value: Any) -> float | None:
    """Coerce to a finite float. Strings are accepted; ``NaN``/``inf`` are not."""
    if isinstance(value, bool):  # bool is an int; a bool bbox is nonsense
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _as_four_numbers(value: Any) -> tuple[float, float, float, float] | None:
    """Read ``[x, y, w, h]`` from a list, a tuple or a dict, tolerantly."""
    if isinstance(value, Mapping):
        x = _as_float(value.get("x"))
        y = _as_float(value.get("y"))
        w = _as_float(value.get("w", value.get("width")))
        h = _as_float(value.get("h", value.get("height")))
        if None in (x, y, w, h):
            return None
        return (x, y, w, h)  # type: ignore[return-value]
    if isinstance(value, (list, tuple)) and len(value) == 4:
        nums = [_as_float(v) for v in value]
        if any(n is None for n in nums):
            return None
        return (nums[0], nums[1], nums[2], nums[3])  # type: ignore[return-value]
    return None


def raw_bbox(entry: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """Pull the raw bbox out of an extraction entry, whatever shape it arrived in.

    The prompt in ``mcp/arch_vision/server.py`` asks for
    ``evidence: {"kind": "bbox", "value": [x, y, w, h]}``, so ``evidence.value`` is
    the real, verified location — not ``evidence.bbox``. We still probe the other
    plausible spellings because the model writes this field freehand and a drift in
    its output must degrade to "no box drawn", never to a crash.
    """
    if not isinstance(entry, Mapping):
        return None
    candidates: list[Any] = []
    evidence = entry.get("evidence")
    if isinstance(evidence, Mapping):
        candidates.extend(
            [evidence.get("value"), evidence.get("bbox"), evidence.get("box")]
        )
    elif isinstance(evidence, (list, tuple)):
        candidates.append(evidence)
    candidates.extend([entry.get("bbox"), entry.get("box")])
    for candidate in candidates:
        nums = _as_four_numbers(candidate)
        if nums is not None:
            return nums
    return None


#: How the four numbers in ``evidence.value`` are to be read. ``xywh`` is what the
#: prompt asks for; ``xyxy`` is what llama-4-maverick actually returns.
BboxConvention = str

#: Slack allowed when judging whether a reading is geometrically possible. The
#: model rounds to three decimals and a box may legitimately touch the edge.
_CONVENTION_TOL = 0.02


def _plausible_xywh(x: float, y: float, a: float, b: float) -> bool:
    return (
        a > 0
        and b > 0
        and x >= -_CONVENTION_TOL
        and y >= -_CONVENTION_TOL
        and x + a <= 1 + _CONVENTION_TOL
        and y + b <= 1 + _CONVENTION_TOL
    )


def _plausible_xyxy(x: float, y: float, a: float, b: float) -> bool:
    return (
        a > x
        and b > y
        and all(-_CONVENTION_TOL <= v <= 1 + _CONVENTION_TOL for v in (x, y, a, b))
    )


@dataclass(frozen=True)
class BboxDetection:
    """Which reading of ``evidence.value`` the numbers in this extraction support."""

    convention: BboxConvention
    xywh_votes: int
    xyxy_votes: int
    ambiguous: int
    total: int

    @property
    def matches_schema(self) -> bool:
        return self.convention == "xywh"

    def as_dict(self) -> dict[str, Any]:
        return {
            "convention": self.convention,
            "documented": "xywh",
            "xywh_votes": self.xywh_votes,
            "xyxy_votes": self.xyxy_votes,
            "ambiguous": self.ambiguous,
            "total": self.total,
            "note": (
                "Read as [x, y, width, height], exactly as EXTRACT_PROMPT specifies."
                if self.matches_schema
                else (
                    "Read as [x1, y1, x2, y2] corner coordinates. The prompt asks for "
                    "width and height, but this extraction's numbers are impossible "
                    f"under that reading for {self.xyxy_votes} of {self.total} elements "
                    "and consistent as corners. Converted to width/height before "
                    "anything was drawn."
                )
            ),
        }


def detect_bbox_convention(extraction: Mapping[str, Any]) -> BboxDetection:
    """Decide, once for the whole extraction, how to read ``evidence.value``.

    One decision for every element, never per element: a mixture of conventions
    inside a single diagram would place some boxes correctly and others not, and
    nothing in the UI would reveal which were which.

    Only unambiguous elements vote — those where one reading is geometrically
    impossible (a width that runs off the image, or a second corner to the left of
    the first) and the other is not. ``xywh`` wins ties and wins by default,
    because that is what the prompt asks for and what a corrected model would
    return.
    """
    xywh_votes = xyxy_votes = ambiguous = total = 0
    for key in ("components", "connections", "boundaries"):
        for entry in _entries(extraction, key):
            nums = raw_bbox(entry)
            if nums is None:
                continue
            total += 1
            x, y, a, b = nums
            ok_xywh = _plausible_xywh(x, y, a, b)
            ok_xyxy = _plausible_xyxy(x, y, a, b)
            if ok_xywh and not ok_xyxy:
                xywh_votes += 1
            elif ok_xyxy and not ok_xywh:
                xyxy_votes += 1
            else:
                ambiguous += 1
    convention = "xyxy" if xyxy_votes > xywh_votes else "xywh"
    return BboxDetection(
        convention=convention,
        xywh_votes=xywh_votes,
        xyxy_votes=xyxy_votes,
        ambiguous=ambiguous,
        total=total,
    )


def normalize_bbox(
    raw: Sequence[float] | Mapping[str, Any] | None,
    convention: BboxConvention = "xywh",
) -> tuple[NormalizedBox | None, str | None]:
    """Clamp a raw bbox into 0..1 as ``{x, y, w, h}`` and report any correction.

    Returns ``(box, warning)``. ``box`` is ``None`` only when the value cannot be
    read as four finite numbers or when the rectangle has no area at all — an
    undrawable box is dropped, because a zero-width rectangle on the overlay is
    indistinguishable from a rendering bug.

    ``convention`` comes from :func:`detect_bbox_convention` and is measured, not
    assumed. Out-of-range values still arrive after the conversion, and those we
    clamp rather than drop: the element was seen, its position is merely
    untrustworthy, and the warning says exactly that. What we never do is invent a
    rescaling — guessing that the numbers were pixels or percentages would be
    manufacturing a reading the model never gave.
    """
    nums = _as_four_numbers(raw)
    if nums is None:
        return None, None
    source = nums
    x, y, w, h = nums
    reversed_corners = False
    if convention == "xyxy":
        x1, x2 = (x, w) if x <= w else (w, x)
        y1, y2 = (y, h) if y <= h else (h, y)
        # A right-to-left arrow legitimately arrives as x2 < x1. That is the same
        # rectangle with its corners named in the other order, not a bad reading —
        # dropping it would erase a connection the model saw perfectly well.
        reversed_corners = (x1, y1) != (x, y)
        x, y, w, h = x1, y1, x2 - x1, y2 - y1

    if w <= 0 or h <= 0:
        return None, (
            f"bbox {list(source)} read as {convention} has no area "
            f"(w={w:g}, h={h:g}); nothing was drawn for this element."
        )

    # Clamp the rectangle's EDGES, not the origin and the size independently.
    # Clamping x to 0 while keeping w would slide the whole box to the right and
    # move the one edge that was correct; intersecting with the image keeps every
    # edge the model got right exactly where it put it.
    x1 = min(max(x, 0.0), 1.0)
    y1 = min(max(y, 0.0), 1.0)
    x2 = min(max(x + w, 0.0), 1.0)
    y2 = min(max(y + h, 0.0), 1.0)
    cx, cy, cw, ch = x1, y1, x2 - x1, y2 - y1

    # Compare with a tolerance: x2 - x1 reintroduces float error (0.1 + 0.2 - 0.1
    # is not 0.2), and a warning on a perfectly good box teaches the user to ignore
    # the warnings that matter.
    clamped = any(
        abs(a - b) > 1e-9 for a, b in zip((cx, cy, cw, ch), (x, y, w, h))
    )
    if clamped and (cw <= 0 or ch <= 0):
        return None, (
            f"bbox {list(source)} read as {convention} lies entirely outside the image "
            f"(the 0..1 range); nothing was drawn for this element."
        )

    warning = None
    if reversed_corners and not clamped:
        warning = (
            f"bbox {list(source)} arrived with its corners in reverse order "
            f"(read as {convention}). The rectangle is unchanged — the element simply "
            f"runs right-to-left or bottom-to-top."
        )
    if clamped:
        warning = (
            f"bbox {list(source)} read as {convention} fell outside the documented 0..1 "
            f"range and was clamped to [{cx:g}, {cy:g}, {cw:g}, {ch:g}]. The element was "
            f"read, but its position on the image is unreliable — verify it before "
            f"trusting it."
        )
    return (
        NormalizedBox(
            x=round(cx, 6),
            y=round(cy, 6),
            w=round(cw, 6),
            h=round(ch, 6),
            clamped=clamped,
            original=source,
        ),
        warning,
    )


def _entries(extraction: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = extraction.get(key) if isinstance(extraction, Mapping) else None
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, Mapping)]


def normalize_bbox_index(
    extraction: Mapping[str, Any], convention: BboxConvention | None = None
) -> BboxIndex:
    """Build ``{element_id: NormalizedBox}`` for every element that carries a box."""
    if convention is None:
        convention = detect_bbox_convention(extraction).convention
    boxes: dict[str, NormalizedBox] = {}
    warnings: list[BboxWarning] = []
    for kind, key in (
        ("component", "components"),
        ("connection", "connections"),
        ("boundary", "boundaries"),
    ):
        for entry in _entries(extraction, key):
            element_id = entry.get("id")
            if not isinstance(element_id, str) or not element_id:
                continue
            raw = raw_bbox(entry)
            if raw is None:
                continue
            box, warning = normalize_bbox(raw, convention)
            if box is not None:
                boxes[element_id] = box
            if warning:
                warnings.append(
                    BboxWarning(
                        element_id=element_id,
                        kind=kind,
                        message=warning,
                        original=list(raw),
                    )
                )
    return BboxIndex(boxes=boxes, warnings=warnings)


def normalize_bboxes(extraction: Mapping[str, Any]) -> dict[str, list[float]]:
    """``{element_id: [x, y, w, h]}`` in 0..1, clamped, ready for the canvas.

    Denormalizing against the rendered image is the client's job; deciding what is
    drawable is ours.
    """
    return {k: v.as_list() for k, v in normalize_bbox_index(extraction).boxes.items()}


# --------------------------------------------------------------------------- #
# Extraction shaping
# --------------------------------------------------------------------------- #
def is_error_payload(payload: Mapping[str, Any]) -> bool:
    """The tools report failure as a key, not as an exception."""
    return isinstance(payload, Mapping) and "error" in payload


def _confidence_of(entry: Mapping[str, Any]) -> float | None:
    value = _as_float(entry.get("confidence"))
    if value is None:
        return None
    return min(max(value, 0.0), 1.0)


def _label_of(entry: Mapping[str, Any]) -> str | None:
    """The literal text read off the drawing, never a normalized or translated name."""
    evidence = entry.get("evidence")
    if isinstance(evidence, Mapping):
        label = evidence.get("label_text")
        if isinstance(label, str) and label.strip():
            return label.strip()
    for key in ("label", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def summarize_quality(extraction: Mapping[str, Any]) -> QualitySummary:
    """Pass the server's ``_quality`` through and add two client-facing derivations.

    ``broken_refs`` and ``connections_needing_verification`` come straight from
    ``server.py`` and are not recomputed — the server is the authority on its own
    output. ``orphan_components`` (present in ``components[]`` but referenced by no
    connection) and ``low_confidence_components`` (< 0.85) are ours, so the UI can
    rank findings without doing arithmetic in JavaScript.
    """
    quality = extraction.get("_quality") if isinstance(extraction, Mapping) else None
    quality = quality if isinstance(quality, Mapping) else {}

    def _str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str)]

    components = _entries(extraction, "components")
    connections = _entries(extraction, "connections")

    referenced: set[str] = set()
    for conn in connections:
        for endpoint in ("from", "to"):
            value = conn.get(endpoint)
            if isinstance(value, str):
                referenced.add(value)

    orphans: list[str] = []
    low_confidence: list[str] = []
    for comp in components:
        comp_id = comp.get("id")
        if not isinstance(comp_id, str) or not comp_id:
            continue
        if comp_id not in referenced:
            orphans.append(comp_id)
        confidence = _confidence_of(comp)
        if confidence is not None and confidence < 0.85:
            low_confidence.append(comp_id)

    action = quality.get("action_required")
    return QualitySummary(
        broken_refs=_str_list(quality.get("broken_refs")),
        connections_needing_verification=_str_list(
            quality.get("connections_needing_verification")
        ),
        action_required=action if isinstance(action, str) else None,
        orphan_components=orphans,
        low_confidence_components=low_confidence,
    )


def find_entry(
    extraction: Mapping[str, Any], target_kind: str, target_id: str
) -> dict[str, Any] | None:
    """Locate one component or connection by id, for the extract-vs-verify panel."""
    key = {"component": "components", "connection": "connections"}.get(target_kind)
    if key is None:
        return None
    for entry in _entries(extraction, key):
        if entry.get("id") == target_id:
            return dict(entry)
    return None


def compose_claim(
    extraction: Mapping[str, Any], target_kind: str, target_id: str
) -> str:
    """Build ONE verifiable natural-language claim from an extracted element.

    Uses the literal ``label_text`` read off the drawing, never a translated or
    normalized name: the verifier is looking at the same pixels, and asking it
    about a name the drawing does not contain guarantees an ``uncertain``.

    One claim per call. A compound claim ("is there an arrow from A to B carrying
    JSON over HTTPS?") produces a verdict nobody can act on, because a ``false``
    does not say which half was wrong.
    """
    entry = find_entry(extraction, target_kind, target_id)
    if entry is None:
        raise NotFound(
            "vision_target_not_found",
            "That element is not in the extraction",
            f"No {target_kind} with id '{target_id}' exists in this run's extraction.json.",
            remedy=(
                "Pick an id from GET /api/runs/{run_id}/vision, or send "
                "target_kind='free' with your own claim."
            ),
            target_kind=target_kind,
            target_id=target_id,
        )

    if target_kind == "component":
        label = _label_of(entry) or target_id
        kind = entry.get("kind")
        suffix = f", drawn as a {kind}" if isinstance(kind, str) and kind not in ("", "unknown") else ""
        claim = f'The diagram contains a component labelled "{label}"{suffix}.'
    else:
        components = {
            c.get("id"): c for c in _entries(extraction, "components") if c.get("id")
        }
        src = components.get(entry.get("from"))
        dst = components.get(entry.get("to"))
        src_label = _label_of(src) if isinstance(src, Mapping) else None
        dst_label = _label_of(dst) if isinstance(dst, Mapping) else None
        src_label = src_label or str(entry.get("from") or "an unnamed element")
        dst_label = dst_label or str(entry.get("to") or "an unnamed element")
        claim = (
            f'There is an arrow drawn from "{src_label}" to "{dst_label}" in the diagram.'
        )
        edge_label = _label_of(entry)
        if edge_label:
            claim += f' The arrow carries the written label "{edge_label}".'
        # The `protocol` field is deliberately NOT added here. It is the model's
        # classification, not text it read; asking the verifier whether "jdbc" is
        # written next to the arrow would produce a "false" about a word nobody
        # ever claimed was drawn, and a spurious finding is worse than none.

    claim = " ".join(claim.split())
    if len(claim) > 500:
        claim = claim[:497].rstrip() + "..."
    if len(claim) < 10:  # the tool enforces min_length=10
        claim = f"{claim} Confirm this is visible in the drawing."[:500]
    return claim


def enrich_extraction(extraction: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the extraction with front-end-ready geometry attached.

    The front end never sees the vision model's own shape. Every component,
    connection and boundary gains:

    * ``bbox``          -> ``{x, y, w, h}`` in 0..1, top-left origin, or ``null``
    * ``bbox_array``    -> the same box as ``[x, y, w, h]`` for the canvas overlay
    * ``bbox_clamped``  -> true when the raw box was out of range
    * ``bbox_warning``  -> the human-readable reason, or ``null``
    * ``confidence``    -> coerced to a float in 0..1, or ``null``
    * ``label_text``    -> the literal label read off the drawing, or ``null``

    Plus one document-level key, ``_bbox_convention``, recording how the four
    numbers were read and why. Everything the model produced is left in place
    beside these fields, so nothing is lost and ``extraction.raw.json`` remains the
    untouched audit copy — which is what makes this normalization inspectable
    rather than a black box.
    """
    if not isinstance(extraction, Mapping):
        return {
            "components": [],
            "connections": [],
            "boundaries": [],
            "unknowns": [],
            "overall_confidence": None,
            "legibility_notes": None,
        }

    out: dict[str, Any] = dict(extraction)
    warnings: list[BboxWarning] = []
    boxes: dict[str, list[float]] = {}
    detection = detect_bbox_convention(extraction)
    convention = detection.convention

    for kind, key in (
        ("component", "components"),
        ("connection", "connections"),
        ("boundary", "boundaries"),
    ):
        enriched: list[dict[str, Any]] = []
        for entry in _entries(extraction, key):
            item = dict(entry)
            element_id = item.get("id") if isinstance(item.get("id"), str) else None
            box, warning = normalize_bbox(raw_bbox(entry), convention)
            item["bbox"] = box.as_dict() if box else None
            item["bbox_array"] = box.as_list() if box else None
            item["bbox_clamped"] = bool(box.clamped) if box else False
            item["bbox_warning"] = warning
            item["confidence"] = _confidence_of(entry)
            item["label_text"] = _label_of(entry)
            if box and element_id:
                boxes[element_id] = box.as_list()
            if warning:
                warnings.append(
                    BboxWarning(
                        element_id=element_id or "(unnamed)",
                        kind=kind,
                        message=warning,
                        original=list(raw_bbox(entry) or []) or None,
                    )
                )
            enriched.append(item)
        out[key] = enriched

    unknowns = extraction.get("unknowns")
    out["unknowns"] = [u for u in unknowns if isinstance(u, Mapping)] if isinstance(unknowns, list) else []
    out["overall_confidence"] = _as_float(extraction.get("overall_confidence"))
    notes = extraction.get("legibility_notes")
    out["legibility_notes"] = notes if isinstance(notes, str) else None
    out["_bboxes"] = boxes
    out["_bbox_warnings"] = [w.as_dict() for w in warnings]
    out["_bbox_convention"] = detection.as_dict()
    return out


# --------------------------------------------------------------------------- #
# The MCP client
# --------------------------------------------------------------------------- #
class ArchVisionClient:
    """Talks to ``mcp/arch_vision/server.py`` over stdio, one session per call."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- plumbing ----------------------------------------------------------- #
    def _server_params(self) -> Any:
        try:
            from mcp import StdioServerParameters
        except ImportError as exc:  # pragma: no cover - depends on the interpreter
            raise UpstreamError(
                "mcp_sdk_missing",
                "The MCP SDK is not importable",
                f"`import mcp` failed in this process: {exc}",
                remedy=_MCP_IMPORT_REMEDY,
            ) from exc

        server = self.settings.mcp_server_path
        if not server.exists():
            raise UpstreamError(
                "mcp_server_missing",
                "The arch_vision MCP server was not found",
                f"Expected the server at {server}.",
                remedy=(
                    "Check ARCH2CODE_PROJECT_ROOT: it must point at the repository root "
                    "that contains mcp/arch_vision/server.py."
                ),
                expected_path=str(server),
            )

        return StdioServerParameters(
            command=self.settings.python_bin,
            args=[str(server)],
            # A full environment, not the SDK's minimal default: the server reads
            # mcp/arch_vision/.env itself, but SSL_CERT_FILE / HTTPS_PROXY and the
            # rest of a corporate network's configuration live in the ambient
            # environment and dropping them turns a working machine into a TLS
            # failure that takes an afternoon to diagnose.
            env=subprocess_env(self.settings),
            cwd=str(self.settings.project_root),
        )

    @staticmethod
    @contextmanager
    def _stderr_capture() -> Any:
        """Capture the server's stderr into a real file the child can inherit.

        Without this, a server that dies during startup reaches us as
        "McpError: Connection closed" while the line that explains it — say
        ``ModuleNotFoundError: No module named 'httpx'`` — goes to the terminal
        where nobody using the web UI will ever see it. A real file (not a pipe)
        because anyio hands the descriptor straight to the subprocess.
        """
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as fh:
            yield fh

    @staticmethod
    def _read_stderr(handle: Any, limit: int = 2000) -> str:
        try:
            handle.flush()
            handle.seek(0)
            text = handle.read()
        except (OSError, ValueError):  # pragma: no cover
            return ""
        text = (text or "").strip()
        return text[-limit:]

    async def _invoke(
        self,
        tool: str,
        args: Mapping[str, Any],
        timeout_s: float,
        errlog: Any = None,
    ) -> str:
        """Spawn the server, call one tool, return the concatenated text content."""
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - depends on the interpreter
            raise UpstreamError(
                "mcp_sdk_missing",
                "The MCP SDK is not importable",
                f"`from mcp import ClientSession` failed in this process: {exc}",
                remedy=_MCP_IMPORT_REMEDY,
            ) from exc

        params = self._server_params()
        read_timeout = timedelta(seconds=timeout_s)
        client = (
            stdio_client(params, errlog=errlog)
            if errlog is not None
            else stdio_client(params)
        )
        async with client as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=read_timeout
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool, dict(args), read_timeout_seconds=read_timeout
                )

        chunks: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
        text = "".join(chunks)

        if getattr(result, "isError", False):
            raise UpstreamError(
                "mcp_tool_failed",
                f"{tool} failed inside the MCP server",
                text.strip()[:2000] or "The server reported an error with no message.",
                remedy=(
                    "Run the server by hand to see the traceback: "
                    f"`{self.settings.python_bin} {self.settings.mcp_server_path}`."
                ),
                tool=tool,
            )
        return text

    async def call(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Call one tool and return its parsed JSON object.

        Raises :class:`VisionToolError` when the payload carries an ``error`` key,
        with the server's own message intact.
        """
        if tool not in TOOLS:
            raise UpstreamError(
                "mcp_unknown_tool",
                "Unknown arch_vision tool",
                f"'{tool}' is not one of {list(TOOLS)}.",
                remedy="This is a bug in the webapp; the tool name is hard-coded.",
                tool=tool,
            )

        budget = float(timeout_s or self.settings.vision_timeout_s)
        with self._stderr_capture() as errlog:
            try:
                # The outer budget covers the spawn and the handshake too. The inner
                # read timeout only covers a request that was already sent, and a
                # server that dies during startup would otherwise hang us forever.
                text = await asyncio.wait_for(
                    self._invoke(tool, args, budget, errlog), timeout=budget + 15.0
                )
            except AppError:
                raise
            except asyncio.TimeoutError as exc:
                raise UpstreamError(
                    "mcp_timeout",
                    "The arch_vision MCP server did not answer in time",
                    _with_stderr(
                        f"{tool} exceeded the {budget:g}s budget "
                        f"(ARCH2CODE_VISION_TIMEOUT_S).",
                        self._read_stderr(errlog),
                    ),
                    remedy=(
                        "Raise ARCH2CODE_VISION_TIMEOUT_S, or shrink the image: a diagram "
                        "normalized by capture_diagram.py to 1568px answers far faster than "
                        "a 4000px phone photo."
                    ),
                    tool=tool,
                ) from exc
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - anyio raises groups here
                raise self._spawn_error(tool, exc, self._read_stderr(errlog)) from exc

        return self._parse_payload(tool, text)

    def _spawn_error(
        self, tool: str, exc: BaseException, server_stderr: str = ""
    ) -> AppError:
        """Turn a transport-level explosion into something a human can act on."""
        flat = _flatten_exception(exc)
        detail = "; ".join(f"{type(e).__name__}: {e}" for e in flat)[:2000]
        return UpstreamError(
            "mcp_transport_failed",
            "Could not talk to the arch_vision MCP server",
            _with_stderr(detail or f"{type(exc).__name__}: {exc}", server_stderr),
            remedy=(
                f"Start it by hand to see the real error: "
                f"`{self.settings.python_bin} {self.settings.mcp_server_path}`. "
                f"If it exits immediately, the interpreter in ARCH2CODE_PYTHON is missing "
                f"mcp/httpx/pydantic — the system python3 is 3.9.6 and has none of them."
            ),
            tool=tool,
        )

    def _parse_payload(self, tool: str, text: str) -> dict[str, Any]:
        stripped = (text or "").strip()
        if not stripped:
            raise UpstreamError(
                "mcp_empty_response",
                f"{tool} returned nothing",
                "The MCP call succeeded but carried no text content.",
                remedy=(
                    "Check the server's stderr; this usually means the tool crashed in a "
                    "way that bypassed its own error handler."
                ),
                tool=tool,
            )
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                "mcp_bad_json",
                f"{tool} did not return JSON",
                f"{exc}. First 300 characters: {stripped[:300]}",
                remedy=(
                    "Every arch_vision tool returns a JSON string; non-JSON means the "
                    "server printed something onto stdout. Check for stray print() calls "
                    "in mcp/arch_vision/server.py — stdout is the MCP transport."
                ),
                tool=tool,
            ) from exc

        if not isinstance(payload, dict):
            return {"result": payload}

        if is_error_payload(payload):
            message = payload.get("error")
            raise VisionToolError(
                tool,
                str(message) if message is not None else "The tool reported an error.",
                raw=payload,
            )
        return payload

    # -- tools -------------------------------------------------------------- #
    async def list_tools(self) -> list[str]:
        """stdio handshake plus ``list_tools``. Used by the health probe."""
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover
            raise UpstreamError(
                "mcp_sdk_missing",
                "The MCP SDK is not importable",
                str(exc),
                remedy=_MCP_IMPORT_REMEDY,
            ) from exc

        params = self._server_params()
        budget = min(float(self.settings.vision_timeout_s), 45.0)

        async def _handshake(errlog: Any) -> list[str]:
            async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    return [t.name for t in getattr(listing, "tools", []) or []]

        with self._stderr_capture() as errlog:
            try:
                return await asyncio.wait_for(_handshake(errlog), timeout=budget)
            except AppError:
                raise
            except asyncio.TimeoutError as exc:
                raise UpstreamError(
                    "mcp_handshake_timeout",
                    "The arch_vision MCP server did not complete its handshake",
                    _with_stderr(
                        f"No response within {budget:g}s of spawning "
                        f"`{self.settings.python_bin} {self.settings.mcp_server_path}`.",
                        self._read_stderr(errlog),
                    ),
                    remedy=(
                        "Run that command in a terminal. A handshake timeout is almost "
                        "always an interpreter that cannot import mcp — set "
                        "ARCH2CODE_PYTHON to one that can."
                    ),
                ) from exc
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                raise self._spawn_error(
                    "list_tools", exc, self._read_stderr(errlog)
                ) from exc

    async def list_intake(self, directory: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if directory:
            args["directory"] = directory
        return await self.call("arch_vision_list_intake", args)

    async def describe_diagram(
        self, image_path: str, focus: str | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"image_path": _absolute(image_path, self.settings)}
        if focus:
            args["focus"] = focus
        return await self.call("arch_vision_describe_diagram", args)

    async def extract_architecture(
        self,
        image_path: str,
        source_kind: SourceKind = "screenshot",
        hint: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "image_path": _absolute(image_path, self.settings),
            "source_kind": source_kind,
        }
        if hint:
            args["hint"] = hint
        return await self.call("arch_vision_extract_architecture", args)

    async def verify_element(self, image_path: str, claim: str) -> dict[str, Any]:
        return await self.call(
            "arch_vision_verify_element",
            {"image_path": _absolute(image_path, self.settings), "claim": claim},
        )


def _absolute(image_path: str | Path, settings: Settings) -> str:
    """Always hand the server an absolute path.

    ``server.py`` resolves a relative path against its OWN ``PROJECT_ROOT``, not
    against the caller's cwd. For a Mode A run the image lives under
    ``webapp/runs/<run_id>/workspace/``, which that fallback would never find.
    """
    path = Path(image_path)
    if not path.is_absolute():
        path = (settings.project_root / path).resolve()
    return str(path)


def _with_stderr(detail: str, server_stderr: str) -> str:
    """Attach what the server printed. That line is usually the whole answer."""
    if not server_stderr:
        return detail
    return f"{detail}\n\nThe MCP server printed:\n{server_stderr}"


def _flatten_exception(exc: BaseException) -> list[BaseException]:
    """Flatten anyio's exception groups into the leaves that actually explain it.

    ``stdio_client`` runs inside an anyio task group, so a dead subprocess reaches
    us as a group wrapping the real cause. Reporting the group's ``repr`` tells the
    user nothing; the leaves name the actual failure.
    """
    sub_exceptions = getattr(exc, "exceptions", None)
    if isinstance(sub_exceptions, (list, tuple)) and sub_exceptions:
        leaves: list[BaseException] = []
        for sub in sub_exceptions:
            if isinstance(sub, BaseException):
                leaves.extend(_flatten_exception(sub))
        return leaves or [exc]
    return [exc]


# --------------------------------------------------------------------------- #
# Mode A: capture -> extract -> persist
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VisionStageResult:
    """What one Mode A stage produced, for the runner to fold into StageState."""

    stage: str
    ok: bool
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)
    script: ScriptResult | None = None


def vision_dir(store: "RunStore", run_id: str) -> Path:
    path = store.vision_dir(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)


def _read_json(path: Path) -> Any | None:
    """Read a JSON file, returning ``None`` for anything unreadable.

    A half-written artifact is a legitimate state on this path (the process may
    have been cancelled), and it must present as "not there yet" rather than as a
    500 on a page whose whole job is to show what went wrong.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def capture_paths(store: "RunStore", run_id: str) -> dict[str, Path]:
    """Every file Mode A writes for a run, in one place.

    ``capture_manifest`` is an accepted alias for ``capture``: the pipeline runner
    writes the manifest under the name the harness uses and this module writes it
    with absolute paths resolved. Both are read.
    """
    vdir = store.vision_dir(run_id)
    return {
        "capture": vdir / "capture.json",
        "capture_manifest": vdir / "capture-manifest.json",
        "extraction": vdir / "extraction.json",
        "extraction_raw": vdir / "extraction.raw.json",
        "quality": vdir / "quality.json",
        "verifications": vdir / "verifications.jsonl",
    }


def load_capture(store: "RunStore", run_id: str) -> CaptureManifest | None:
    paths = capture_paths(store, run_id)
    for key in ("capture", "capture_manifest"):
        data = _read_json(paths[key])
        if not isinstance(data, Mapping):
            continue
        try:
            return CaptureManifest.model_validate(dict(data))
        except Exception:  # noqa: BLE001 - a stale manifest must not break the page
            continue
    return None


def load_extraction(store: "RunStore", run_id: str) -> dict[str, Any] | None:
    """Read the extraction and guarantee the front end gets normalized geometry.

    ``extraction.json`` may have been written by this module (already enriched) or
    straight from the MCP payload by the pipeline runner. The absence of
    ``_bbox_convention`` identifies the second case, and we enrich on read rather
    than trusting whoever wrote the file: raw ``evidence.value`` numbers reaching
    the canvas is precisely the bug this normalization exists to prevent, and it
    would show up as an overlay that looks plausible and is wrong.

    Enriching is idempotent and costs microseconds on a payload this size, so
    doing it twice is harmless; skipping it once is not.
    """
    paths = capture_paths(store, run_id)
    for key in ("extraction", "extraction_raw"):
        data = _read_json(paths[key])
        if not isinstance(data, Mapping):
            continue
        payload = dict(data)
        if "_bbox_convention" not in payload:
            payload = enrich_extraction(payload)
        return payload
    return None


def normalized_image_path(store: "RunStore", run_id: str) -> Path | None:
    """The PNG the model actually saw — the only image the bboxes are valid for."""
    manifest = load_capture(store, run_id)
    if manifest is None:
        return None
    raw = manifest.normalized_artifact or manifest.working_copy
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (store.workspace_dir(run_id) / path).resolve()
    return path if path.exists() else None


def image_dimensions(manifest: CaptureManifest | None) -> tuple[int | None, int | None]:
    """Width and height of the normalized PNG, straight from the capture manifest."""
    if manifest is None or not isinstance(manifest.normalization, Mapping):
        return None, None
    normalized = manifest.normalization.get("normalized")
    if not isinstance(normalized, Mapping):
        return None, None
    width = normalized.get("width")
    height = normalized.get("height")
    return (
        width if isinstance(width, int) else None,
        height if isinstance(height, int) else None,
    )


async def _emit(emit: Emit | None, type_: str, data: Mapping[str, Any], stage: str | None) -> None:
    if emit is None:
        return
    with suppress(Exception):
        await emit(type_, dict(data), stage=stage)


async def run_capture_stage(
    settings: Settings,
    store: "RunStore",
    run_id: str,
    *,
    source: Path,
    cwd: Path,
    emit: Emit | None = None,
) -> tuple[CaptureManifest, VisionStageResult]:
    """Stage 1 of Mode A: normalize the image with ``capture_diagram.py``.

    ``cwd`` decides where the artifacts land, and that is deliberate:
    ``capture_diagram.py`` writes ``.arch/intake/<run_id>/`` relative to its own
    working directory. Mode A passes ``webapp/runs/<run_id>/workspace/`` so a vision
    preview leaves the repository completely untouched; Mode B passes the repo root,
    where those artifacts genuinely belong to the audit trail.
    """
    started = _now_ms()
    await _emit(
        emit,
        "vision.capture.started",
        {"source_path": str(source), "run_id": run_id, "cwd": str(cwd)},
        "capture",
    )

    manifest, script = await capture_diagram(
        settings, source=source, run_id=run_id, cwd=cwd
    )
    duration_ms = _now_ms() - started

    await _emit(
        emit,
        "script.finished",
        {
            "script": "capture_diagram.py",
            "argv": list(script.argv),
            "exit_code": script.exit_code,
            "stdout_tail": script.stdout[-4000:],
            "stderr_tail": script.stderr[-4000:],
            "duration_ms": script.duration_ms,
        },
        "capture",
    )

    normalization = manifest.normalization if isinstance(manifest.normalization, Mapping) else {}
    normalized = normalization.get("normalized") if isinstance(normalization, Mapping) else {}
    normalized = normalized if isinstance(normalized, Mapping) else {}

    normalized_path = manifest.normalized_artifact or manifest.working_copy
    absolute_normalized = None
    if normalized_path:
        candidate = Path(normalized_path)
        absolute_normalized = str(
            candidate if candidate.is_absolute() else (cwd / candidate).resolve()
        )

    # Persist a copy with the path already absolute: every consumer downstream
    # (the image endpoint, verify, the overlay) needs it resolved, and none of them
    # knows which cwd this ran in.
    stored = manifest.model_dump(mode="json")
    stored["normalized_artifact"] = absolute_normalized or stored.get("normalized_artifact")
    stored["_cwd"] = str(cwd)
    _write_json(capture_paths(store, run_id)["capture"], stored)

    await _emit(
        emit,
        "vision.capture.finished",
        {
            "manifest": stored,
            "normalized_path": absolute_normalized,
            "width": normalized.get("width"),
            "height": normalized.get("height"),
            "exif_rotation_applied": bool(normalization.get("exif_rotation_applied")),
            "scale": normalization.get("scale", 1.0),
            "warnings": list(manifest.warnings or []),
            "exit_code": script.exit_code,
        },
        "capture",
    )

    resolved = CaptureManifest.model_validate(stored)
    return resolved, VisionStageResult(
        stage="capture",
        ok=True,
        duration_ms=duration_ms,
        detail={
            "normalized_path": absolute_normalized,
            "warnings": list(manifest.warnings or []),
        },
        script=script,
    )


async def run_extract_stage(
    settings: Settings,
    store: "RunStore",
    client: ArchVisionClient,
    run_id: str,
    *,
    image_path: Path,
    source_kind: SourceKind,
    hint: str | None,
    emit: Emit | None = None,
) -> tuple[dict[str, Any], VisionStageResult]:
    """Stage 2 of Mode A: the structured extraction, persisted three ways.

    ``extraction.raw.json`` is the model's answer untouched, for the audit trail.
    ``extraction.json`` is the enriched copy the UI reads, with every bbox already
    clamped into 0..1. ``quality.json`` is the ranked findings list.
    """
    started = _now_ms()
    await _emit(
        emit,
        "vision.extract.started",
        {
            "tool": "arch_vision_extract_architecture",
            "image_path": str(image_path),
            "source_kind": source_kind,
            "hint": hint,
        },
        "extract",
    )

    try:
        raw = await client.extract_architecture(
            str(image_path), source_kind=source_kind, hint=hint
        )
    except VisionToolError as exc:
        await _emit(
            emit,
            "vision.tool_error",
            {
                "tool": "arch_vision_extract_architecture",
                "message": exc.detail,
                "remedy": exc.remedy,
                "raw": exc.raw,
            },
            "extract",
        )
        raise
    except AppError as exc:
        await _emit(
            emit,
            "vision.tool_error",
            {
                "tool": "arch_vision_extract_architecture",
                "message": exc.detail,
                "remedy": exc.remedy,
                "raw": {"code": exc.code},
            },
            "extract",
        )
        raise

    paths = capture_paths(store, run_id)
    _write_json(paths["extraction_raw"], raw)

    enriched = enrich_extraction(raw)
    _write_json(paths["extraction"], enriched)

    quality = summarize_quality(raw)
    _write_json(paths["quality"], quality.model_dump(mode="json"))

    provenance = raw.get("_provenance") if isinstance(raw.get("_provenance"), Mapping) else {}
    duration_ms = _now_ms() - started

    await _emit(
        emit,
        "vision.extract.finished",
        {
            "components": len(enriched.get("components") or []),
            "connections": len(enriched.get("connections") or []),
            "boundaries": len(enriched.get("boundaries") or []),
            "unknowns": len(enriched.get("unknowns") or []),
            "overall_confidence": enriched.get("overall_confidence"),
            "quality": {
                "broken_refs": quality.broken_refs,
                "connections_needing_verification": quality.connections_needing_verification,
                "action_required": quality.action_required,
            },
            "model": provenance.get("model"),
            "prompt_version": provenance.get("prompt_version"),
            "duration_ms": duration_ms,
            "bbox_warnings": enriched.get("_bbox_warnings") or [],
            "bbox_convention": enriched.get("_bbox_convention") or {},
        },
        "extract",
    )

    return enriched, VisionStageResult(
        stage="extract",
        ok=True,
        duration_ms=duration_ms,
        detail={
            "components": len(enriched.get("components") or []),
            "connections": len(enriched.get("connections") or []),
            "bbox_warnings": len(enriched.get("_bbox_warnings") or []),
        },
    )


async def run_vision_preview(
    settings: Settings,
    store: "RunStore",
    client: ArchVisionClient,
    run_id: str,
    *,
    source: Path,
    source_kind: SourceKind = "screenshot",
    hint: str | None = None,
    emit: Emit | None = None,
) -> list[VisionStageResult]:
    """The whole of Mode A: capture, then extract. Spends no Bobcoin.

    Kept here rather than inside ``PipelineRunner`` so the flow is testable without
    a Bob binary and so ``_run_vision_stage`` stays a thin adapter that maps these
    results onto ``StageState``. It emits every event itself and never mutates
    ``run.json`` — run state belongs to the runner.
    """
    workspace = store.workspace_dir(run_id)
    workspace.mkdir(parents=True, exist_ok=True)

    manifest, capture_result = await run_capture_stage(
        settings, store, run_id, source=source, cwd=workspace, emit=emit
    )

    image = normalized_image_path(store, run_id)
    if image is None:
        raise UpstreamError(
            "capture_no_image",
            "capture_diagram.py produced no normalized image",
            (
                f"The capture manifest for {run_id} routes this artifact as "
                f"'{manifest.extraction_path}', so no normalized PNG was written and the "
                f"vision model has nothing to read."
            ),
            remedy=(
                "Upload a raster image (.png/.jpg/.jpeg/.webp/.heic/.tif). A .drawio or "
                ".puml belongs on the deterministic path — parse_drawio.py reads it "
                "exactly, for free, with no risk of hallucination."
            ),
            extraction_path=manifest.extraction_path,
        )

    _, extract_result = await run_extract_stage(
        settings,
        store,
        client,
        run_id,
        image_path=image,
        source_kind=source_kind,
        hint=hint,
        emit=emit,
    )
    return [capture_result, extract_result]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def read_verifications(
    store: "RunStore", run_id: str, target_id: str | None = None
) -> list[VerifyRecord]:
    """Replay ``vision/verifications.jsonl``, skipping anything unreadable."""
    path = capture_paths(store, run_id)["verifications"]
    if not path.exists():
        return []
    records: list[VerifyRecord] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            record = VerifyRecord.model_validate(payload)
        except Exception:  # noqa: BLE001 - a truncated tail is not an error
            continue
        if target_id and record.target_id != target_id:
            continue
        records.append(record)
    return records


def append_verification(store: "RunStore", run_id: str, record: VerifyRecord) -> None:
    path = capture_paths(store, run_id)["verifications"]
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


async def verify_target(
    settings: Settings,
    store: "RunStore",
    client: ArchVisionClient,
    run_id: str,
    *,
    target_kind: str,
    target_id: str | None,
    claim: str | None,
    emit: Emit | None = None,
) -> VerifyRecord:
    """Run the second pass over the same image and record the answer.

    A tool-level failure becomes ``verdict="error"`` carrying the server's own
    message, not an HTTP 500: the tool never throws, and neither does this. What
    the UI must never do is soften ``false`` or ``uncertain`` into a pass — the
    server itself attaches ``_action`` telling you not to approve the AIR.
    """
    image = normalized_image_path(store, run_id)
    if image is None:
        raise Conflict(
            "vision_not_captured",
            "This run has no normalized image yet",
            f"Run {run_id} has not completed its capture stage, so there is nothing to verify against.",
            remedy="Start the run (POST /api/runs/{run_id}/start) and wait for the capture stage.",
            run_id=run_id,
        )

    extraction = load_extraction(store, run_id) or {}
    if target_kind != "free":
        if not target_id:
            raise AppError(
                "vision_target_required",
                "target_id is required",
                f"target_kind='{target_kind}' needs the id of the element to verify.",
                remedy="Send target_kind='free' with your own claim to verify something arbitrary.",
                status=422,
            )
        if not claim:
            claim = compose_claim(extraction, target_kind, target_id)

    if not claim or len(claim.strip()) < 10:
        raise AppError(
            "vision_claim_too_short",
            "The claim is too short to verify",
            "arch_vision_verify_element requires a claim of 10 to 500 characters.",
            remedy="Write one complete, checkable sentence about a single element of the drawing.",
            status=422,
        )
    claim = claim.strip()[:500]

    extract_side = (
        find_entry(extraction, target_kind, target_id)
        if target_kind != "free" and target_id
        else None
    )

    started = _now_ms()
    verdict = "error"
    payload: dict[str, Any] = {}
    confidence: float | None = None
    observed: str | None = None
    contradiction: str | None = None
    action: str | None = None
    model: str | None = None
    prompt_version: str | None = None

    try:
        payload = await client.verify_element(str(image), claim)
    except VisionToolError as exc:
        payload = {"error": exc.detail, **exc.raw}
        observed = None
        action = exc.remedy
        await _emit(
            emit,
            "vision.tool_error",
            {
                "tool": "arch_vision_verify_element",
                "message": exc.detail,
                "remedy": exc.remedy,
                "raw": exc.raw,
            },
            "extract",
        )
    except AppError as exc:
        payload = {"error": exc.detail, "code": exc.code}
        action = exc.remedy
        await _emit(
            emit,
            "vision.tool_error",
            {
                "tool": "arch_vision_verify_element",
                "message": exc.detail,
                "remedy": exc.remedy,
                "raw": {"code": exc.code},
            },
            "extract",
        )
    else:
        raw_verdict = payload.get("verdict")
        verdict = (
            raw_verdict.strip().lower()
            if isinstance(raw_verdict, str)
            and raw_verdict.strip().lower() in ("true", "false", "uncertain")
            else "uncertain"
        )
        confidence = _as_float(payload.get("confidence"))
        if confidence is not None:
            confidence = min(max(confidence, 0.0), 1.0)
        observed = payload.get("observed") if isinstance(payload.get("observed"), str) else None
        contradiction = (
            payload.get("contradiction")
            if isinstance(payload.get("contradiction"), str)
            else None
        )
        action = payload.get("_action") if isinstance(payload.get("_action"), str) else None
        model = payload.get("_model") if isinstance(payload.get("_model"), str) else None
        prompt_version = (
            payload.get("_prompt_version")
            if isinstance(payload.get("_prompt_version"), str)
            else None
        )

    duration_ms = _now_ms() - started
    record = VerifyRecord(
        verification_id=uuid.uuid4().hex[:16],
        target_kind=target_kind,
        target_id=target_id,
        claim=claim,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        observed=observed,
        contradiction=contradiction,
        action=action,
        model=model,
        prompt_version=prompt_version,
        duration_ms=duration_ms,
        created_at=datetime.now(timezone.utc),
        extract_side=extract_side,
        raw=payload if isinstance(payload, dict) else {"result": payload},
    )
    append_verification(store, run_id, record)

    await _emit(
        emit,
        "vision.verify.finished",
        {
            "verification_id": record.verification_id,
            "target_kind": record.target_kind,
            "target_id": record.target_id,
            "claim": record.claim,
            "verdict": record.verdict,
            "confidence": record.confidence,
            "observed": record.observed,
            "contradiction": record.contradiction,
            "duration_ms": record.duration_ms,
        },
        "extract",
    )
    return record


def _now_ms() -> int:
    """Wall-clock milliseconds. Durations here are reported to a human, not used
    for scheduling, so the clock the human's watch agrees with is the right one."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)
