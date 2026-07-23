#!/usr/bin/env python3
"""
profiles.py — discovery and strict loading of platform profiles.

A profile is a directory under `profiles/` containing a `target.yaml`. Adding a
target means adding a directory; it never means editing this file. That is the
registration-by-convention pattern every mature generator converged on, and the
reason is always the same: the day a new target needs a core change is the day
nobody adds targets any more.

Loading is deliberately strict. A profile that declares a capability the AIR
schema has never heard of is not a harmless typo — it is a capability that will
never match, so the target silently refuses work it was built to do. Better to
refuse to load and say which line is wrong.

Usage as a library:

    from profiles import Registry
    reg = Registry.discover()
    p = reg["orchestrate-adk"]
    p.supports("component_kinds", "database")      # False
    p.excluded_reason("component_kinds", "database")
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = SKILL_ROOT / "profiles"
PROFILE_SCHEMA = PROFILES_DIR / "profile.schema.json"
AIR_SCHEMA = SKILL_ROOT.parent / "air-normalizer" / "air.schema.json"

# Groups in target.yaml -> where the authoritative enum lives in air.schema.json.
CAPABILITY_SOURCES: Dict[str, Tuple[str, ...]] = {
    "component_kinds": ("properties", "components", "items", "properties", "kind", "enum"),
    "protocols": ("properties", "connections", "items", "properties", "protocol", "enum"),
    "sync_modes": ("properties", "connections", "items", "properties", "sync", "enum"),
    "boundary_kinds": ("properties", "boundaries", "items", "properties", "kind", "enum"),
}

# `unknown` is never a capability: it is the AIR admitting it could not read the
# drawing, which is always a question for the human and never a refusal.
NEVER_A_CAPABILITY = {"unknown"}

# for_kind accepts `kind`, `kind[key=value]` and the pseudo-kind `project`.
_SELECTOR = re.compile(r"^(?P<kind>[a-z_]+)(?:\[(?P<key>[a-z_]+)=(?P<value>[a-z_0-9-]+)\])?$")


class ProfileError(Exception):
    """Raised when a profile cannot be trusted. Always names the file and the field."""


# ---------------------------------------------------------------------------
def _yaml_load(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise ProfileError(
            "pyyaml is not installed, so no profile can be read.\n"
            "  Fix: pip install pyyaml"
        ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProfileError(f"{path}: not valid YAML — {exc}") from exc


def air_enums() -> Dict[str, List[str]]:
    """The capability vocabulary, read from air.schema.json rather than copied.

    Copying it here is how a profile ends up declaring support for a `kind` the
    schema dropped two releases ago and nobody noticing, because the mismatch is
    invisible until a real diagram happens to use it.
    """
    if not AIR_SCHEMA.exists():
        raise ProfileError(
            f"{AIR_SCHEMA} not found. The capability vocabulary is read from the AIR\n"
            "schema so the two can never drift; without it, profiles cannot be validated.\n"
            "  Fix: run this from a checkout where .bob/skills/air-normalizer/ exists."
        )
    schema = json.loads(AIR_SCHEMA.read_text(encoding="utf-8"))
    out: Dict[str, List[str]] = {}
    for group, path in CAPABILITY_SOURCES.items():
        node: Any = schema
        for step in path:
            node = node[step]
        out[group] = [v for v in node if v not in NEVER_A_CAPABILITY]
    return out


# ---------------------------------------------------------------------------
@dataclass
class Exclusion:
    feature: str
    reason: str
    workaround: Optional[str] = None

    def render(self, subject: str = "") -> str:
        head = f"{self.feature}: {self.reason}"
        if subject:
            head = f"{subject} — {head}"
        if self.workaround:
            head += f"\n      What to do instead: {self.workaround}"
        return head


@dataclass
class Profile:
    id: str
    name: str
    status: str
    summary: str
    path: Path
    raw: Dict[str, Any]
    provenance: List[str] = field(default_factory=list)
    _supports: Dict[str, List[str]] = field(default_factory=dict)
    _excludes: Dict[str, Dict[str, Exclusion]] = field(default_factory=dict)

    # -- capability queries -------------------------------------------------
    def supports(self, group: str, feature: str) -> bool:
        return feature in self._supports.get(group, [])

    def supported(self, group: str) -> List[str]:
        return list(self._supports.get(group, []))

    def exclusion(self, group: str, feature: str) -> Optional[Exclusion]:
        return self._excludes.get(group, {}).get(feature)

    def excluded_reason(self, group: str, feature: str) -> str:
        exc = self.exclusion(group, feature)
        if exc:
            return exc.reason
        return (
            f"'{feature}' is neither supported nor explicitly excluded by profile "
            f"'{self.id}'. An undeclared capability is treated as a refusal on purpose: "
            f"the alternative is generating something plausible with nothing behind it."
        )

    # -- artifacts ----------------------------------------------------------
    @property
    def artifacts(self) -> List[Dict[str, Any]]:
        return self.raw.get("artifacts", [])

    def artifacts_for(self, kind: str) -> List[Dict[str, Any]]:
        """Artifacts whose for_kind matches this component kind, selector included."""
        out = []
        for art in self.artifacts:
            m = _SELECTOR.match(art["for_kind"])
            if m and m.group("kind") == kind:
                out.append(art)
        return out

    @property
    def questions(self) -> List[Dict[str, Any]]:
        return self.raw.get("questions", [])

    @property
    def inference_rules(self) -> List[Dict[str, Any]]:
        return self.raw.get("inference_rules", [])

    @property
    def checks(self) -> List[Dict[str, Any]]:
        return self.raw.get("validate", {}).get("checks", [])

    @property
    def validation_level(self) -> str:
        return self.raw.get("validate", {}).get("level", "structural-only")

    # -- introspection ------------------------------------------------------
    def coverage_gaps(self, enums: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """Vocabulary a profile has an opinion about neither way.

        This is the hole the whole design exists to close. A feature that is
        neither supported nor excluded is where a generator quietly invents.
        """
        gaps: Dict[str, List[str]] = {}
        for group, values in enums.items():
            declared = set(self._supports.get(group, [])) | set(self._excludes.get(group, {}))
            missing = [v for v in values if v not in declared]
            if missing:
                gaps[group] = missing
        return gaps


# ---------------------------------------------------------------------------
def _validate_against_schema(doc: Dict[str, Any], path: Path) -> List[str]:
    """JSON Schema pass. Degrades loudly rather than silently when unavailable."""
    try:
        import jsonschema
    except ImportError:
        return [
            f"{path}: jsonschema is not installed, so target.yaml was NOT checked "
            f"against profile.schema.json — only the semantic rules below ran. "
            f"Fix: pip install jsonschema"
        ]
    schema = json.loads(PROFILE_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errs = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        errs.append(f"{path}: schema at {loc}: {err.message}")
    return errs


def _validate_semantics(doc: Dict[str, Any], path: Path, enums: Dict[str, List[str]]) -> List[str]:
    """The rules JSON Schema cannot express, because they compare two documents."""
    errs: List[str] = []
    caps = doc.get("capabilities", {})

    for group, vocabulary in enums.items():
        block = caps.get(group) or {}
        supports = block.get("supports", []) or []
        excludes = block.get("excludes", []) or []
        exc_features = [e.get("feature") for e in excludes]

        for feature in supports:
            if feature in NEVER_A_CAPABILITY:
                errs.append(
                    f"{path}: capabilities.{group}.supports lists '{feature}'. "
                    f"'unknown' means the extraction could not read the drawing; it is "
                    f"always a question for the human, never something a target supports."
                )
            elif feature not in vocabulary:
                errs.append(
                    f"{path}: capabilities.{group}.supports lists '{feature}', which is not "
                    f"in the AIR vocabulary for that group. Valid: {', '.join(vocabulary)}. "
                    f"A capability the AIR cannot express never matches anything."
                )
        for feature in exc_features:
            if feature not in vocabulary:
                errs.append(
                    f"{path}: capabilities.{group}.excludes refuses '{feature}', which is not "
                    f"in the AIR vocabulary for that group. Valid: {', '.join(vocabulary)}. "
                    f"Refusing something nobody can draw refuses nothing."
                )
        both = sorted(set(supports) & set(exc_features))
        if both:
            errs.append(
                f"{path}: capabilities.{group} both supports and excludes {both}. "
                f"The negotiation would depend on evaluation order — decide."
            )
        dupes = sorted({f for f in exc_features if exc_features.count(f) > 1})
        if dupes:
            errs.append(f"{path}: capabilities.{group}.excludes lists {dupes} more than once.")

    # Artifacts must be reachable: an artifact for a kind the profile refuses is
    # dead template that will never be written, and reads as support that is not there.
    kinds = enums["component_kinds"]
    supported_kinds = set((caps.get("component_kinds") or {}).get("supports", []) or [])
    for art in doc.get("artifacts", []) or []:
        raw_kind = art.get("for_kind", "")
        m = _SELECTOR.match(raw_kind)
        if not m:
            errs.append(
                f"{path}: artifacts[].for_kind '{raw_kind}' is not a kind, a "
                f"kind[key=value] selector, or the pseudo-kind 'project'."
            )
            continue
        kind = m.group("kind")
        if kind == "project":
            continue
        if kind not in kinds:
            errs.append(
                f"{path}: artifacts[].for_kind '{raw_kind}' refers to component kind "
                f"'{kind}', which is not in the AIR vocabulary."
            )
        elif kind not in supported_kinds:
            errs.append(
                f"{path}: artifacts[].for_kind '{raw_kind}' produces files for '{kind}', "
                f"but capabilities.component_kinds does not support '{kind}'. The artifact "
                f"can never be written, and the profile reads as if it could."
            )

    # An `ask` with no question is a dead end for whoever is at the gate.
    for rule in doc.get("inference_rules", []) or []:
        if rule.get("when_absent") == "ask" and not rule.get("question"):
            errs.append(
                f"{path}: inference_rules for '{rule.get('param')}' says when_absent: ask "
                f"but has no question. The human would be blocked with nothing to answer."
            )
        if rule.get("when_absent") == "default" and "default" not in rule:
            errs.append(
                f"{path}: inference_rules for '{rule.get('param')}' says when_absent: default "
                f"but declares no default value."
            )

    names = [c.get("name") for c in (doc.get("validate", {}).get("checks") or [])]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errs.append(f"{path}: validate.checks has duplicate names {dupes}.")

    # A check that can be skipped has to say what stops being true when it is.
    for check in doc.get("validate", {}).get("checks") or []:
        if check.get("requires") and not check.get("degrade"):
            errs.append(
                f"{path}: check '{check.get('name')}' has requirements but no `degrade` text. "
                f"A silently skipped gate is worse than no gate — it reads as a pass."
            )

    if doc.get("id") != path.parent.name:
        errs.append(
            f"{path}: id is '{doc.get('id')}' but the directory is '{path.parent.name}'. "
            f"Discovery is by directory name, so the two have to agree."
        )

    return errs


def _build(doc: Dict[str, Any], path: Path) -> Profile:
    prof = Profile(
        id=doc["id"],
        name=doc["name"],
        status=doc["status"],
        summary=doc["summary"].strip(),
        path=path,
        raw=doc,
        provenance=list(doc.get("provenance", [])),
    )
    for group, block in (doc.get("capabilities") or {}).items():
        if group == "nonfunctional_honored":
            continue
        prof._supports[group] = list((block or {}).get("supports", []) or [])
        prof._excludes[group] = {
            e["feature"]: Exclusion(e["feature"], e["reason"], e.get("workaround"))
            for e in (block or {}).get("excludes", []) or []
        }
    return prof


def _merge_inherited(child: Dict[str, Any], parent: Dict[str, Any]) -> Dict[str, Any]:
    """`extends`: capabilities merge group by group, artifacts by (for_kind, path)."""
    merged = dict(parent)
    merged.update({k: v for k, v in child.items() if k not in ("capabilities", "artifacts")})

    caps = dict(parent.get("capabilities") or {})
    caps.update(child.get("capabilities") or {})
    merged["capabilities"] = caps

    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {
        (a["for_kind"], a["path"]): a for a in parent.get("artifacts") or []
    }
    for art in child.get("artifacts") or []:
        by_key[(art["for_kind"], art["path"])] = art
    merged["artifacts"] = list(by_key.values())
    return merged


def load(path: Path, enums: Optional[Dict[str, List[str]]] = None,
         _seen: Optional[List[str]] = None) -> Profile:
    """Load one target.yaml, refusing anything that cannot be trusted."""
    enums = enums or air_enums()
    doc = _yaml_load(path)
    if not isinstance(doc, dict):
        raise ProfileError(f"{path}: top level is not a mapping.")

    parent_id = doc.get("extends")
    if parent_id:
        _seen = list(_seen or [])
        if parent_id in _seen:
            raise ProfileError(
                f"{path}: `extends` cycle {' -> '.join(_seen + [parent_id])}."
            )
        parent_path = PROFILES_DIR / parent_id / "target.yaml"
        if not parent_path.exists():
            raise ProfileError(
                f"{path}: extends '{parent_id}', but {parent_path} does not exist."
            )
        parent_doc = load(parent_path, enums, _seen + [doc.get("id", "?")]).raw
        doc = _merge_inherited(doc, parent_doc)
        doc["extends"] = parent_id

    errs = _validate_against_schema(doc, path)
    hard = [e for e in errs if "jsonschema is not installed" not in e]
    soft = [e for e in errs if "jsonschema is not installed" in e]
    hard += _validate_semantics(doc, path, enums)
    if hard:
        raise ProfileError(
            f"Profile '{path.parent.name}' is not loadable:\n  - " + "\n  - ".join(hard)
        )
    prof = _build(doc, path)
    prof.raw.setdefault("_warnings", []).extend(soft)
    return prof


# ---------------------------------------------------------------------------
class Registry(dict):
    """id -> Profile. Discovery is a directory scan; there is no import list."""

    errors: Dict[str, str]

    @classmethod
    def discover(cls, directory: Optional[Path] = None, strict: bool = False) -> "Registry":
        directory = directory or PROFILES_DIR
        if not directory.exists():
            raise ProfileError(
                f"{directory} does not exist. Profiles are discovered by scanning that "
                f"directory for subdirectories containing target.yaml."
            )
        enums = air_enums()
        reg = cls()
        reg.errors = {}
        for child in sorted(directory.iterdir()):
            manifest = child / "target.yaml"
            if not child.is_dir() or not manifest.exists():
                continue
            try:
                prof = load(manifest, enums)
            except ProfileError as exc:
                if strict:
                    raise
                reg.errors[child.name] = str(exc)
                continue
            reg[prof.id] = prof
        if not reg and not reg.errors:
            raise ProfileError(
                f"No profile found under {directory}. Each target needs its own "
                f"subdirectory with a target.yaml inside."
            )
        return reg

    def require(self, profile_id: str) -> Profile:
        if profile_id in self:
            return self[profile_id]
        if profile_id in getattr(self, "errors", {}):
            raise ProfileError(
                f"Profile '{profile_id}' exists but failed to load:\n{self.errors[profile_id]}"
            )
        raise ProfileError(
            f"Unknown profile '{profile_id}'. Available: "
            f"{', '.join(sorted(self)) or '(none)'}"
        )


def iter_profiles(reg: Registry) -> Iterable[Profile]:
    return (reg[k] for k in sorted(reg))
