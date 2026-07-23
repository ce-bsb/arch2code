#!/usr/bin/env python3
"""
check_jcl_structure.py — the structural gate for generated JCL.

    python3 check_jcl_structure.py job.jcl [more.jcl ...]

There is no open-source JCL linter to defer to: the parsing libraries that exist
do not validate, and everything that does is commercial. So this is a grammar
written here, covering the rules that are cheap to check and expensive to get
wrong on somebody else's system:

  * //JOB card present and first
  * step names <= 8 characters, DSNs <= 44
  * nothing past column 72, continuation is a non-blank in column 72
  * every EXEC PGM= has the DD statements its program needs
  * COPY in the paired COBOL means //COBOL.SYSLIB must exist here

Exit: 0 = no errors, 1 = at least one error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

Finding = Tuple[str, int, str]

MAX_COLUMN = 72
NAME_RE = re.compile(r"^//([A-Z@#$][A-Z0-9@#$]{0,7})?\s+(JOB|EXEC|DD|PROC|PEND|SET|INCLUDE)\b")
STMT_RE = re.compile(r"^//([A-Z@#$][A-Z0-9@#$]*)?\s+(\S+)")
DSN_RE = re.compile(r"DSN=([A-Z0-9@#$.()+\-]+)", re.IGNORECASE)


def check_file(path: Path) -> List[Finding]:
    out: List[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [("ERROR", 0, f"cannot read: {exc}")]

    saw_job = False
    steps: List[str] = []
    dds: List[str] = []

    for n, line in enumerate(lines, 1):
        if line.startswith("//*") or not line.strip():
            continue

        if len(line.rstrip()) > MAX_COLUMN:
            out.append(("ERROR", n,
                        f"line is {len(line.rstrip())} characters. JCL stops at column "
                        f"{MAX_COLUMN}; continue with a non-blank in column 72 and resume the "
                        f"text in column 16."))

        if not line.startswith("//") and not line.startswith("/*"):
            out.append(("ERROR", n, "statement does not start with // — JCL statements begin in "
                                    "columns 1-2, and in-stream data ends with /*."))
            continue

        m = STMT_RE.match(line)
        if not m:
            continue
        label, verb = m.group(1) or "", m.group(2).upper()

        if verb == "JOB":
            if saw_job:
                out.append(("ERROR", n, "second JOB card in the same file."))
            elif steps or dds:
                out.append(("ERROR", n, "JOB card appears after an EXEC or DD; it has to be first."))
            saw_job = True
            if not label:
                out.append(("ERROR", n, "JOB card has no job name."))
        elif verb == "EXEC":
            steps.append(label)
            if label and len(label) > 8:
                out.append(("ERROR", n, f"step name '{label}' is {len(label)} characters; the "
                                        f"limit is 8."))
        elif verb == "DD":
            dds.append(label)
            if label and len(label) > 8:
                out.append(("ERROR", n, f"DDNAME '{label}' is {len(label)} characters; the "
                                        f"limit is 8."))

        for dsn in DSN_RE.findall(line):
            bare = dsn.split("(")[0]
            if len(bare) > 44:
                out.append(("ERROR", n, f"DSN '{bare}' is {len(bare)} characters; the limit is 44."))
            for qualifier in bare.split("."):
                if qualifier and len(qualifier) > 8:
                    out.append(("ERROR", n, f"DSN qualifier '{qualifier}' is longer than 8 "
                                            f"characters."))

        if line.count("(") != line.count(")") and not line.rstrip().endswith(","):
            out.append(("WARN", n, "unbalanced parentheses on a line that does not continue — "
                                   "check the PARM or DCB."))

    if not saw_job:
        out.append(("ERROR", 0, "no //... JOB card. The interpreter rejects the file before "
                                "reaching anything useful."))
    if not steps:
        out.append(("ERROR", 0, "no EXEC statement — this JCL runs nothing."))

    text = "\n".join(lines).upper()
    cobol_nearby = list(path.parent.parent.glob("cobol/*.cbl")) + list(path.parent.glob("*.cbl"))
    uses_copy = any(
        re.search(r"^\s*COPY\s+\S", c.read_text(encoding="utf-8", errors="replace"),
                  re.IGNORECASE | re.MULTILINE)
        for c in cobol_nearby
    )
    if uses_copy and "SYSLIB" not in text:
        out.append(("ERROR", 0,
                    "a COBOL program beside this JCL uses COPY, but there is no //COBOL.SYSLIB "
                    "DD here. This is the single most common defect in generated JCL: it fails "
                    "at compile time on the client's system, not on yours."))

    if "IGYWCL" in text or "IGYWCLG" in text or "DFHYITVL" in text or "DFHZITCL" in text:
        if "SYSIN" not in text and "COBOL.SYSIN" not in text:
            out.append(("WARN", 0, "a compile procedure is invoked with no //COBOL.SYSIN DD — "
                                   "the compiler has no source to read."))
    if "DFHYITVL" in text or "DFHZITCL" in text:
        if "RENT" not in text or "NODYNAM" not in text:
            out.append(("ERROR", 0,
                        "CICS compile procedure without RENT and NODYNAM in the compiler "
                        "options. Both are mandatory for CICS."))
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
          f"{len(paths)} file(s)). No JCL linter exists offline — this grammar is the gate.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
