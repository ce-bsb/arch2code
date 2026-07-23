#!/usr/bin/env python3
"""
capture_diagram.py — registers and normalizes an inbox artifact for the vision path.

Usage:
    python3 capture_diagram.py --list
    python3 capture_diagram.py .arch/intake/inbox/sketch.jpg --run 20260716-1430-pedidos

What it does:
  1. Decides the extraction path by file type (deterministic vs vision) and WARNS
     if you are about to use vision where a structured source exists.
  2. Fixes EXIF rotation (a phone photo comes in sideways; a vision model reads a
     diagram rotated 90 degrees and produces garbage with high confidence).
  3. Resizes the longest edge down to <= MAX_EDGE, preserving the aspect ratio.
  4. Converts to PNG, computes the sha256 of the ORIGINAL and writes the manifest.

Why normalize before calling the model: a phone photo is 4000px wide, carries EXIF
rotation and compression noise. Sending that raw costs tokens, delivers less and
introduces error that nobody can debug afterwards.

Dependency: Pillow (pip install pillow). pypdf/pdf2image optional, for PDF.
"""

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

MAX_EDGE = 1568  # the useful limit of most multimodal models; above it the provider
                 # rescales anyway and you just pay more

INBOX = Path(".arch/intake/inbox")

VISION_EXT = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
DETERMINISTIC_EXT = {".drawio", ".xml", ".puml", ".plantuml", ".mmd", ".mermaid",
                     ".md", ".json", ".yaml", ".yml"}
PDF_EXT = {".pdf"}

KIND_BY_EXT = {**{e: "screenshot" for e in VISION_EXT},
               ".drawio": "drawio", ".xml": "drawio",
               ".puml": "plantuml", ".plantuml": "plantuml",
               ".mmd": "mermaid", ".mermaid": "mermaid",
               ".md": "prose", ".json": "prose", ".yaml": "prose", ".yml": "prose",
               ".pdf": "pdf"}


def route(path: Path) -> Dict[str, str]:
    """Decides the extraction path. It is the most consequential decision of stage 1."""
    ext = path.suffix.lower()
    if ext in DETERMINISTIC_EXT:
        return {"path": "deterministic", "kind": KIND_BY_EXT.get(ext, "prose"),
                "tool": "parse_drawio.py" if ext in {".drawio", ".xml"} else "read_file"}
    if ext in PDF_EXT:
        return {"path": "hybrid", "kind": "pdf",
                "tool": "read_file (try text first; vision only if it is a pure image)"}
    if ext in VISION_EXT:
        return {"path": "vision", "kind": "screenshot",
                "tool": "arch_vision_extract_architecture (via use_mcp_tool)"}
    return {"path": "unknown", "kind": "prose", "tool": "ask the human"}


def sibling_structured(path: Path) -> List[Path]:
    """Is there a structured source for the same drawing? If so, vision is a waste."""
    return [p for p in path.parent.glob(f"{path.stem}.*")
            if p != path and p.suffix.lower() in DETERMINISTIC_EXT]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_image(src: Path, dest: Path) -> Dict[str, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("ERROR: Pillow is missing. Run: pip install pillow", file=sys.stderr)
        raise SystemExit(1)

    with Image.open(src) as im:
        original = {"width": im.width, "height": im.height, "mode": im.mode,
                    "format": im.format}
        im = ImageOps.exif_transpose(im)          # a phone photo comes in sideways
        rotated = (im.width, im.height) != (original["width"], original["height"])
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        scale = 1.0
        if max(im.size) > MAX_EDGE:
            scale = MAX_EDGE / max(im.size)
            im = im.resize((round(im.width * scale), round(im.height * scale)),
                           Image.LANCZOS)

        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "PNG", optimize=True)
        return {"original": original, "normalized": {"width": im.width, "height": im.height},
                "exif_rotation_applied": rotated, "scale": round(scale, 4)}


