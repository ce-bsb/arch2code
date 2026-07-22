"""Deterministic AIR construction from an ``extraction.json``, for use as a
FALLBACK only — never as the normal path.

Stage 2 (``arch-analyst``) turns the extraction into an AIR. When it cannot run
to completion — the observed failures are a transient HTTP 504 from IBM's
authorization service, and the stage being killed by the stall watchdog — the
run used to end there, with an ``extraction.json`` on disk and nothing
downstream to look at. This module produces the part of the AIR that is a pure
transform of what is already in hand, so the run can continue to the critic and
be judged.

A NOTE ON THE STALL, because an earlier version of this docstring blamed an
exhausted account budget and that was wrong: the run that produced the
canonical stall had spent 1.19 of 100 coins on a fresh account. What actually
happens is measured in :data:`app.bobproc.STALL_BEFORE_FIRST_LINE_S` — Bob
emits nothing while the model generates a tool call's arguments, and for the
analyst that argument is the whole AIR. Do not send anyone to check their
balance over it.

WHAT IS MECHANICALLY DERIVABLE, and is therefore built here:

* ``components[]``  — id, name, kind, confidence, evidence (bbox/cell/text)
* ``connections[]`` — id, from, to, protocol, sync, confidence, evidence
* ``boundaries[]``  — when the extraction recorded any
* ``unknowns[]``    — the ones the extraction already recorded, plus one per
  low-confidence connection, plus the one below
* ``meta{}``        — run id, source, timestamps, derived overall confidence

WHAT AN ANALYST PRODUCES AND A TRANSFORM CANNOT, and is therefore NOT invented:

* ``assumptions[]`` with a declared ``impact`` ("what breaks in the generated
  code if this is wrong"). Reading an assumption off a drawing is a
  contradiction in terms: an assumption is precisely what is *not* drawn.
* a real ``experiment_plan`` with falsifiable hypotheses about the architecture.

THE RULE THIS MODULE EXISTS TO ENFORCE: **the fallback AIR must never pass for a
reasoned AIR.** Concretely:

1. ``meta.extractor`` names this file and says the analyst did not run.
2. ``assumptions`` is ``[]``. Empty is the truthful value; a plausible-sounding
   assumption with a made-up impact is the single most dangerous thing this
   module could emit, because stage 4 generates code from it.
3. A blocking unknown (:data:`FALLBACK_UNKNOWN_ID`) states that
   contextualization did not complete and that no assumption was declared or
   impact-assessed.
4. ``experiment_plan`` contains one hypothesis, and it is a hypothesis about
   *this artifact's provenance*, not about the architecture — the only claim
   that can honestly be made without reasoning about the system.

The intended consequence is that ``validate_air.py --gate`` REJECTS this AIR:
an open blocking unknown is a stage-3 gate error. That is correct. The user sees
the whole flow, the artifacts and the gate doing its job, and nothing in the
system claims work that was not done.

Two conventions worth stating, because both are places where a transform is
tempted to become an author:

* **Unrecognised vocabulary becomes ``"unknown"``, never a guess.** A component
  typed ``"client"`` by the extractor is not silently promoted to ``"ui"``, and
  a connection labelled ``"REST"`` is not promoted to ``"http"``. The literal is
  preserved in ``note`` so nothing is lost and the human can decide.
* **A missing confidence becomes ``0.0``.** The schema requires a number and
  there is no null; ``0.0`` is the only value that cannot be mistaken for a
  measurement, and it pulls ``overall_confidence`` in the safe direction.

This module does no I/O, spawns nothing and calls no model. It is a pure
function of a dict, which is what makes it testable against the schema.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

__all__ = [
    "AIR_VERSION",
    "FALLBACK_EXTRACTOR",
    "FALLBACK_UNKNOWN_ID",
    "LOW_CONFIDENCE_BELOW",
    "build_fallback_air",
]

#: The only value ``air.schema.json`` accepts for ``air_version``.
AIR_VERSION = "1.0"

#: Written verbatim into ``meta.extractor``. Anyone reading the AIR — human,
#: critic, or the export bundle — learns from this single field that stage 2 did
#: not run. Keep the words "did not run" in it.
FALLBACK_EXTRACTOR = (
    "air_fallback.py@1.0 — mechanically derived from extraction.json; "
    "the arch-analyst stage did not run"
)

#: Id of the blocking unknown that makes the stage-3 gate reject this AIR.
FALLBACK_UNKNOWN_ID = "contextualization_incomplete"

#: Connections below this confidence get their own unknown. Mirrors
#: VERIFY_SECOND_PASS_BELOW in .bob/skills/air-normalizer/scripts/validate_air.py,
#: which is where the critic's own threshold lives.
LOW_CONFIDENCE_BELOW = 0.85

# --------------------------------------------------------------------------- #
# schema vocabularies — copied from air.schema.json, deliberately
#
# These are duplicated rather than parsed out of the schema at import time on
# purpose: this module must remain a pure in-memory transform with no file
# access. The tests validate every produced AIR against the schema on disk, so a
# drift between these sets and the schema fails the suite rather than a run.
# --------------------------------------------------------------------------- #

_COMPONENT_KINDS = frozenset({
    "service", "ui", "gateway", "database", "cache", "queue", "topic", "job",
    "function", "storage", "external", "actor", "agent", "tool",
    "knowledge_base", "unknown",
})

_PROTOCOLS = frozenset({
    "http", "https", "grpc", "graphql", "amqp", "kafka", "mqtt", "jdbc", "sql",
    "s3", "file", "websocket", "internal", "external_chat", "unknown",
})

_SYNC = frozenset({"sync", "async", "unknown"})

_BOUNDARY_KINDS = frozenset({
    "vpc", "namespace", "cluster", "zone", "onprem", "cloud", "dmz", "account",
    "logical", "external_system",
})

_SOURCE_KINDS = frozenset({
    "napkin", "whiteboard", "screenshot", "drawio", "plantuml", "mermaid",
    "pdf", "prose",
})

_EXTRACTION_PATHS = frozenset({"deterministic", "vision"})

_EVIDENCE_KINDS = frozenset({"bbox", "cell", "line", "text", "human"})

#: ``meta.source_kind`` is required and has no "unknown" member. When neither
#: the caller nor the extraction says what the artifact was, this is what goes
#: in — together with a non-blocking unknown recording that it was not observed.
_SOURCE_KIND_LAST_RESORT = "screenshot"

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

#: ``unknowns[].question`` has minLength 10. A shorter note from the extraction
#: is wrapped in this rather than dropped or padded with filler.
_SHORT_QUESTION_WRAPPER = "Clarify this point recorded by the extraction: {text}"


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str | None:
    """A non-empty stripped string, or None. Never raises on odd input."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _confidence(value: Any) -> float:
    """Clamp to [0, 1]. Anything unreadable becomes 0.0 — see module docstring."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return round(min(1.0, max(0.0, float(value))), 4)


def _as_bbox(value: Any) -> list[float] | None:
    """A 4-number sequence, or None. Pixel and normalized boxes both pass.

    The schema documents bbox as normalized 0..1 but constrains only the length
    and the item type. Extractions in this repository contain both conventions,
    and re-scaling one into the other without the image dimensions would be a
    fabrication, so the numbers are passed through untouched.
    """
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        if len(items) == 4 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in items
        ):
            return [float(v) for v in items]
    return None


def _make_id(raw: Any, *, prefix: str, taken: set[str]) -> str:
    """Force any extractor id into ``^[a-z][a-z0-9_]*$``, uniquely.

    Extractions in this repository use ``comp-1``, ``C1`` and ``ai_chat`` for the
    same concept, and only the third is a legal AIR id. The mapping is
    deterministic so that re-running the transform on the same extraction yields
    byte-identical output, and ``taken`` is shared across components, inline
    tools, connections and boundaries because ``validate_air.py`` treats an id
    reused between a component and a tool as an error (one file, two things).
    """
    ascii_only = (
        unicodedata.normalize("NFKD", str(raw if raw is not None else ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_only).strip("_")
    if not slug:
        slug = prefix
    if not slug[0].isalpha():
        slug = f"{prefix}_{slug}"
    slug = slug[:64]

    candidate = slug
    counter = 2
    while candidate in taken:
        candidate = f"{slug}_{counter}"
        counter += 1
    taken.add(candidate)
    return candidate


def _evidence(raw: Any, *, subject: str) -> dict[str, Any]:
    """Normalize whatever the extractor called evidence into the AIR shape.

    ``evidence`` is REQUIRED on every component and connection, and it is the
    field that answers "where did this come from?" in a review. When the
    extraction recorded nothing, that absence is written down in plain words
    rather than covered up with a fake bbox.
    """
    if isinstance(raw, Mapping):
        kind = (_text(raw.get("kind")) or "").lower()
        value = raw.get("value")
        bbox = _as_bbox(value)
        out: dict[str, Any]
        if bbox is not None:
            out = {"kind": kind if kind in _EVIDENCE_KINDS else "bbox", "value": bbox}
        elif _text(value):
            out = {"kind": kind if kind in _EVIDENCE_KINDS else "text", "value": _text(value)}
        else:
            # Some extractors write prose under `description` and no `value` at
            # all. That prose IS the evidence; it just has another name.
            description = _text(raw.get("description"))
            out = (
                {"kind": "text", "value": description}
                if description
                else _no_evidence(subject)
            )
        label = raw.get("label_text")
        if isinstance(label, str):
            out["label_text"] = label
        return out

    literal = _text(raw)
    if literal:
        return {"kind": "text", "value": literal}
    return _no_evidence(subject)


def _no_evidence(subject: str) -> dict[str, Any]:
    return {
        "kind": "text",
        "value": f"extraction.json recorded no evidence for {subject}",
    }


def _enum_or_unknown(value: Any, allowed: frozenset[str]) -> tuple[str, str | None]:
    """``(normalized, discarded_literal)``.

    ``discarded_literal`` is non-None exactly when the extractor used a word
    outside the AIR vocabulary. The caller writes it into ``note`` so the
    information survives the mapping to ``"unknown"``.
    """
    literal = _text(value)
    if literal is None:
        return "unknown", None
    normalized = re.sub(r"[\s-]+", "_", literal.lower())
    if normalized in allowed:
        return normalized, None
    return "unknown", literal


def _join_notes(*parts: str | None) -> str | None:
    kept = [p for p in (_text(p) for p in parts) if p]
    return " ".join(kept) or None


# --------------------------------------------------------------------------- #
# the transform
# --------------------------------------------------------------------------- #


def build_fallback_air(
    extraction: Mapping[str, Any],
    *,
    run_id: str,
    source_kind: str | None = None,
    source_artifact: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an AIR dict from an extraction dict. Pure; no I/O, no model.

    Args:
        extraction: the parsed ``.arch/intake/<run_id>/extraction.json``.
        run_id: the run id. ``meta.run_id`` is pinned by the schema to
            ``YYYYMMDD-HHMM-<slug>``; a value that does not match is still
            written, so that the resulting AIR fails validation loudly instead
            of being silently attributed to a different run.
        source_kind: the run's declared source kind, when the caller knows it.
        source_artifact: relative path of the original drawing, when known.
        reason: one line on *why* the analyst did not run, quoted inside the
            blocking unknown. The stage error's title is a good value.
        now: injectable clock, for reproducible tests.

    Returns:
        A dict that validates against ``air.schema.json`` and that
        ``validate_air.py --gate`` is expected to REJECT.
    """
    stamp = now or datetime.now(timezone.utc)
    meta_in = _mapping(extraction.get("meta")) or _mapping(extraction.get("_meta")) or {}
    provenance = _mapping(extraction.get("_provenance")) or {}
    quality = _mapping(extraction.get("_quality")) or {}

    # Reserved before anything else is named. FALLBACK_UNKNOWN_ID is the id a
    # reader, the critic and the tests look the blocking unknown up by; letting
    # an extraction entry that happens to carry the same id take it first would
    # push the real one to `..._2` and quietly break that contract.
    taken: set[str] = {FALLBACK_UNKNOWN_ID}
    extra_unknowns: list[dict[str, Any]] = []

    components, component_ids = _build_components(
        extraction.get("components"), taken=taken
    )
    connections = _build_connections(
        extraction.get("connections"),
        component_ids=component_ids,
        taken=taken,
        dropped=extra_unknowns,
    )
    boundaries = _build_boundaries(
        extraction.get("boundaries"),
        component_ids=component_ids,
        taken=taken,
        notes=extra_unknowns,
    )
    _attach_boundaries(components, boundaries)

    # One unknown per connection the extraction itself was unsure about. This is
    # not analysis: it is the extraction's own confidence number, promoted into
    # a question a human can answer.
    for conn in connections:
        if conn["confidence"] < LOW_CONFIDENCE_BELOW:
            extra_unknowns.append(
                {
                    "id": _make_id(
                        f"low_confidence_{conn['id']}", prefix="unknown", taken=taken
                    ),
                    "about": conn["id"],
                    "question": (
                        f"The extraction read the connection {conn['from']} -> "
                        f"{conn['to']} with confidence {conn['confidence']:.2f}, below "
                        f"{LOW_CONFIDENCE_BELOW}. Is this connection real, and are its "
                        f"protocol ({conn['protocol']}) and synchronicity "
                        f"({conn['sync']}) correct?"
                    ),
                    "blocking": False,
                    "answer": None,
                }
            )

    resolved_kind, kind_observed = _resolve_source_kind(source_kind, meta_in, provenance)
    if not kind_observed:
        extra_unknowns.append(
            {
                "id": _make_id("source_kind_not_observed", prefix="unknown", taken=taken),
                "about": None,
                "question": (
                    "Neither the run nor the extraction recorded what kind of artifact "
                    f"the source was; the AIR says {_SOURCE_KIND_LAST_RESORT!r} because "
                    "the schema requires a value. What was it really (napkin, "
                    "whiteboard, screenshot, drawio, pdf, ...)?"
                ),
                "blocking": False,
                "answer": None,
            }
        )

    unknowns = _build_unknowns(extraction.get("unknowns"), taken=taken)
    unknowns.extend(extra_unknowns)
    unknowns.append(_blocking_unknown(reason))

    air: dict[str, Any] = {
        "air_version": AIR_VERSION,
        "meta": _build_meta(
            run_id=run_id,
            meta_in=meta_in,
            provenance=provenance,
            quality=quality,
            components=components,
            source_kind=resolved_kind,
            source_artifact=source_artifact,
            stamp=stamp,
        ),
        "components": components,
        "connections": connections,
        # NOT NEGOTIABLE. A transform has nothing to declare here, and an
        # assumption without a real impact statement is worse than no assumption
        # at all: stage 4 writes code from it. The blocking unknown above says
        # out loud that this list is empty because nobody reasoned, not because
        # there was nothing to assume.
        "assumptions": [],
        "unknowns": unknowns,
        "experiment_plan": _experiment_plan(),
    }
    if boundaries:
        air["boundaries"] = boundaries
    return air


