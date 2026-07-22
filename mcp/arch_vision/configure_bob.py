#!/usr/bin/env python3
"""
configure_bob.py — generate this machine's .bob/mcp.json, verified.

    python3 mcp/arch_vision/configure_bob.py            # write it
    python3 mcp/arch_vision/configure_bob.py --check    # diagnose only
    python3 mcp/arch_vision/configure_bob.py --portable # relative, for Git

WHY THIS SCRIPT EXISTS
----------------------
Writing mcp.json by hand runs into three traps, and all three show up in Bob as
the SAME useless symptom ("it does not appear" or "it does not connect"), with no
traceback:

1. An undocumented key (a comment, for instance). Bob validates the config; a
   stray key silently kills the WHOLE FILE. For STDIO the docs list only:
   command (required), args, cwd, env, alwaysAllow, disabled.

2. venv. If you installed the deps into a .venv and mcp.json says
   command: "python3", that resolves to the SYSTEM python, which does not have
   the deps. The server starts and dies with ModuleNotFoundError.

3. A relative path in `args`. Bob's docs do not specify which directory it
   resolves them from. An absolute path leaves no room for doubt.

This script settles all three: it finds the right interpreter, PROVES that it can
import the dependencies before writing anything, and writes absolute paths.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp" / "arch_vision" / "server.py"
CONFIG = ROOT / ".bob" / "mcp.json"
TOOLS = [
    "arch_vision_list_intake",
    "arch_vision_describe_diagram",
    "arch_vision_extract_architecture",
    "arch_vision_verify_element",
]
DEPS = ("mcp", "httpx", "pydantic")


def candidates() -> list[Path]:
    """Interpreters to test, from the most likely to the least.

    NEVER use .resolve() here. `.venv/bin/python` is a SYMLINK to the system
    python; resolve() follows the link and gives back /usr/bin/python3, which
    cannot see the venv's site-packages. The venv works because you invoke the
    wrapper. Tested: `.venv/bin/python` gives sys.prefix=<venv>; the same path
    after resolve() gives sys.prefix=/usr. Absolutize without resolving.
    """
    out = []
    for rel in (".venv/bin/python", "venv/bin/python",
                ".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        p = ROOT / rel
        if p.exists():
            out.append(p.absolute())               # absolute(), not resolve()
    out.append(Path(sys.executable).absolute())    # the one running this script
    for name in ("python3", "python"):
        w = shutil.which(name)
        if w:
            out.append(Path(w).absolute())
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def can_import(py: Path) -> tuple[bool, str]:
    """Only trust an interpreter that PROVED it can import the server's deps."""
    r = subprocess.run([str(py), "-c", f"import {', '.join(DEPS)}"],
                       capture_output=True, timeout=60)
    if r.returncode == 0:
        return True, ""
    err = r.stderr.decode(errors="replace").strip().splitlines()
    return False, err[-1] if err else "failed with no message"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="diagnose only, do not write")
    ap.add_argument("--portable", action="store_true",
                    help="write relative paths and command='python3' (for Git; "
                         "requires the deps in the system python)")
    a = ap.parse_args()

    if not SERVER.exists():
        print(f"!! server.py not found at {SERVER}")
        print("   Run this from the root of the arch2code project.")
        return 1

    print(f"project root    : {ROOT}")
    print("looking for an interpreter that imports mcp, httpx and pydantic:\n")

    escolhido = None
    for py in candidates():
        ok, err = can_import(py)
        print(f"  {'OK  ' if ok else 'no  '} {py}")
        if not ok:
            print(f"         {err}")
        if ok and escolhido is None:
            escolhido = py

    if escolhido is None:
        print("\n!! No interpreter has the dependencies.\n")
        print("   Pick ONE path:\n")
        print("   A) System python (simple; mcp.json stays shareable in Git):")
        print("      pip install -r mcp/arch_vision/requirements.txt --break-system-packages\n")
        print("   B) venv (isolated; mcp.json ends up with this machine's paths):")
        print("      python3 -m venv .venv")
        print("      .venv/bin/pip install -r mcp/arch_vision/requirements.txt\n")
        print("   Then run this script again.")
        return 1

    print(f"\nchosen: {escolhido}")

    if a.portable:
        cfg_server = {"command": "python3",
                      "args": ["mcp/arch_vision/server.py"]}
        nota = ("--portable mode: relative paths and command='python3'. "
                "Shareable through Git, but it ONLY works if every person has the "
                "deps in their system python (option A).")
    else:
        cfg_server = {"command": str(escolhido), "args": [str(SERVER)]}
        nota = ("absolute paths for this machine: kills the 'which python' doubt and "
                "the 'where does a relative arg resolve from' doubt. To commit it to "
                "Git, run with --portable once it is working.")

    cfg_server.update({"alwaysAllow": TOOLS, "disabled": False})
    cfg = {"mcpServers": {"arch_vision": cfg_server}}
    texto = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"

    if a.check:
        print("\n--check: nothing written. It would look like this:\n")
        print(texto)
        return 0

    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(texto, encoding="utf-8")
    print(f"\nwritten: {CONFIG}")
    print(f"note: {nota}\n")
    print(texto)
    print("Now, in Bob: Settings -> MCP -> click the refresh button (⟳).")
    print("If it does not show up, Settings -> MCP -> Edit Project MCP and see WHICH")
    print("file opens: if it is not this one, the open workspace is not the project root.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
