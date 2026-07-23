#!/usr/bin/env python3
"""
check_cobol_structure.py — the structural gate for generated COBOL.

    python3 check_cobol_structure.py prog.cbl [more.cbl ...]

This is NOT a compiler and never claims to be. It is the grammar that catches the
mistakes a language model makes in COBOL, which are structural rather than
syntactic: divisions in the wrong order, a missing PROCEDURE DIVISION, text past
column 72, STOP RUN in a CICS program, QSAM I/O inside a transaction, COPY with
no matching copybook library.

When GnuCOBOL is installed, `cobc -fsyntax-only` runs alongside this and is
strictly stronger. When it is not — which is the case on the machine this was
written on — this is all the assurance there is, and the profile says so by
declaring validation level structural-only.

Exit: 0 = no errors, 1 = at least one error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

Finding = Tuple[str, int, str]      # (severity, line number, message)

DIVISIONS = ["IDENTIFICATION", "ENVIRONMENT", "DATA", "PROCEDURE"]
DIV_RE = re.compile(r"^\s*(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\s*\.",
                    re.IGNORECASE)
QSAM_VERBS = re.compile(r"^\s*(OPEN|CLOSE|READ|WRITE|REWRITE)\b", re.IGNORECASE)
MAX_COLUMN = 72


def _is_comment(line: str) -> bool:
    # Fixed format: indicator area is column 7 (index 6).
    return len(line) > 6 and line[6] in ("*", "/")


def check_file(path: Path, free_format: bool = False) -> List[Finding]:
    out: List[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [("ERROR", 0, f"cannot read: {exc}")]

    text_upper = "\n".join(lines).upper()
    is_cics = "EXEC CICS" in text_upper

    seen: List[Tuple[str, int]] = []
    for n, line in enumerate(lines, 1):
        if _is_comment(line):
            continue

        if not free_format and len(line.rstrip()) > MAX_COLUMN:
            out.append(("ERROR", n,
                        f"line is {len(line.rstrip())} characters; fixed-format COBOL ignores "
                        f"everything past column {MAX_COLUMN}. The compiler will not complain — "
                        f"it will silently drop the tail, which is worse."))

        m = DIV_RE.match(line)
        if m:
            name = m.group(1).upper()
            seen.append(("IDENTIFICATION" if name == "ID" else name, n))

        if is_cics:
            if re.search(r"\bSTOP\s+RUN\b", line, re.IGNORECASE):
                out.append(("ERROR", n,
                            "STOP RUN in a CICS program. A transaction ends with "
                            "EXEC CICS RETURN; STOP RUN takes down the whole task."))
            if QSAM_VERBS.match(line) and "EXEC CICS" not in line.upper():
                out.append(("ERROR", n,
                            f"'{line.strip().split()[0].upper()}' is QSAM file I/O inside a "
                            f"CICS program. File access goes through EXEC CICS READ/WRITE; "
                            f"this compiles on a distributed COBOL and fails on the LPAR."))

    names = [s[0] for s in seen]
    for required in ("IDENTIFICATION", "PROCEDURE"):
        if required not in names:
            out.append(("ERROR", 0, f"no {required} DIVISION found."))
    ordered = [d for d in DIVISIONS if d in names]
    actual = [n for n in names if n in DIVISIONS]
    if actual != ordered:
        out.append(("ERROR", seen[0][1] if seen else 0,
                    f"divisions are in the order {actual}; COBOL requires "
                    f"{[d for d in DIVISIONS if d in actual]} and rejects any other."))
    for name in set(names):
        if names.count(name) > 1:
            out.append(("ERROR", 0, f"{name} DIVISION appears {names.count(name)} times."))

    if not re.search(r"^\s*PROGRAM-ID\s*\.", "\n".join(lines), re.IGNORECASE | re.MULTILINE):
        out.append(("ERROR", 0, "no PROGRAM-ID — it is the one mandatory statement in the "
                                "IDENTIFICATION DIVISION."))

    if is_cics and "DFHCOMMAREA" not in text_upper:
        out.append(("WARN", 0,
                    "CICS program with no 01 DFHCOMMAREA in the LINKAGE SECTION. If this is "
                    "pseudo-conversational, state is being lost between transactions."))

    copies = re.findall(r"^\s*COPY\s+([A-Z0-9$#@-]+)", "\n".join(lines),
                        re.IGNORECASE | re.MULTILINE)
    if copies:
        jcl = list(path.parent.parent.glob("jcl/*.jcl")) + list(path.parent.glob("*.jcl"))
        if jcl and not any("SYSLIB" in j.read_text(encoding="utf-8", errors="replace").upper()
                           for j in jcl):
            out.append(("ERROR", 0,
                        f"the program COPYs {sorted(set(c.upper() for c in copies))} but no "
                        f"//COBOL.SYSLIB DD appears in the JCL beside it. COPY without SYSLIB "
                        f"produces JCL that fails at compile time, every time."))
        elif not jcl:
            out.append(("WARN", 0,
                        f"the program COPYs {sorted(set(c.upper() for c in copies))}; make sure "
                        f"the JCL carries //COBOL.SYSLIB pointing at the copybook library."))

    if not re.search(r"\bGOBACK\b|\bSTOP\s+RUN\b|EXEC\s+CICS\s+RETURN", text_upper):
        out.append(("ERROR", 0, "no GOBACK, STOP RUN or EXEC CICS RETURN — the program has no "
                                "way to end."))
    return out


def main(argv: List[str]) -> int:
    paths = [Path(a) for a in argv[1:] if not a.startswith("--")]
    free = "--free" in argv
    if not paths:
        print(__doc__)
        return 2
    total_errors = 0
    for path in paths:
        findings = check_file(path, free_format=free)
        errors = [f for f in findings if f[0] == "ERROR"]
        total_errors += len(errors)
        print(f"{path}: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
        for sev, line, msg in findings:
            where = f":{line}" if line else ""
            print(f"  [{sev}] {path.name}{where}: {msg}")
    print("-" * 72)
    print(f"{'INVALID' if total_errors else 'STRUCTURALLY VALID'} "
          f"({total_errors} error(s) across {len(paths)} file(s)). "
          f"Structure only — this is not a compiler.")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
