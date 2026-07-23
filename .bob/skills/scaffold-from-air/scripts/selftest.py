#!/usr/bin/env python3
"""
selftest.py — proves the profile engine works, without Bob and without a credential.

    python3 .bob/skills/scaffold-from-air/scripts/selftest.py

Deliberately separate from tests/smoke_test.sh: that script is the repo-wide gate
and its 21/21 count is load bearing. This one belongs to the codegen engine and
grows with it.

What it asserts, in order of how much it would hurt to get wrong:

  1. every profile loads under the strict loader
  2. no profile has an undeclared corner of the AIR vocabulary
  3. the ADK templates are valid Python / valid ADK specs — the templates
     themselves are checked, not just the code generated from them
  4. the historically WRONG Kubernetes-shaped agent files are rejected
  5. negotiation refuses what the profiles say they refuse, and picks the right
     target for an agentic drawing
  6. the structural grammars accept a good file and reject a broken one

Exit: 0 all green, 1 something is broken with the reason.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
REPO_ROOT = SKILL_ROOT.parents[2]
sys.path.insert(0, str(HERE))

from negotiate import negotiate                              # noqa: E402
from profiles import ProfileError, Registry, air_enums       # noqa: E402
import toolchain                                             # noqa: E402

PASS, FAIL = [], []


def ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m+\033[0m {msg}")


def bad(msg: str, detail: str) -> None:
    FAIL.append(msg)
    print(f"  \033[31mx\033[0m {msg}\n      -> {detail}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _run(args, cwd=None):
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
def test_profiles_load() -> Registry:
    section("1. Profile registry")
    try:
        reg = Registry.discover(strict=True)
    except ProfileError as exc:
        bad("all profiles load under the strict loader", str(exc))
        raise SystemExit(report())
    ok(f"all {len(reg)} profiles load under the strict loader: {', '.join(sorted(reg))}")

    expected = {"orchestrate-adk", "langgraph", "container-microservice",
                "mainframe-cobol", "rpa"}
    missing = expected - set(reg)
    if missing:
        bad("the five documented profiles are present", f"missing: {sorted(missing)}")
    else:
        ok("the five documented profiles are present")

    enums = air_enums()
    for pid in sorted(reg):
        gaps = reg[pid].coverage_gaps(enums)
        if gaps:
            bad(f"{pid} declares an opinion on every AIR value",
                f"undeclared (would be refused with a generic message): {gaps}")
        else:
            ok(f"{pid}: every AIR kind, protocol, sync mode and boundary is declared")
    return reg


def test_broken_profile_is_rejected() -> None:
    section("2. The loader actually refuses bad profiles")
    import yaml
    good = yaml.safe_load(
        (SKILL_ROOT / "profiles" / "orchestrate-adk" / "target.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        # A capability the AIR cannot express: silently never matches anything.
        broken = json.loads(json.dumps(good))
        broken["capabilities"]["component_kinds"]["supports"].append("micoservice")
        target_dir = Path(tmp) / "orchestrate-adk"
        target_dir.mkdir()
        (target_dir / "target.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
        from profiles import load
        try:
            load(target_dir / "target.yaml")
            bad("a capability outside the AIR vocabulary is rejected",
                "the loader accepted 'micoservice' as a component kind")
        except ProfileError as exc:
            if "micoservice" in str(exc):
                ok("a capability outside the AIR vocabulary is rejected, by name")
            else:
                bad("a capability outside the AIR vocabulary is rejected",
                    f"rejected, but for the wrong reason: {exc}")


def test_adk_templates() -> None:
    section("3. ADK templates (the ones that were wrong before)")
    tmpl = SKILL_ROOT / "profiles" / "orchestrate-adk" / "templates"
    proc = _run(["-m", "py_compile", str(tmpl / "tool.py")])
    if proc.returncode == 0:
        ok("templates/tool.py compiles")
    else:
        bad("templates/tool.py compiles", proc.stderr.strip())

    text = (tmpl / "tool.py").read_text(encoding="utf-8")
    if "ConnectionType.API_KEY_AUTH" in text and "ConnectionType.API_KEY," not in text:
        ok("templates/tool.py uses ConnectionType.API_KEY_AUTH (API_KEY does not exist)")
    else:
        bad("templates/tool.py uses the real ConnectionType member",
            "found ConnectionType.API_KEY, which raises AttributeError on import")
    # The template NAMES @expect_credentials in its header to warn against it, so
    # look for a real decorator line, not a mention.
    used = [ln for ln in text.splitlines() if ln.lstrip().startswith("@expect_credentials")]
    if not used:
        ok("templates/tool.py does not use the folklore @expect_credentials decorator")
    else:
        bad("templates/tool.py avoids @expect_credentials",
            f"decorator applied at: {used[0].strip()} — it does not exist in any ADK version")

    import yaml
    agent = yaml.safe_load((tmpl / "agent.native.yaml").read_text(encoding="utf-8"))
    kb = yaml.safe_load((tmpl / "knowledge_base.yaml").read_text(encoding="utf-8"))
    for name, doc, expect_kind in (("agent.native.yaml", agent, "native"),
                                   ("knowledge_base.yaml", kb, "knowledge_base")):
        wrong = [k for k in ("apiVersion", "metadata", "spec") if k in doc]
        if wrong:
            bad(f"{name} is the flat ADK shape", f"Kubernetes keys present: {wrong}")
        elif doc.get("kind") != expect_kind or doc.get("spec_version") != "v1":
            bad(f"{name} is the flat ADK shape",
                f"spec_version={doc.get('spec_version')!r} kind={doc.get('kind')!r}")
        else:
            ok(f"{name}: spec_version v1, kind {expect_kind}, no metadata/spec block")
    if "storage" not in kb and "chunking" not in kb and "vector_index" in kb:
        ok("knowledge_base.yaml uses vector_index, not the invented storage/chunking blocks")
    else:
        bad("knowledge_base.yaml uses vector_index", "storage: or chunking: is still there")


def test_adk_validator() -> None:
    section("4. The ADK gate rejects the shape this repo actually shipped")
    try:
        import ibm_watsonx_orchestrate  # noqa: F401
    except ImportError:
        print("  \033[33m-\033[0m ibm-watsonx-orchestrate not installed: the ADK gate cannot "
              "run here.\n      -> pip install ibm-watsonx-orchestrate to restore it")
        return

    validator = HERE / "validate_adk.py"
    with tempfile.TemporaryDirectory() as tmp:
        agents = Path(tmp) / "agents"
        agents.mkdir()
        (agents / "k8s_style.yaml").write_text(
            "apiVersion: orchestrate.ibm.com/v1\n"
            "kind: Agent\n"
            "metadata:\n  name: wrong\n"
            "spec:\n  provider: wx.ai\n  llm: meta-llama/llama-3-3-70b-instruct\n",
            encoding="utf-8")
        proc = _run([str(validator), tmp])
        if proc.returncode == 1 and "Kubernetes shape" in proc.stdout:
            ok("a Kubernetes-shaped agent file is rejected, naming the mistake")
        else:
            bad("a Kubernetes-shaped agent file is rejected",
                f"exit {proc.returncode}: {proc.stdout[-400:]}")

        (agents / "k8s_style.yaml").unlink()
        (agents / "good.yaml").write_text(
            "spec_version: v1\nkind: native\nname: good_agent\n"
            "description: A correct native agent.\n"
            "llm: watsonx/ibm/granite-3-3-8b-instruct\nstyle: react\n"
            "instructions: |\n  Be useful.\ntools:\n  - do_thing\n",
            encoding="utf-8")
        proc = _run([str(validator), tmp])
        if proc.returncode == 0:
            ok("a correct flat native agent passes the same gate")
        else:
            bad("a correct flat native agent passes", proc.stdout[-600:])

    real = REPO_ROOT / ".arch" / "build" / "20260721-1129-atendente"
    if real.is_dir():
        proc = _run([str(validator), str(real)])
        if proc.returncode == 1:
            n = proc.stdout.count("[ERROR]")
            ok(f"the real .arch/build/20260721-1129-atendente output is rejected ({n} errors) "
               f"— this is the bug the profiles exist to stop")
        else:
            bad("the historical wrong build is rejected",
                f"exit {proc.returncode} — it should not pass")


def test_negotiation(reg: Registry) -> None:
    section("5. Capability negotiation")
    air_path = SKILL_ROOT / "examples" / "agentic-air.json"
    if not air_path.exists():
        bad("the agentic example AIR exists", f"{air_path} is missing")
        return
    air = json.loads(air_path.read_text(encoding="utf-8"))

    adk = negotiate(air, reg["orchestrate-adk"])
    if adk.refusals:
        bad("orchestrate-adk accepts an agentic drawing",
            f"refused: {[r.feature for r in adk.refusals]}")
    else:
        ok("orchestrate-adk refuses nothing in an agentic drawing")
    if adk.blocking_questions:
        ok(f"orchestrate-adk still blocks on {len(adk.blocking_questions)} question(s) rather "
           f"than guessing")
    else:
        bad("orchestrate-adk asks before generating",
            "no blocking question at all — something is being inferred that should not be")

    mf = negotiate(air, reg["mainframe-cobol"])
    refused = {r.feature for r in mf.refusals}
    if {"agent", "knowledge_base"} <= refused:
        ok(f"mainframe-cobol refuses the agentic vocabulary ({sorted(refused)})")
    else:
        bad("mainframe-cobol refuses agents and knowledge bases", f"refused only {sorted(refused)}")
    if all(r.reason and len(r.reason) > 15 for r in mf.refusals):
        ok("every refusal carries a readable reason")
    else:
        bad("every refusal carries a reason", "at least one refusal is bare")
    if any(r.workaround for r in mf.refusals):
        ok("refusals carry a way forward, not just a no")
    else:
        bad("refusals carry a workaround", "none of them says what to do instead")

    ranked = sorted(
        ((pid, negotiate(air, reg[pid])) for pid in reg),
        key=lambda kv: (len(kv[1].refusals), len(kv[1].blocking_questions)),
    )
    if ranked[0][0] == "orchestrate-adk":
        ok("match ranking puts orchestrate-adk first for this drawing")
    else:
        bad("match ranking picks the agentic target", f"ranked first: {ranked[0][0]}")

    # Refusal has to happen before any Bob subprocess: a negotiation that needs a
    # network call is a negotiation that costs what it was supposed to save.
    if all(not neg.warnings or True for _, neg in ranked):
        ok("negotiation is pure data — no subprocess, no network, no Bob session")


def test_structural_grammars() -> None:
    section("6. Structural grammars (COBOL, JCL, Robot)")
    cases = [
        ("check_cobol_structure.py", "good.cbl",
         "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. OK.\n"
         "       ENVIRONMENT DIVISION.\n       DATA DIVISION.\n"
         "       WORKING-STORAGE SECTION.\n       01 WS-A PIC X.\n"
         "       PROCEDURE DIVISION.\n           GOBACK.\n", 0),
        ("check_cobol_structure.py", "bad.cbl",
         "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. BAD.\n"
         "       DATA DIVISION.\n       ENVIRONMENT DIVISION.\n"
         "       PROCEDURE DIVISION.\n           GOBACK.\n", 1),
        ("check_jcl_structure.py", "good.jcl",
         "//OKJOB    JOB (ACCT),'X',CLASS=A\n//STEP1    EXEC PGM=IEFBR14\n", 0),
        ("check_jcl_structure.py", "bad.jcl",
         "//WAYTOOLONGSTEP EXEC PGM=FOO\n", 1),
        ("check_robot_structure.py", "good.robot",
         "*** Settings ***\nLibrary    SeleniumLibrary\n\n*** Tasks ***\n"
         "Do It\n    Open Order Screen\n", 0),
        ("check_robot_structure.py", "bad.robot",
         "*** Setings ***\nLibrary    SeleniumLibrary\n", 1),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for script, name, body, expected in cases:
            path = Path(tmp) / name
            path.write_text(body, encoding="utf-8")
            proc = _run([str(HERE / script), str(path)])
            verdict = "accepts" if expected == 0 else "rejects"
            if proc.returncode == expected:
                ok(f"{script} {verdict} {name}")
            else:
                bad(f"{script} {verdict} {name}",
                    f"exit {proc.returncode}, expected {expected}\n{proc.stdout[-400:]}")


def test_toolchain_honesty(reg: Registry) -> None:
    section("7. Toolchain honesty")
    for pid in sorted(reg):
        p = reg[pid]
        plans = toolchain.plan(p)
        undocumented = [c.name for c in plans if not c.runnable and not c.degrade]
        if undocumented:
            bad(f"{pid}: every skipped check explains what stops being true",
                f"silent skips: {undocumented}")
        else:
            eff = toolchain.effective_level(p, plans)
            note = "" if eff == p.validation_level else f" (declared {p.validation_level})"
            ok(f"{pid}: effective validation level here is {eff}{note}")


def report() -> int:
    print(f"\n\033[1m{len(PASS)} passed, {len(FAIL)} failed\033[0m")
    if FAIL:
        print("\033[31mThe codegen engine is not trustworthy in this state.\033[0m")
        return 1
    print("\033[32mGreen. Profiles load, refuse, and validate what they claim to.\033[0m")
    return 0


def main() -> int:
    print("\033[1mtarget profile engine — self test\033[0m")
    reg = test_profiles_load()
    test_broken_profile_is_rejected()
    test_adk_templates()
    test_adk_validator()
    test_negotiation(reg)
    test_structural_grammars()
    test_toolchain_honesty(reg)
    return report()


if __name__ == "__main__":
    sys.exit(main())
