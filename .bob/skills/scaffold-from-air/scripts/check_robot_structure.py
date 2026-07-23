#!/usr/bin/env python3
"""
check_robot_structure.py — the structural gate for generated Robot Framework suites.

    python3 check_robot_structure.py suite.robot [more.robot ...]

`robot --dryrun` is strictly better than this and should run whenever Robot
Framework is installed: it resolves every keyword without executing anything.
This grammar exists for the machine that does not have it, and it also catches
three things a dry run does not care about but a reviewer does:

  * a selector literal inlined in a step, instead of living in a resource file
  * Sleep with a magic number where a Wait Until keyword belongs
  * something that looks like a credential sitting in a variable

Exit: 0 = no errors, 1 = at least one error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

Finding = Tuple[str, int, str]

VALID_SECTIONS = {
    "SETTINGS", "SETTING", "VARIABLES", "VARIABLE", "TEST CASES", "TEST CASE",
    "TASKS", "TASK", "KEYWORDS", "KEYWORD", "COMMENTS", "COMMENT",
}
SECTION_RE = re.compile(r"^\*+\s*([^*]+?)\s*\**\s*$")
SELECTOR_RE = re.compile(r"((?:xpath:|css:|id:)\S+|//[A-Za-z*\[@]\S*)")
SLEEP_RE = re.compile(r"^\s{2,}Sleep\s+\S", re.IGNORECASE)
SECRET_RE = re.compile(
    r"(?i)^\s*\$\{(pass(word)?|secret|token|api[_-]?key|credential)[^}]*\}\s+\S")


def check_file(path: Path) -> List[Finding]:
    out: List[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [("ERROR", 0, f"cannot read: {exc}")]

    sections: List[str] = []
    current = ""
    is_resource = path.suffix == ".resource"

    for n, line in enumerate(lines, 1):
        if line.startswith("*"):
            m = SECTION_RE.match(line)
            name = (m.group(1).strip().upper() if m else "")
            if name not in VALID_SECTIONS:
                out.append(("ERROR", n,
                            f"'{line.strip()}' is not a Robot Framework section header. "
                            f"Valid: *** Settings ***, *** Variables ***, *** Test Cases ***, "
                            f"*** Tasks ***, *** Keywords ***, *** Comments ***."))
            else:
                sections.append(name)
                current = name
            continue

        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if current in ("TEST CASES", "TEST CASE", "TASKS", "TASK") and line.startswith((" ", "\t")):
            found = SELECTOR_RE.search(line)
            if found:
                out.append(("ERROR", n,
                            f"selector literal '{found.group(0)}' inlined in a step. Selectors "
                            f"belong in a .resource file so a UI change is one edit, not twenty."))
        if SLEEP_RE.match(line):
            out.append(("WARN", n,
                        "Sleep with a fixed delay. Use a Wait Until keyword — a magic sleep is "
                        "how a suite that passes here goes flaky on a slower runner."))
        if current in ("VARIABLES", "VARIABLE") and SECRET_RE.match(line):
            out.append(("ERROR", n,
                        "this looks like a credential in a variable. Robot credentials come "
                        "from the orchestrator's credential store or a vault, never from the "
                        "suite file."))

    if not sections:
        out.append(("ERROR", 0, "no section header at all — an empty or malformed suite."))
    elif not is_resource and not ({"TEST CASES", "TEST CASE", "TASKS", "TASK"} & set(sections)):
        out.append(("ERROR", 0,
                    "no *** Test Cases *** or *** Tasks *** section, so this suite runs nothing. "
                    "If it is meant to be shared keywords, name it .resource."))
    if is_resource and ({"TEST CASES", "TEST CASE", "TASKS", "TASK"} & set(sections)):
        out.append(("ERROR", 0, "a .resource file cannot contain test cases or tasks."))
    return out


def main(argv: List[str]) -> int:
    paths = [Path(a) for a in argv[1:] if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    total = 0
    for path in paths:
        findings = check_file(path)
        errors = [f for f in findings if f[0] == "ERROR"]
        total += len(errors)
        print(f"{path}: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
        for sev, line, msg in findings:
            where = f":{line}" if line else ""
            print(f"  [{sev}] {path.name}{where}: {msg}")
    print("-" * 72)
    print(f"{'INVALID' if total else 'STRUCTURALLY VALID'} ({total} error(s) across "
          f"{len(paths)} file(s)). Run `robot --dryrun` as well when it is installed.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