def cmd_list() -> int:
    if not INBOX.exists():
        print(f"{INBOX} does not exist. Create it and drop the drawing there.")
        return 1
    items = sorted(p for p in INBOX.iterdir() if p.is_file() and not p.name.startswith("."))
    if not items:
        print(f"{INBOX} is empty. See the 'Capture' section of the README for the "
              f"ways to put a drawing here.")
        return 0
    print(f"{len(items)} artifact(s) in {INBOX}:\n")
    for p in items:
        r = route(p)
        kb = p.stat().st_size / 1024
        print(f"  {p.name}")
        print(f"    {kb:8.1f} KB   kind={r['kind']:<11} path={r['path']}")
        print(f"    -> {r['tool']}")
        if r["path"] == "vision" and (sib := sibling_structured(p)):
            print(f"    !! a structured source exists: {[s.name for s in sib]} — use it")
        print()
    return 0


def cmd_capture(src: Path, run_id: str) -> int:
    if not src.exists():
        print(f"ERROR: {src} does not exist")
        return 1

    r = route(src)
    out_dir = Path(".arch/intake") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "source_artifact": str(src),
        "source_sha256": sha256(src),
        "source_kind": r["kind"],
        "extraction_path": r["path"],
        "next_tool": r["tool"],
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "bytes": src.stat().st_size,
        "warnings": [],
    }

    if sib := sibling_structured(src):
        manifest["structured_sibling"] = [str(s) for s in sib]
        manifest["warnings"].append(
            f"A structured source exists ({[s.name for s in sib]}). Use the "
            f"deterministic path: 100% fidelity, no hallucination, no token cost.")

    if r["path"] == "vision":
        norm = out_dir / f"{src.stem}.normalized.png"
        manifest["normalization"] = normalize_image(src, norm)
        manifest["normalized_artifact"] = str(norm)
        n = manifest["normalization"]["normalized"]
        if min(n["width"], n["height"]) < 480:
            manifest["warnings"].append(
                f"Small image ({n['width']}x{n['height']}): labels may be illegible. "
                f"Extracting badly is worse than not extracting at all — ask for another photo.")
        if r["kind"] in ("screenshot",) and manifest["normalization"]["scale"] < 0.35:
            manifest["warnings"].append(
                "Heavy downscale applied: small text may have turned into a blur. "
                "If the labels matter, crop by region and capture in parts.")
    else:
        copy = out_dir / src.name
        shutil.copy2(src, copy)
        manifest["working_copy"] = str(copy)

    mpath = out_dir / "capture-manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"run    : {run_id}")
    print(f"source : {src}  ({manifest['bytes'] / 1024:.1f} KB)")
    print(f"sha256 : {manifest['source_sha256'][:16]}...")
    print(f"kind   : {manifest['source_kind']}   path: {manifest['extraction_path']}")
    if "normalization" in manifest:
        o, n = manifest["normalization"]["original"], manifest["normalization"]["normalized"]
        rot = " (EXIF rotation corrected)" if manifest["normalization"]["exif_rotation_applied"] else ""
        print(f"image  : {o['width']}x{o['height']} -> {n['width']}x{n['height']}{rot}")
        print(f"ready  : {manifest['normalized_artifact']}")
    print(f"next   : {manifest['next_tool']}")
    for w in manifest["warnings"]:
        print(f"WARN   : {w}")
    print(f"\nmanifest: {mpath}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("artifact", nargs="?", help="path to the artifact in the inbox")
    ap.add_argument("--run", help="run id, format YYYYMMDD-HHMM-<slug>")
    ap.add_argument("--list", action="store_true", help="list the inbox and the routing")
    a = ap.parse_args()

    if a.list or not a.artifact:
        return cmd_list()
    run = a.run or datetime.now().strftime("%Y%m%d-%H%M-adhoc")
    return cmd_capture(Path(a.artifact), run)


if __name__ == "__main__":
    sys.exit(main())
