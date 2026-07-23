#!/usr/bin/env python3
"""
negotiate.py — capability negotiation between an AIR and a platform profile.

The model is the MCP handshake: both sides announce what they can do and the
session proceeds only over the intersection. Here the drawing announces what it
needs (component kinds, protocols, sync modes) and the target announces what it
supports. Everything in the drawing and not in the target becomes

    (a) an explicit refusal with a reason and a way forward, or
    (b) a question for the human,

and never (c) a silent invention. That third option is the only one that produces
code which looks right and is wrong, and it is the one every generator takes by
default.

This runs BEFORE any `bob --chat-mode=...` subprocess. One trivial Bob stage in
this repo measured 37,154 tokens; refusing here costs nothing and refusing there
costs a stage that could never have ended in valid output anyway.

Four verdicts, not two:

    REFUSAL    the target cannot express this, with the redraw that would work
    QUESTION   the drawing is ambiguous or the target needs a fact nobody stated
    DOWNGRADE  it will be generated, but part of the intent becomes documentation
    RESOLVED   a parameter the AIR answered on its own, no human needed
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

try:  # library or script, both have to work
    from profiles import Profile
except ImportError:  # pragma: no cover
    from .profiles import Profile  # type: ignore


# ---------------------------------------------------------------------------
@dataclass
class Refusal:
    subject: str
    subject_name: str
    group: str
    feature: str
    reason: str
    workaround: Optional[str] = None


@dataclass
class Question:
    id: str
    text: str
    source: str                       # air | profile | inference
    blocking: bool = True
    about: Optional[str] = None
    options: List[str] = field(default_factory=list)
    why: Optional[str] = None
    param: Optional[str] = None


@dataclass
class Downgrade:
    subject: str
    group: str
    feature: str
    detail: str


@dataclass
class Resolution:
    param: str
    value: Any
    source: str


@dataclass
class Negotiation:
    profile_id: str
    profile_status: str
    validation_level: str
    refusals: List[Refusal] = field(default_factory=list)
    questions: List[Question] = field(default_factory=list)
    downgrades: List[Downgrade] = field(default_factory=list)
    resolutions: List[Resolution] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def blocking_questions(self) -> List[Question]:
        return [q for q in self.questions if q.blocking]

    @property
    def can_proceed(self) -> bool:
        return not self.refusals and not self.blocking_questions

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile_id,
            "profile_status": self.profile_status,
            "validation_level": self.validation_level,
            "can_proceed": self.can_proceed,
            "refusals": [asdict(r) for r in self.refusals],
            "questions": [asdict(q) for q in self.questions],
            "downgrades": [asdict(d) for d in self.downgrades],
            "resolutions": [asdict(r) for r in self.resolutions],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
def _dotted(air: Dict[str, Any], path: str) -> Any:
    """Read `meta.output_language` or `components[].tech` out of an AIR.

    `[]` collects across an array; for a collected value the most frequent
    non-null wins, which is how you infer "this is a Python system" from six
    components that mention Python and one that mentions nothing.
    """
    node: Any = air
    parts = path.split(".")
    for i, part in enumerate(parts):
        if part.endswith("[]"):
            key = part[:-2]
            arr = (node or {}).get(key) if isinstance(node, dict) else None
            if not isinstance(arr, list):
                return None
            rest = ".".join(parts[i + 1:])
            values = [_dotted(item, rest) if rest else item for item in arr]
            values = [v for v in values if v not in (None, "", [])]
            if not values:
                return None
            try:
                return Counter(values).most_common(1)[0][0]
            except TypeError:      # unhashable values: no sensible majority
                return values[0]
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def _predicate_matches(when: Dict[str, Any], air: Dict[str, Any]) -> bool:
    if when.get("always"):
        return True
    if "air_missing" in when:
        return _dotted(air, when["air_missing"]) in (None, "", [])
    if "air_present" in when:
        return _dotted(air, when["air_present"]) not in (None, "", [])
    if "has_component_kind" in when:
        wanted = when["has_component_kind"]
        if any(c.get("kind") == wanted for c in air.get("components", [])):
            return True
        # An inline tool counts as a tool being present, because it is one.
        if wanted == "tool":
            return any(c.get("tools") for c in air.get("components", []))
        return False
    if "has_protocol" in when:
        return any(c.get("protocol") == when["has_protocol"] for c in air.get("connections", []))
    if "has_sync_mode" in when:
        return any(c.get("sync") == when["has_sync_mode"] for c in air.get("connections", []))
    if "has_boundary_kind" in when:
        return any(b.get("kind") == when["has_boundary_kind"] for b in air.get("boundaries", []))
    return False


def _refuse(profile: Profile, group: str, feature: str, subject: str, name: str) -> Refusal:
    exc = profile.exclusion(group, feature)
    return Refusal(
        subject=subject,
        subject_name=name,
        group=group,
        feature=feature,
        reason=exc.reason if exc else profile.excluded_reason(group, feature),
        workaround=exc.workaround if exc else None,
    )


# ---------------------------------------------------------------------------
def negotiate(air: Dict[str, Any], profile: Profile) -> Negotiation:
    """Intersect the drawing's needs with the target's capabilities."""
    neg = Negotiation(
        profile_id=profile.id,
        profile_status=profile.status,
        validation_level=profile.validation_level,
    )

    declared = (air.get("meta") or {}).get("target_profile")
    if declared and declared != profile.id:
        neg.warnings.append(
            f"meta.target_profile says '{declared}' but this negotiation is against "
            f"'{profile.id}'. The same AIR against a different profile is a different "
            f"system — update meta.target_profile, or negotiate against the declared one."
        )
    if profile.status != "verified":
        neg.warnings.append(
            f"Profile '{profile.id}' is status '{profile.status}': its artifact contract "
            f"has not been checked against an installed toolchain. Read `provenance` in "
            f"target.yaml before trusting the generated files."
        )

    # -- components ---------------------------------------------------------
    for comp in air.get("components", []):
        cid, kind = comp.get("id", "?"), comp.get("kind")
        cname = comp.get("name", cid)
        if kind == "unknown":
            neg.questions.append(Question(
                id=f"q_kind_{cid}",
                about=cid,
                text=f"What kind of component is '{cname}'? The extraction could not tell.",
                options=profile.supported("component_kinds"),
                blocking=True,
                source="air",
                why="A component with no kind has no artifact. Guessing it here decides the "
                    "technology of everything downstream from a shape on a whiteboard.",
            ))
        elif not profile.supports("component_kinds", kind):
            neg.refusals.append(_refuse(profile, "component_kinds", kind, cid, cname))

        if comp.get("tools") and not profile.supports("component_kinds", "tool"):
            neg.refusals.append(Refusal(
                subject=cid,
                subject_name=cname,
                group="component_kinds",
                feature="tool",
                reason=(
                    f"'{cname}' owns {len(comp['tools'])} tool(s) in the drawing, but "
                    f"{profile.id} has no artifact for a tool. Generating the component "
                    f"without its tools would produce something that cannot do its job."
                ),
                workaround=profile.exclusion("component_kinds", "tool").workaround
                if profile.exclusion("component_kinds", "tool") else
                "Use an agentic profile (orchestrate-adk, langgraph) for this part of "
                "the drawing.",
            ))

    # -- connections --------------------------------------------------------
    for conn in air.get("connections", []):
        eid = conn.get("id", "?")
        edge = f"{conn.get('from')} -> {conn.get('to')}"
        proto, sync = conn.get("protocol"), conn.get("sync")

        if proto == "unknown":
            neg.questions.append(Question(
                id=f"q_protocol_{eid}",
                about=eid,
                text=f"Which protocol carries {edge}?",
                options=profile.supported("protocols"),
                blocking=True,
                source="air",
                why="The protocol decides the adapter, the contract and the failure mode. "
                    "Defaulting it to HTTP is how a message bus becomes a REST call nobody asked for.",
            ))
        elif not profile.supports("protocols", proto):
            neg.refusals.append(_refuse(profile, "protocols", proto, eid, edge))

        if sync == "unknown":
            neg.questions.append(Question(
                id=f"q_sync_{eid}",
                about=eid,
                text=f"Is {edge} synchronous or asynchronous?",
                options=profile.supported("sync_modes"),
                blocking=True,
                source="air",
                why="Sync and async are different code, different error handling and "
                    "different tests. An arrow without a head does not say which.",
            ))
        elif not profile.supports("sync_modes", sync):
            neg.refusals.append(_refuse(profile, "sync_modes", sync, eid, edge))

    # -- boundaries: never a refusal, always a declared loss ----------------
    for bnd in air.get("boundaries", []):
        bkind = bnd.get("kind")
        if bkind and not profile.supports("boundary_kinds", bkind):
            exc = profile.exclusion("boundary_kinds", bkind)
            neg.downgrades.append(Downgrade(
                subject=bnd.get("id", "?"),
                group="boundary_kinds",
                feature=bkind,
                detail=(exc.reason if exc else profile.excluded_reason("boundary_kinds", bkind))
                       + (f" What to do instead: {exc.workaround}" if exc and exc.workaround else "")
                       + " It becomes documentation, not generated structure.",
            ))

    honored = set((profile.raw.get("capabilities") or {}).get("nonfunctional_honored") or [])
    for nfr in air.get("nonfunctional", []) or []:
        if nfr.get("kind") not in honored:
            neg.downgrades.append(Downgrade(
                subject=",".join(nfr.get("applies_to", [])) or "(whole system)",
                group="nonfunctional",
                feature=nfr.get("kind", "?"),
                detail=(
                    f"\"{nfr.get('text')}\" is not something {profile.id} turns into code. "
                    f"It is carried into the README so it is not lost, but nothing generated "
                    f"here enforces it."
                ),
            ))

    # -- the model's own blocking unknowns ---------------------------------
    for unk in air.get("unknowns", []) or []:
        if unk.get("blocking") and not unk.get("answer"):
            neg.questions.append(Question(
                id=unk.get("id", "u_?"),
                about=unk.get("about"),
                text=unk.get("question", ""),
                options=list(unk.get("options") or []),
                blocking=True,
                source="air",
                why="Raised by the extraction itself: the model knew it did not know.",
            ))

    # -- what the profile needs and the AIR did not say --------------------
    for rule in profile.inference_rules:
        param = rule["param"]
        value = _dotted(air, rule["from"])
        if value not in (None, "", []):
            neg.resolutions.append(Resolution(param, value, f"air:{rule['from']}"))
        elif rule["when_absent"] == "default":
            neg.resolutions.append(Resolution(param, rule.get("default"), "profile-default"))
        elif rule["when_absent"] == "ask":
            neg.questions.append(Question(
                id=f"q_param_{param}",
                text=rule.get("question", f"What value should '{param}' take?"),
                options=list(rule.get("options") or []),
                blocking=bool(rule.get("blocking", True)),
                source="inference",
                param=param,
                why=f"The AIR does not answer '{rule['from']}' and this profile has no "
                    f"default it is willing to invent.",
            ))

    asked = {q.param for q in neg.questions if q.param}
    for pq in profile.questions:
        if not _predicate_matches(pq.get("when", {}), air):
            continue
        if pq.get("param") and pq["param"] in asked:
            continue           # an inference rule already asked for this exact value
        neg.questions.append(Question(
            id=pq["id"],
            text=pq["question"],
            options=list(pq.get("options") or []),
            blocking=bool(pq.get("blocking", True)),
            source="profile",
            param=pq.get("param"),
            why=pq.get("why"),
        ))

    return neg


# ---------------------------------------------------------------------------
def render(neg: Negotiation, air: Dict[str, Any]) -> str:
    """Human-readable report. Refusals first: they are the ones that end the run."""
    meta = air.get("meta") or {}
    lines: List[str] = []
    lines.append(f"AIR      : {meta.get('run_id', '?')}  ({meta.get('title') or 'untitled'})")
    lines.append(f"Profile  : {neg.profile_id}  [status: {neg.profile_status}]")
    lines.append(f"Validation level: {neg.validation_level}"
                 + ("   <- nothing offline can prove this compiles"
                    if neg.validation_level == "structural-only" else ""))
    lines.append("-" * 74)

    if neg.refusals:
        lines.append(f"REFUSED ({len(neg.refusals)}) — this target cannot express these:")
        for r in neg.refusals:
            lines.append(f"  x {r.subject_name} [{r.subject}] :: {r.group}={r.feature}")
            lines.append(f"      {r.reason}")
            if r.workaround:
                lines.append(f"      What to do instead: {r.workaround}")
        lines.append("")

    if neg.questions:
        blocking = [q for q in neg.questions if q.blocking]
        lines.append(f"QUESTIONS ({len(neg.questions)}, {len(blocking)} blocking):")
        for q in sorted(neg.questions, key=lambda q: (not q.blocking, q.id)):
            mark = "!" if q.blocking else "?"
            about = f" [{q.about}]" if q.about else ""
            lines.append(f"  {mark} {q.id}{about} ({q.source}): {q.text}")
            if q.options:
                lines.append(f"      options: {', '.join(str(o) for o in q.options)}")
            if q.why:
                lines.append(f"      why it matters: {q.why}")
        lines.append("")

    if neg.downgrades:
        lines.append(f"DOWNGRADES ({len(neg.downgrades)}) — generated, but part of the intent "
                     f"becomes documentation:")
        for d in neg.downgrades:
            lines.append(f"  ~ {d.subject} :: {d.group}={d.feature}")
            lines.append(f"      {d.detail}")
        lines.append("")

    if neg.resolutions:
        lines.append(f"RESOLVED ({len(neg.resolutions)}) — read from the AIR, nobody asked:")
        for r in neg.resolutions:
            lines.append(f"  = {r.param} = {r.value!r}   ({r.source})")
        lines.append("")

    for w in neg.warnings:
        lines.append(f"  ! {w}")
    if neg.warnings:
        lines.append("")

    lines.append("-" * 74)
    if neg.can_proceed:
        lines.append("CAN PROCEED: the drawing fits inside this target's capabilities.")
    elif neg.refusals:
        lines.append(
            f"BLOCKED: {len(neg.refusals)} refusal(s). Redraw as described above, or pick a "
            f"target that supports them — `target_engine.py match <air.json>` ranks the five."
        )
    else:
        lines.append(
            f"BLOCKED: {len(neg.blocking_questions)} blocking question(s). Answer them at the "
            f"gate; every one of them decides code that cannot be inferred from the drawing."
        )
    return "\n".join(lines)


def to_json(neg: Negotiation) -> str:
    return json.dumps(neg.to_dict(), indent=2, ensure_ascii=False)
