#!/usr/bin/env python3
"""
target_engine.py — the CLI over the platform profiles.

    python3 target_engine.py list
    python3 target_engine.py show <profile>
    python3 target_engine.py doctor [--profile <id>]
    python3 target_engine.py negotiate <air.json> --profile <id> [--json]
    python3 target_engine.py match <air.json> [--json]
    python3 target_engine.py check <project_dir> --profile <id> [--json]

Exit codes are meant to be branched on by the pipeline:

    0  fine — can proceed / all gates green
    1  refused, or a gate failed, or a profile is broken
    2  blocked on questions for the human
    3  usage error

Order of operations that the whole design exists to enforce: `negotiate` runs
BEFORE `bob --chat-mode=arch-scaffold`. A refusal here is free; the same refusal
after a scaffold stage costs tens of thousands of tokens and minutes of billed
job time, on a run that could never have ended valid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from negotiate import negotiate, render, to_json          # noqa: E402
from profiles import ProfileError, Registry, air_enums    # noqa: E402
import toolchain                                          # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_QUESTIONS, EXIT_USAGE = 0, 1, 2, 3


# ---------------------------------------------------------------------------
def _load_air(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: {path} does not exist.\n"
                         f"  Fix: point this at .arch/air/<run>/air.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: {path} is not valid JSON — {exc}")


def _registry(strict: bool = False) -> Registry:
    try:
        reg = Registry.discover(strict=strict)
    except ProfileError as exc:
        raise SystemExit(f"ERROR: {exc}")
    for pid, err in getattr(reg, "errors", {}).items():
        print(f"[WARN] profile '{pid}' did not load:\n{err}\n", file=sys.stderr)
    return reg


# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    reg = _registry()
    enums = air_enums()
    if args.json:
        print(json.dumps([
            {
                "id": p.id, "name": p.name, "status": p.status,
                "validation_level": p.validation_level,
                "effective_level": toolchain.effective_level(p, toolchain.plan(p)),
                "component_kinds": p.supported("component_kinds"),
                "protocols": p.supported("protocols"),
                "coverage_gaps": p.coverage_gaps(enums),
                "summary": p.summary,
            }
            for p in (reg[k] for k in sorted(reg))
        ], indent=2, ensure_ascii=False))
        return EXIT_OK

    print(f"{len(reg)} profile(s) discovered under "
          f"{Path(__file__).resolve().parents[1] / 'profiles'}\n")
    for p in (reg[k] for k in sorted(reg)):
        eff = toolchain.effective_level(p, toolchain.plan(p))
        flag = "" if eff == p.validation_level else f" (effective here: {eff})"
        print(f"  {p.id}")
        print(f"    {p.name}")
        print(f"    status={p.status}  validation={p.validation_level}{flag}")
        print(f"    kinds: {', '.join(p.supported('component_kinds'))}")
        gaps = p.coverage_gaps(enums)
        if gaps:
            print(f"    !! undeclared vocabulary (will be refused): {gaps}")
        print()
    return EXIT_OK if not getattr(reg, "errors", {}) else EXIT_REFUSED


def cmd_show(args: argparse.Namespace) -> int:
    reg = _registry()
    try:
        p = reg.require(args.profile)
    except ProfileError as exc:
        raise SystemExit(f"ERROR: {exc}")

    if args.json:
        print(json.dumps(p.raw, indent=2, ensure_ascii=False))
        return EXIT_OK

    print(f"{p.id} — {p.name}")
    print(f"status: {p.status}   validation level: {p.validation_level}")
    print(f"\n{p.summary}\n")
    print("Provenance (why you should believe the artifact contract):")
    for line in p.provenance:
        print(f"  - {line}")
    for group in ("component_kinds", "protocols", "sync_modes", "boundary_kinds"):
        print(f"\n{group}")
        print(f"  supports: {', '.join(p.supported(group)) or '(none)'}")
        excl = p._excludes.get(group, {})
        for feature, exc in excl.items():
            print(f"  refuses {feature}: {exc.reason}")
            if exc.workaround:
                print(f"      -> {exc.workaround}")
    print("\nArtifacts")
    for art in p.artifacts:
        print(f"  {art['for_kind']:<34} {art['path']:<34} {art['evidence'][:6]}")
    if p.questions:
        print("\nQuestions this profile asks the human")
        for q in p.questions:
            print(f"  [{'blocking' if q['blocking'] else 'optional'}] {q['id']}: {q['question']}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    reg = _registry()
    targets = [reg.require(args.profile)] if args.profile else [reg[k] for k in sorted(reg)]
    payload, worst = [], EXIT_OK

    for p in targets:
        plans = toolchain.plan(p)
        eff = toolchain.effective_level(p, plans)
        payload.append({
            "profile": p.id,
            "declared_level": p.validation_level,
            "effective_level": eff,
            "checks": [
                {
                    "name": c.name, "runnable": c.runnable, "blocking": c.blocking,
                    "missing": c.missing, "confidence": c.confidence, "degrade": c.degrade,
                    "requirements": [
                        {"kind": r.kind, "name": r.name, "present": r.present,
                         "location": r.location}
                        for r in c.requirements
                    ],
                }
                for c in plans
            ],
        })
        if not args.json:
            print(f"{p.id}   declared={p.validation_level}   effective here={eff}")
            for c in plans:
                mark = "OK  " if c.runnable else "MISS"
                req = ", ".join(f"{r.kind}:{r.name}{'' if r.present else ' (absent)'}"
                                for r in c.requirements) or "no external requirement"
                print(f"  [{mark}] {c.name:<18} {req}")
                print(f"           {c.confidence}")
                if not c.runnable:
                    print(f"           DEGRADED: {c.degrade}")
            print()
        if eff != p.validation_level:
            worst = EXIT_REFUSED

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif worst != EXIT_OK:
        print("At least one profile validates weaker here than it declares. That is a "
              "statement about this machine, not about the profile — install the missing "
              "tools above, or ship with the effective level shown in the UI.")
    return worst


def cmd_negotiate(args: argparse.Namespace) -> int:
    reg = _registry()
    air = _load_air(Path(args.air))
    try:
        profile = reg.require(args.profile)
    except ProfileError as exc:
        raise SystemExit(f"ERROR: {exc}")

    neg = negotiate(air, profile)
    print(to_json(neg) if args.json else render(neg, air))
    if neg.refusals:
        return EXIT_REFUSED
    if neg.blocking_questions:
        return EXIT_QUESTIONS
    return EXIT_OK


def cmd_match(args: argparse.Namespace) -> int:
    """Rank every profile against one AIR. Answers 'so which target CAN do this?'."""
    reg = _registry()
    air = _load_air(Path(args.air))
    rows = []
    for p in (reg[k] for k in sorted(reg)):
        neg = negotiate(air, p)
        rows.append({
            "profile": p.id,
            "status": p.status,
            "can_proceed": neg.can_proceed,
            "refusals": len(neg.refusals),
            "blocking_questions": len(neg.blocking_questions),
            "downgrades": len(neg.downgrades),
            "validation_level": p.validation_level,
            "first_refusal": (neg.refusals[0].reason if neg.refusals else None),
        })
    rows.sort(key=lambda r: (r["refusals"], r["blocking_questions"], r["downgrades"]))

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return EXIT_OK if rows and rows[0]["refusals"] == 0 else EXIT_REFUSED

    print(f"{'profile':<24}{'refusals':>9}{'blocking Qs':>13}{'downgrades':>12}   validation")
    print("-" * 74)
    for r in rows:
        print(f"{r['profile']:<24}{r['refusals']:>9}{r['blocking_questions']:>13}"
              f"{r['downgrades']:>12}   {r['validation_level']}")
    print()
    best = rows[0] if rows else None
    if best and best["refusals"] == 0:
        print(f"Closest fit: {best['profile']} — nothing in the drawing is refused"
              + (f", {best['blocking_questions']} question(s) to answer first."
                 if best["blocking_questions"] else "."))
        return EXIT_OK
    print("Every profile refuses something in this drawing. Read `negotiate --profile "
          "<id>` for the reasons and the redraws that would work.")
    return EXIT_REFUSED


def cmd_check(args: argparse.Namespace) -> int:
    reg = _registry()
    try:
        profile = reg.require(args.profile)
    except ProfileError as exc:
        raise SystemExit(f"ERROR: {exc}")
    project = Path(args.project_dir)
    results = toolchain.run_checks(profile, project)

    if args.json:
        print(json.dumps({
            "profile": profile.id,
            "declared_level": profile.validation_level,
            "effective_level": toolchain.effective_level(profile, toolchain.plan(profile)),
            "results": toolchain.results_to_dict(results),
        }, indent=2, ensure_ascii=False))
    else:
        print(f"{profile.id} against {project}")
        for r in results:
            print(f"  [{r.status:<8}] {r.name}"
                  + (f"  ({r.detail})" if r.status in ("skipped", "no-files") else ""))
            if r.status == "fail":
                for line in r.detail.splitlines()[:20]:
                    print(f"             {line}")
        print("\n" + toolchain.summarize(results))
    return EXIT_REFUSED if any(r.status == "fail" and r.blocking for r in results) else EXIT_OK


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="target_engine.py",
        description="Platform profiles: discovery, capability negotiation, offline gates.",
    )
    sub = ap.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="every discovered profile")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="one profile in full")
    p_show.add_argument("profile")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_doc = sub.add_parser("doctor", help="which offline gates this machine can run")
    p_doc.add_argument("--profile")
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    p_neg = sub.add_parser("negotiate", help="intersect an AIR with one profile")
    p_neg.add_argument("air")
    p_neg.add_argument("--profile", required=True)
    p_neg.add_argument("--json", action="store_true")
    p_neg.set_defaults(func=cmd_negotiate)

    p_match = sub.add_parser("match", help="rank every profile against one AIR")
    p_match.add_argument("air")
    p_match.add_argument("--json", action="store_true")
    p_match.set_defaults(func=cmd_match)

    p_chk = sub.add_parser("check", help="run the offline gates on a generated project")
    p_chk.add_argument("project_dir")
    p_chk.add_argument("--profile", required=True)
    p_chk.add_argument("--json", action="store_true")
    p_chk.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