# --------------------------------------------------------------------------- #
# section builders
# --------------------------------------------------------------------------- #


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _build_components(
    raw: Any, *, taken: set[str]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Components plus the ``original id -> AIR id`` map the edges need."""
    out: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}

    for index, item in enumerate(_sequence(raw)):
        entry = _mapping(item)
        if entry is None:
            continue
        original = _text(entry.get("id")) or f"component_{index + 1}"
        name = _text(entry.get("name")) or _text(entry.get("label")) or original
        new_id = _make_id(original, prefix="component", taken=taken)
        id_map[original] = new_id

        # Extractors disagree on the key: `kind` here, `type` there.
        kind, discarded = _enum_or_unknown(
            entry.get("kind") if entry.get("kind") is not None else entry.get("type"),
            _COMPONENT_KINDS,
        )
        note = _join_notes(
            _text(entry.get("note")),
            f"Extraction typed this {discarded!r}, which is not an AIR component kind."
            if discarded
            else None,
        )

        component: dict[str, Any] = {
            "id": new_id,
            "name": name,
            "kind": kind,
            "confidence": _confidence(entry.get("confidence")),
            "evidence": _evidence(entry.get("evidence"), subject=f"component {original!r}"),
        }
        tech = _text(entry.get("tech"))
        if tech:
            component["tech"] = tech
        responsibilities = [r for r in (_text(x) for x in _sequence(entry.get("responsibilities"))) if r]
        if responsibilities:
            component["responsibilities"] = responsibilities
        if note:
            component["note"] = note
        tools = _build_tools(entry.get("tools"), owner=new_id, taken=taken)
        if tools:
            component["tools"] = tools
        out.append(component)

    return out, id_map


def _build_tools(raw: Any, *, owner: str, taken: set[str]) -> list[dict[str, Any]]:
    """Inline ``components[].tools[]``, when the extraction produced any."""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(_sequence(raw)):
        entry = _mapping(item)
        if entry is None:
            continue
        original = _text(entry.get("id")) or f"{owner}_tool_{index + 1}"
        name = _text(entry.get("name")) or _text(entry.get("label")) or original
        tool: dict[str, Any] = {
            "id": _make_id(original, prefix="tool", taken=taken),
            "name": name,
            "kind": "tool",
        }
        tech = _text(entry.get("tech"))
        if tech:
            tool["tech"] = tech
        note = _text(entry.get("note"))
        if note:
            tool["note"] = note
        if entry.get("confidence") is not None:
            tool["confidence"] = _confidence(entry.get("confidence"))
        if entry.get("evidence") is not None:
            tool["evidence"] = _evidence(
                entry.get("evidence"), subject=f"tool {original!r}"
            )
        out.append(tool)
    return out


def _build_connections(
    raw: Any,
    *,
    component_ids: Mapping[str, str],
    taken: set[str],
    dropped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Edges whose two endpoints exist; the rest become unknowns.

    A dangling edge is a known failure mode of vision extraction (a loose label
    read as a node). Keeping it would make the AIR fail semantic validation for
    a reason that has nothing to do with the analyst being absent, and inventing
    the missing component would be exactly the fabrication this module refuses.
    Dropping it and asking about it by name loses nothing: the question carries
    the original endpoint ids and the edge label.

    These are non-blocking on purpose. The single blocking unknown is the one
    about the missing contextualization — that is the headline the human should
    read at the gate, not a queue of edge-level noise.
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(_sequence(raw)):
        entry = _mapping(item)
        if entry is None:
            continue
        original = _text(entry.get("id")) or f"connection_{index + 1}"
        src = _text(entry.get("from"))
        dst = _text(entry.get("to"))
        label = _text(entry.get("label"))
        evidence = _evidence(entry.get("evidence"), subject=f"connection {original!r}")
        if label is None:
            label = _text(evidence.get("label_text"))

        if src not in component_ids or dst not in component_ids:
            dropped.append(
                {
                    "id": _make_id(f"dangling_{original}", prefix="unknown", taken=taken),
                    "about": None,
                    "question": (
                        f"The extraction recorded a connection {original!r} from "
                        f"{src!r} to {dst!r}"
                        + (f" labelled {label!r}" if label else "")
                        + ", but at least one of those endpoints is not a component it "
                        "extracted. Which components does this arrow really join, or "
                        "should it be discarded?"
                    ),
                    "blocking": False,
                    "answer": None,
                }
            )
            continue

        protocol, discarded_protocol = _enum_or_unknown(entry.get("protocol"), _PROTOCOLS)
        sync, discarded_sync = _enum_or_unknown(entry.get("sync"), _SYNC)
        note = _join_notes(
            _text(entry.get("note")),
            f"Extraction called the protocol {discarded_protocol!r}, which is not an "
            "AIR protocol." if discarded_protocol else None,
            f"Extraction called the synchronicity {discarded_sync!r}, which is not an "
            "AIR sync value." if discarded_sync else None,
        )

        connection: dict[str, Any] = {
            "id": _make_id(original, prefix="connection", taken=taken),
            "from": component_ids[src],
            "to": component_ids[dst],
            "protocol": protocol,
            "sync": sync,
            "confidence": _confidence(entry.get("confidence")),
            "evidence": evidence,
        }
        if label:
            connection["label"] = label
        payload = _text(entry.get("payload"))
        if payload:
            connection["payload"] = payload
        if note:
            connection["note"] = note
        if isinstance(entry.get("verified_by_second_pass"), bool):
            connection["verified_by_second_pass"] = entry["verified_by_second_pass"]
        out.append(connection)
    return out


def _build_boundaries(
    raw: Any,
    *,
    component_ids: Mapping[str, str],
    taken: set[str],
    notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Boundaries, with ``contains`` filtered to components that exist.

    ``boundaries[].kind`` has no "unknown" member and no ``note`` field to park
    a discarded literal in, so an unrecognised kind is mapped to ``logical``
    (the member that asserts the least — a grouping with no infrastructure
    meaning) and the original word is preserved as an unknown.
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(_sequence(raw)):
        entry = _mapping(item)
        if entry is None:
            continue
        original = _text(entry.get("id")) or f"boundary_{index + 1}"
        new_id = _make_id(original, prefix="boundary", taken=taken)
        kind, discarded = _enum_or_unknown(entry.get("kind"), _BOUNDARY_KINDS)
        if kind == "unknown":
            kind = "logical"
            if discarded:
                notes.append(
                    {
                        "id": _make_id(
                            f"boundary_kind_{original}", prefix="unknown", taken=taken
                        ),
                        "about": new_id,
                        "question": (
                            f"The extraction typed the boundary {original!r} as "
                            f"{discarded!r}, which is not an AIR boundary kind; it was "
                            "recorded as 'logical'. What kind of boundary is it (vpc, "
                            "namespace, cluster, zone, onprem, cloud, dmz, account, "
                            "logical, external_system)?"
                        ),
                        "blocking": False,
                        "answer": None,
                    }
                )

        boundary: dict[str, Any] = {
            "id": new_id,
            "name": _text(entry.get("name")) or _text(entry.get("label")) or original,
            "kind": kind,
        }
        contains = [
            component_ids[c]
            for c in (_text(x) for x in _sequence(entry.get("contains")))
            if c in component_ids
        ]
        if contains:
            boundary["contains"] = contains
        if entry.get("confidence") is not None:
            boundary["confidence"] = _confidence(entry.get("confidence"))
        out.append(boundary)
    return out


def _attach_boundaries(
    components: list[dict[str, Any]], boundaries: list[dict[str, Any]]
) -> None:
    """Set ``components[].boundary`` from ``boundaries[].contains``.

    Derived from the boundary side rather than trusting a ``boundary`` key on
    the component, because only the boundary side has been through the id
    mapping. A component listed by two boundaries keeps the first, which is the
    schema's shape (one boundary per component) rather than a judgement.
    """
    owner: dict[str, str] = {}
    for boundary in boundaries:
        for cid in boundary.get("contains", []):
            owner.setdefault(cid, boundary["id"])
    for component in components:
        parent = owner.get(component["id"])
        if parent:
            component["boundary"] = parent


def _build_unknowns(raw: Any, *, taken: set[str]) -> list[dict[str, Any]]:
    """The extraction's own unknowns, normalized to the AIR shape.

    ``about`` is dropped rather than remapped: extractions record it against
    their own ids (and sometimes as a list under ``related_elements``), and a
    wrong reference is worse than none — ``validate_air.py`` warns about ids it
    cannot resolve. The original id stays visible inside the question text.
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(_sequence(raw)):
        entry = _mapping(item)
        if entry is None:
            continue
        original = _text(entry.get("id")) or f"unknown_{index + 1}"
        question = (
            _text(entry.get("question"))
            or _text(entry.get("description"))
            or _text(entry.get("text"))
        )
        if question is None:
            continue
        related = [r for r in (_text(x) for x in _sequence(entry.get("related_elements"))) if r]
        if related:
            question = f"{question} (extraction ids: {', '.join(related)})"
        if len(question) < 10:
            question = _SHORT_QUESTION_WRAPPER.format(text=question)

        unknown: dict[str, Any] = {
            "id": _make_id(original, prefix="unknown", taken=taken),
            "question": question,
            "blocking": bool(entry.get("blocking")),
            "answer": _text(entry.get("answer")),
        }
        options = [o for o in (_text(x) for x in _sequence(entry.get("options"))) if o]
        if options:
            unknown["options"] = options
        out.append(unknown)
    return out


def _blocking_unknown(reason: str | None) -> dict[str, Any]:
    """The unknown whose whole job is to make the stage-3 gate say no.

    Takes its id verbatim rather than through :func:`_make_id`: the id is
    reserved up front by the caller precisely so this one cannot be renamed.
    """
    # The reason is a stage error title, which does not end in punctuation; the
    # sentence that follows it is appended verbatim, so terminate it here or the
    # two run together in the gate's output.
    detail = _text(reason)
    because = f" The analyst stage did not complete: {detail.rstrip('.')}." if detail else ""
    return {
        "id": FALLBACK_UNKNOWN_ID,
        "about": None,
        "question": (
            "The contextualization stage (arch-analyst) did not complete, so this AIR "
            "was derived mechanically from extraction.json. NO ASSUMPTION WAS DECLARED "
            "AND NO IMPACT WAS ASSESSED: assumptions[] is empty because nobody reasoned "
            "about the drawing, not because the drawing left nothing open. Everything "
            "here is what the extraction observed and nothing else." + because +
            " Re-run the contextualization stage, or fill assumptions[] by hand, before "
            "any code is generated from this AIR."
        ),
        "options": [
            "Re-run the arch-analyst stage (it is retryable; a stall is not a budget problem)",
            "Complete assumptions[] and experiment_plan by hand and re-run the critic",
            "Abandon this run",
        ],
        "blocking": True,
        "answer": None,
    }


def _experiment_plan() -> dict[str, Any]:
    """A minimal, honest plan.

    The schema requires at least one hypothesis and at least one out-of-scope
    item, so "empty" is not available. The one hypothesis offered is about this
    artifact's own provenance — the only claim that can be made and refuted
    without reasoning about the architecture. Inventing a hypothesis about the
    system (latency, throughput, a broker choice) would be precisely the
    fabrication this module exists to avoid.
    """
    return {
        "goal": (
            "None. No experiment was designed: the contextualization stage did not run."
        ),
        "hypotheses": [
            {
                "id": "h1",
                "statement": (
                    "Every component and connection in this AIR was observed by the "
                    "extraction stage; none was added by reasoning."
                ),
                "falsifiable_by": (
                    "Diffing this AIR against .arch/intake/<run_id>/extraction.json: any "
                    "components[] or connections[] entry with no counterpart there "
                    "refutes it."
                ),
            }
        ],
        "stack": {},
        "out_of_scope": [
            "Everything about the system's behaviour. This AIR carries no assumption, "
            "no technology choice and no hypothesis about the architecture, so there is "
            "nothing here a prototype could validate.",
        ],
    }


def _resolve_source_kind(
    source_kind: str | None,
    meta_in: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> tuple[str, bool]:
    """``(kind, was_observed)``. See ``_SOURCE_KIND_LAST_RESORT``."""
    for candidate in (source_kind, meta_in.get("source_kind"), provenance.get("source_kind")):
        normalized = (_text(candidate) or "").lower()
        if normalized in _SOURCE_KINDS:
            return normalized, True
    return _SOURCE_KIND_LAST_RESORT, False


def _overall_confidence(
    quality: Mapping[str, Any],
    extraction_level: Any,
    components: Sequence[Mapping[str, Any]],
) -> float:
    """The extraction's own number when it reported one, else the mean it implies.

    Never a constant: ``meta.overall_confidence`` is one of the two numbers the
    stage-3 gate thresholds on, and a hard-coded value here would be a claim
    about a drawing this module never saw.
    """
    for candidate in (
        extraction_level,
        quality.get("overall_confidence"),
        quality.get("avg_confidence"),
    ):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return _confidence(candidate)
    scores = [c["confidence"] for c in components if "confidence" in c]
    if scores:
        return _confidence(sum(scores) / len(scores))
    return 0.0


def _build_meta(
    *,
    run_id: str,
    meta_in: Mapping[str, Any],
    provenance: Mapping[str, Any],
    quality: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    source_kind: str,
    source_artifact: str | None,
    stamp: datetime,
) -> dict[str, Any]:
    artifact = (
        _text(source_artifact)
        or _text(meta_in.get("source_artifact"))
        or _text(meta_in.get("source_file"))
        or _text(provenance.get("source_artifact"))
        or "unknown"
    )
    extracted_at = (
        _text(meta_in.get("extracted_at"))
        or _text(provenance.get("extracted_at"))
        or stamp.isoformat().replace("+00:00", "Z")
    )

    meta: dict[str, Any] = {
        "run_id": run_id,
        "source_artifact": artifact,
        "source_kind": source_kind,
        "extracted_at": extracted_at,
        # The one field that has to be read for this AIR to be understood.
        "extractor": FALLBACK_EXTRACTOR,
        "overall_confidence": _overall_confidence(
            quality, meta_in.get("overall_confidence"), components
        ),
    }

    path = (_text(meta_in.get("extraction_path")) or _text(provenance.get("extraction_path")) or "").lower()
    if path in _EXTRACTION_PATHS:
        meta["extraction_path"] = path

    sha = (_text(meta_in.get("source_sha256")) or _text(meta_in.get("sha256")) or "").lower()
    if _SHA256_RE.match(sha):
        meta["source_sha256"] = sha

    title = _text(meta_in.get("title"))
    if title:
        meta["title"] = title

    # Nobody reviewed this. Saying so explicitly is cheaper than letting a reader
    # wonder whether the key is missing or the review is missing.
    meta["human_reviewed_by"] = None
    return meta
