#!/usr/bin/env python3
"""
run_image.py — run ONE architecture image through the MCP server, for real.

    python3 mcp/arch_vision/run_image.py <image-path>
    python3 mcp/arch_vision/run_image.py ~/Desktop/architecture.png --hint "payment flow"
    python3 mcp/arch_vision/run_image.py photo.jpg --kind napkin --out extraction.json

WHAT THIS COMMAND VALIDATES
---------------------------
It does NOT import server.py. It starts the server as a subprocess and speaks MCP
over stdio — the same path Bob uses. And it reads command/args from YOUR
.bob/mcp.json, so it also validates the config Bob is going to use.

If this command works, it works in Bob. If it fails, it fails in Bob too — except
that here you get to see the traceback.

Chain validated end to end:
  .bob/mcp.json -> subprocess -> MCP handshake -> list_tools -> call_tool
  -> image normalization -> watsonx.ai -> structured JSON -> second pass
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".bob" / "mcp.json"


def _reexec_no_interpretador_certo() -> None:
    """If the shell's python lacks the SDK, re-exec with the one from mcp.json.

    WHY: your shell interpreter (conda base, pyenv, whatever) has nothing to do
    with the one Bob uses to start the server. configure_bob.py already found one
    that PROVED it can import the deps and wrote it into mcp.json. Reusing that one
    beats making the user guess which of the machine's pythons to install into —
    a dev laptop has about four, and `pip install` always hits the wrong one.
    """
    import json as _json
    import os as _os

    try:
        import mcp  # noqa: F401
        return                                   # the current python already works
    except ImportError:
        pass

    if _os.environ.get("_ARCH2CODE_REEXEC"):     # guard against an infinite loop
        print("!! Neither this shell's python nor the one in mcp.json has the MCP SDK.")
        print("   Run: python3 mcp/arch_vision/configure_bob.py")
        sys.exit(1)

    if not CONFIG.exists():
        print("!! MCP SDK not installed in this python, and .bob/mcp.json does not exist.")
        print(f"   current python: {sys.executable}")
        print("   Run this first: python3 mcp/arch_vision/configure_bob.py")
        sys.exit(1)

    try:
        cmd = _json.loads(CONFIG.read_text())["mcpServers"]["arch_vision"]["command"]
    except Exception:
        print("!! .bob/mcp.json is unreadable. Run: python3 mcp/arch_vision/configure_bob.py")
        sys.exit(1)

    if Path(cmd).name.startswith("python") and Path(cmd).is_absolute() and Path(cmd).exists():
        print(f"(this shell uses {sys.executable}, which does not have the MCP SDK;")
        print(f" re-executing with the interpreter from mcp.json: {cmd})\n")
        _os.environ["_ARCH2CODE_REEXEC"] = "1"
        _os.execv(cmd, [cmd, str(Path(__file__).resolve()), *sys.argv[1:]])

    print("!! MCP SDK not installed in this python.")
    print(f"   this shell's python : {sys.executable}")
    print(f"   mcp.json command    : {cmd}")
    print(f"   Install it in this shell's python:")
    print(f"     {sys.executable} -m pip install -r mcp/arch_vision/requirements.txt")
    sys.exit(1)


_reexec_no_interpretador_certo()

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def carrega_config() -> StdioServerParameters:
    """Reads the same mcp.json Bob reads. If it is wrong here, it is wrong there."""
    if not CONFIG.exists():
        print(f"!! {CONFIG} does not exist. Run: python3 mcp/arch_vision/configure_bob.py")
        sys.exit(1)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    srv = cfg.get("mcpServers", {}).get("arch_vision")
    if not srv:
        print("!! 'arch_vision' is not in mcp.json")
        sys.exit(1)
    print(f"config : {CONFIG}")
    print(f"command: {srv['command']} {' '.join(srv.get('args', []))}")
    return StdioServerParameters(
        command=srv["command"],
        args=srv.get("args", []),
        env=srv.get("env"),
        cwd=srv.get("cwd") or str(ROOT),
    )


async def roda(img: Path, kind: str, hint: str | None, out: Path | None) -> int:
    params = carrega_config()
    print(f"image  : {img}\n")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as sessao:
            await sessao.initialize()
            print("✓ MCP handshake (the server started and spoke the protocol)")

            tools = {t.name for t in (await sessao.list_tools()).tools}
            print(f"✓ {len(tools)} tools: {', '.join(sorted(tools))}\n")

            args = {"image_path": str(img), "source_kind": kind}
            if hint:
                args["hint"] = hint
            print(f"calling arch_vision_extract_architecture (source_kind={kind})...")
            r = await sessao.call_tool("arch_vision_extract_architecture", args)

            # If the tool lets an exception escape, MCP returns isError with raw
            # text — not JSON. Parsing it straight away would hide the real cause
            # behind a JSONDecodeError. Show whatever came back.
            bruto = r.content[0].text if r.content else ""
            if getattr(r, "isError", False):
                print(f"\n✗ the tool failed on the server:\n{bruto}\n")
                return 1
            try:
                data = json.loads(bruto)
            except json.JSONDecodeError:
                print(f"\n✗ the server did not return JSON:\n{bruto[:600]}\n")
                return 1

            if "error" in data:
                print(f"\n✗ {data['error']}\n")
                return 1

            comps = data.get("components", [])
            conns = data.get("connections", [])
            unks = data.get("unknowns", [])
            print(f"\n✓ extracted: {len(comps)} components, {len(conns)} connections, "
                  f"{len(unks)} unknowns  (confidence {data.get('overall_confidence')})\n")

            print("COMPONENTS")
            for c in comps:
                t = f" [{c['tech']}]" if c.get("tech") else ""
                print(f"  {c.get('name','?'):26s} {c.get('kind','?'):10s}{t:14s} "
                      f"conf={c.get('confidence')}")

            print("\nCONNECTIONS")
            for c in conns:
                lab = f'  "{c["label"]}"' if c.get("label") else ""
                print(f"  {c.get('from','?'):18s} -> {c.get('to','?'):18s} "
                      f"[{c.get('protocol')}/{c.get('sync')}] conf={c.get('confidence')}{lab}")

            if unks:
                print("\nUNKNOWNS (what the drawing does NOT say — this is what matters most)")
                for u in unks:
                    b = "BLOCKING" if u.get("blocking") else "optional"
                    print(f"  [{b}] {u.get('question')}")
                    if u.get("options"):
                        print(f"           options: {u['options']}")
            else:
                print("\n!! ZERO unknowns.")
                print("   Be suspicious: a real architecture drawing almost always has")
                print("   ambiguity. Zero unknowns usually means the model completed")
                print("   the drawing instead of reporting what it saw.")

            # Second pass: what arch-critic does in stage 3. A different prompt and
            # a different framing -> error decorrelated from the extraction.
            duvidosas = data.get("_quality", {}).get("connections_needing_verification", [])
            if duvidosas:
                print(f"\nSECOND PASS ({len(duvidosas)} connection(s) with conf<0.85)")
                for cid in duvidosas:
                    c = next((x for x in conns if x.get("id") == cid), None)
                    if not c:
                        continue
                    claim = (f"is there an arrow from '{c.get('from')}' to "
                             f"'{c.get('to')}' in this diagram")
                    v = await sessao.call_tool("arch_vision_verify_element",
                                               {"image_path": str(img), "claim": claim})
                    vd = json.loads(v.content[0].text)
                    marca = {"true": "confirms", "false": "CONTRADICTS",
                             "uncertain": "uncertain"}.get(vd.get("verdict"), "?")
                    print(f"  {marca:11s} {cid}: {vd.get('observed','')[:70]}")
                    if vd.get("verdict") in ("false", "uncertain"):
                        print(f"             -> becomes an unknown, does NOT enter the AIR as fact")

            if out:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                               encoding="utf-8")
                print(f"\nwritten: {out}")

    print("\n" + "=" * 70)
    print("If you got this far, the whole chain works:")
    print("  mcp.json -> subprocess -> MCP -> watsonx.ai -> structured JSON")
    print("Bob uses exactly this path.")
    print("=" * 70)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Runs an image through the arch_vision MCP server")
    ap.add_argument("imagem", help="image path (png/jpg/webp)")
    ap.add_argument("--kind", default="napkin",
                    choices=["napkin", "whiteboard", "screenshot", "pdf"],
                    help="nature of the artifact; napkin/whiteboard turn on extra skepticism")
    ap.add_argument("--hint", default=None,
                    help="context that helps the reading, e.g.: 'payment flow'")
    ap.add_argument("--out", default=None, type=Path, help="write the extracted JSON")
    a = ap.parse_args()

    img = Path(a.imagem).expanduser()
    if not img.is_absolute():
        img = (Path.cwd() / img).resolve()

    if not img.exists():
        print(f"!! not found: {img}")
        # '/diagrams/x.png' points at the SYSTEM ROOT. Almost always the person
        # meant 'diagrams/x.png', inside the project. Suggest instead of just refusing.
        alts, vistos = [], set()
        crua = a.imagem.lstrip("/")
        for base in (Path.cwd(), ROOT):            # usually the same one: dedup
            c = (base / crua).resolve()
            if c.exists() and str(c) not in vistos:
                vistos.add(str(c))
                alts.append(c)
        if alts:
            print(f"\n   You wrote '{a.imagem}', with a leading slash: that is the SYSTEM")
            print("   root, not the project folder. I found the file here:")
            for c in alts:
                print(f"     {c}")
            print(f"\n   Run:   python3 mcp/arch_vision/run_image.py {crua}")
        else:
            nome = Path(a.imagem).name
            achados = [p for p in ROOT.rglob(nome) if p.is_file()][:5]
            if achados:
                print(f"\n   I found '{nome}' in:")
                for c in achados:
                    print(f"     {c.relative_to(ROOT)}")
        return 1

    return asyncio.run(roda(img, a.kind, a.hint, a.out))


if __name__ == "__main__":
    sys.exit(main())
