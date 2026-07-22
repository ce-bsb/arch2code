#!/usr/bin/env python3
"""
Vendor Carbon + IBM Plex into webapp/static/vendor/ and regenerate
webapp/static/css/tokens.css.

Why this exists
---------------
The app must boot with one command and work offline: no CDN, no npm at
runtime, no build step. So the network is used exactly once — here, by a
human or by the Docker "assets" stage — and everything it produces is
committed to the repository and served from disk.

Two things this script is responsible for:

1.  `@carbon/styles` publishes a pre-compiled `css/styles.min.css`. It is
    compiled with `$use-akamai-cdn: true`, which embeds 105 `@font-face`
    blocks pointing at `https://1.www.s81c.com/...` (Arabic, Hebrew,
    Devanagari, Thai, Serif, every italic, every split). Inside a container
    with no egress those become 105 hanging requests and a FOUT. Every one
    of them is stripped and replaced by four local faces.

2.  The theme tokens are *extracted* from that same file rather than typed
    by hand. Hand-transcribed Carbon palettes get `--cds-focus` wrong in
    g100 (it is #ffffff there, not blue) and the dark-theme focus ring
    disappears. Extracting makes that class of bug impossible.

Usage
-----
    python3 webapp/tools/vendor_assets.py            # download + build
    python3 webapp/tools/vendor_assets.py --verify   # check, write nothing

Requires `npm` on PATH and network access. Neither is needed at runtime.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Pinned. The pre-compiled css/styles.min.css is published but NOT documented
# by IBM (the package README says Dart Sass is required), so it can disappear
# or change shape in a minor. Pin hard, vendor the output, never resolve at
# runtime.
CARBON_STYLES = "@carbon/styles@1.111.0"
PLEX_SANS = "@ibm/plex-sans@1.1.0"
PLEX_MONO = "@ibm/plex-mono@1.1.0"

# "complete" rather than the split Latin1 subsets: +159 KB buys zero risk of a
# missing glyph in a component name read off a user's whiteboard photo.
PLEX_FACES = [
    # (package dir, file stem, css family, weight)
    ("ibm-plex-sans", "IBMPlexSans-Light", "IBM Plex Sans", 300),
    ("ibm-plex-sans", "IBMPlexSans-Regular", "IBM Plex Sans", 400),
    ("ibm-plex-sans", "IBMPlexSans-SemiBold", "IBM Plex Sans", 600),
    ("ibm-plex-mono", "IBMPlexMono-Regular", "IBM Plex Mono", 400),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "webapp" / "static"
VENDOR = STATIC / "vendor"
TOKENS_CSS = STATIC / "css" / "tokens.css"

FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}")
CDN_HOST = "1.www.s81c.com"

# Theme-aware application tokens Carbon does not provide. Only add something
# here when no --cds-* token can do the job, and say why.
#
# The amber exists because Carbon's caution tokens are FILL colours meant to sit
# behind a black glyph: --cds-support-warning (#f1c21b) as text on white is
# 1.68:1 and --cds-support-caution-major (#ff832b) is 2.46:1, both far under the
# 4.5:1 AA floor. These two are IBM orange-70 / orange-30 from the same Carbon
# palette, measured at ~9:1 in light and ~8.8:1 in dark.
EXTRA_THEMED: dict[str, tuple[str, str]] = {
    "--a2c-amber": ("#8a3800", "#ffb784"),
}

# Groups for the generated tokens.css, in the order they are emitted. Anything
# that matches no prefix lands in the trailing "component" group.
TOKEN_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Surfaces", ("--cds-background", "--cds-layer", "--cds-field")),
    ("Text", ("--cds-text",)),
    ("Icons", ("--cds-icon",)),
    ("Borders", ("--cds-border",)),
    ("Links", ("--cds-link",)),
    ("Interaction and focus", ("--cds-focus", "--cds-interactive", "--cds-highlight",
                               "--cds-overlay", "--cds-shadow", "--cds-toggle")),
    ("Status", ("--cds-support",)),
    ("Buttons", ("--cds-button",)),
    ("Tags", ("--cds-tag",)),
    ("Notifications", ("--cds-notification",)),
    ("Skeleton", ("--cds-skeleton",)),
    ("Application (not from Carbon — see EXTRA_THEMED)", ("--a2c-",)),
]


def fail(message: str) -> "None":
    """Exit loudly with something the reader can act on."""
    print(f"vendor_assets: {message}", file=sys.stderr)
    raise SystemExit(1)


def npm_pack(work: Path) -> None:
    if shutil.which("npm") is None:
        fail(
            "npm is not on PATH. This script is the only step that needs the "
            "network; install Node 20+ and rerun, or copy webapp/static/vendor/ "
            "from a machine that has it."
        )
    print(f"downloading {CARBON_STYLES} {PLEX_SANS} {PLEX_MONO} ...")
    result = subprocess.run(
        ["npm", "pack", CARBON_STYLES, PLEX_SANS, PLEX_MONO],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            "npm pack failed (exit "
            f"{result.returncode}). Check network/proxy and the pinned "
            f"versions above.\n--- npm stderr ---\n{result.stderr.strip()}"
        )
    tarballs = sorted(work.glob("*.tgz"))
    if len(tarballs) != 3:
        fail(f"expected 3 tarballs from npm pack, got {len(tarballs)}: "
             f"{[t.name for t in tarballs]}")
    for tgz in tarballs:
        # npm flattens the scope: @carbon/styles -> carbon-styles-1.111.0.tgz.
        name = re.sub(r"-\d+\.\d+\.\d+.*\.tgz$", "", tgz.name)
        dest = work / "x" / name
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz) as tar:
            _safe_extract(tar, dest)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract refusing any member that escapes `dest` (CVE-2007-4559)."""
    root = dest.resolve()
    for member in tar.getmembers():
        target = (root / member.name).resolve()
        if not str(target).startswith(str(root)):
            fail(f"tarball member escapes the extraction root: {member.name!r}")
    tar.extractall(dest)  # noqa: S202 - members validated above


def build_carbon_css(work: Path) -> tuple[str, int, int]:
    src = work / "x" / "carbon-styles" / "package" / "css" / "styles.min.css"
    if not src.exists():
        fail(
            f"{src} is missing. {CARBON_STYLES} no longer ships the "
            "pre-compiled css/styles.min.css; either pin an older version or "
            "drop Carbon and rely on webapp/static/css/tokens.css alone."
        )
    css = src.read_text(encoding="utf-8")
    blocks = FONT_FACE_RE.findall(css)
    stripped = FONT_FACE_RE.sub("", css)
    if CDN_HOST in stripped:
        fail(
            f"{CDN_HOST} still appears outside @font-face after stripping. "
            "The upstream CSS changed shape; inspect it before shipping — the "
            "container has no egress and every such URL is a hung request."
        )
    header = (
        "/*\n"
        f" * Carbon Design System — from {CARBON_STYLES}, css/styles.min.css.\n"
        " * Generated by webapp/tools/vendor_assets.py. Do not edit by hand.\n"
        " *\n"
        f" * {len(blocks)} @font-face blocks ({sum(map(len, blocks))} bytes) that\n"
        f" * pointed at https://{CDN_HOST}/ have been removed; the four local\n"
        " * faces are declared in ../plex/plex.css instead.\n"
        " *\n"
        " * OPTIONAL. The app's own stylesheet (css/tokens.css + css/app.css)\n"
        " * does not need this file. Link it only if you want real cds--*\n"
        " * component markup, and read webapp/static/css/README.md first.\n"
        " *\n"
        " * Apache-2.0, (c) IBM Corp. See LICENSE next to this file.\n"
        " */\n"
    )
    return header + stripped, len(blocks), sum(map(len, blocks))


def build_plex_css() -> str:
    lines = [
        "/*",
        " * IBM Plex, self-hosted. Generated by webapp/tools/vendor_assets.py.",
        " *",
        " * Four faces, woff2 only, 'complete' (not split) so no glyph in a",
        " * diagram label can go missing. font-display: swap so a slow disk read",
        " * never blanks the UI. Paths are absolute because webapp/static is",
        " * mounted at / by FastAPI's StaticFiles.",
        " *",
        " * SIL Open Font License 1.1, (c) IBM Corp. See LICENSE-plex.txt.",
        " */",
        "",
    ]
    for _pkg, stem, family, weight in PLEX_FACES:
        lines += [
            "@font-face {",
            f"  font-family: '{family}';",
            "  font-style: normal;",
            f"  font-weight: {weight};",
            "  font-display: swap;",
            f"  src: url('/vendor/plex/{stem}.woff2') format('woff2');",
            "}",
            "",
        ]
    return "\n".join(lines)


def extract_theme(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\{(--cds-[^}]*)\}", css)
    if match is None:
        fail(
            f"could not find the {selector} token block in the Carbon CSS. "
            "Upstream changed; tokens.css cannot be generated safely."
        )
    return {
        k: v.strip()
        for k, v in re.findall(r"(--cds-[a-z0-9-]+)\s*:\s*([^;]*)", match.group(1))
    }


def group_tokens(names: list[str]) -> list[tuple[str, list[str]]]:
    remaining = set(names)
    out: list[tuple[str, list[str]]] = []
    for label, prefixes in TOKEN_GROUPS:
        picked = sorted(n for n in remaining if n.startswith(prefixes))
        if picked:
            out.append((label, picked))
            remaining -= set(picked)
    if remaining:
        out.append(("Component-specific", sorted(remaining)))
    return out


def render_block(indent: str, tokens: dict[str, str], names: list[str],
                 with_groups: bool) -> str:
    lines: list[str] = []
    if with_groups:
        for label, picked in group_tokens(names):
            lines.append(f"{indent}/* {label} */")
            lines += [f"{indent}{n}: {tokens[n]};" for n in picked]
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
    else:
        lines = [f"{indent}{n}: {tokens[n]};" for n in names]
    return "\n".join(lines)


def build_tokens_css(work: Path) -> tuple[str, int, int]:
    src = work / "x" / "carbon-styles" / "package" / "css" / "styles.min.css"
    css = src.read_text(encoding="utf-8")
    g10 = extract_theme(css, ".cds--g10")
    g100 = extract_theme(css, ".cds--g100")

    # g10 carries one token g100 does not (--cds-notification-action-hover).
    # Emitting it only in the light block would leave dark inheriting a light
    # value, so mirror it from the closest g100 equivalent instead of guessing.
    for missing in sorted(set(g10) - set(g100)):
        g100[missing] = g100.get("--cds-link-primary", g10[missing])

    for name, (light, dark) in EXTRA_THEMED.items():
        g10[name] = light
        g100[name] = dark

    names = sorted(g10)
    dark_names = [n for n in names if n in g100]

    head = f'''/*
 * arch2code — theme tokens.
 *
 * The IBM Carbon g10 (light) and g100 (dark) semantic palettes, extracted
 * verbatim from {CARBON_STYLES} css/styles.min.css by
 * webapp/tools/vendor_assets.py. DO NOT EDIT BY HAND — rerun the script.
 *
 * Contract for everything else in webapp/static/css:
 *
 *   - Colour is ALWAYS var(--cds-*) or var(--a2c-*). A raw hex value anywhere
 *     outside this file is a bug: it will be wrong in one of the two themes.
 *   - Light is the default. Dark applies when the OS asks for it, unless the
 *     user has explicitly pinned a theme.
 *   - The pin is data-theme on <html>: data-theme="dark" or data-theme="light".
 *     It beats the OS in both directions. Absent = follow the OS.
 *   - color-scheme is set alongside so native scrollbars, form controls and
 *     the canvas match, with no flash of white on load.
 *
 * Cascade order (later wins, all three have specificity 0-1-1 or lower):
 *   :root                                        -> g10
 *   @media dark  :root:not([data-theme=light])   -> g100
 *   :root[data-theme=dark]                       -> g100
 *
 * Note for anyone mixing in real Carbon markup: vendor/carbon/carbon.css
 * defines the same tokens under .cds--g10 / .cds--g100 CLASS selectors, which
 * outrank :root inside their subtree. Put those classes on <body> only if you
 * intend to override this file for that subtree.
 *
 * {len(names)} tokens in g10, {len(dark_names)} in g100.
 */

:root {{
  color-scheme: light dark;

{render_block("  ", g10, names, with_groups=True)}
}}

/* Follow the OS, unless the user pinned light. */
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;

{render_block("    ", g100, dark_names, with_groups=False)}
  }}
}}

/* Explicit pin. Beats the OS in both directions. */
:root[data-theme="light"] {{
  color-scheme: light;
}}

:root[data-theme="dark"] {{
  color-scheme: dark;

{render_block("  ", g100, dark_names, with_groups=False)}
}}

/* ---------------------------------------------------------------------------
 * Application layer. Carbon has no semantic token for "a bounding box drawn
 * over a whiteboard photo" or "low model confidence", so those are defined
 * here in terms of Carbon tokens and switched by the same rules above.
 * ------------------------------------------------------------------------- */

:root {{
  /* Type. Plex is vendored; the stacks below are the offline fallbacks. */
  --a2c-font-sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  --a2c-font-mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo,
    Consolas, "Liberation Mono", monospace;

  /* Carbon type set, the sizes an application chrome actually uses. */
  --a2c-type-caption: 0.75rem;   /* 12px - labels, metadata, table cells   */
  --a2c-type-body-sm: 0.875rem;  /* 14px - the app's default body size     */
  --a2c-type-body: 1rem;         /* 16px - long prose only                 */
  --a2c-type-heading-sm: 1rem;
  --a2c-type-heading: 1.25rem;
  --a2c-type-heading-lg: 1.75rem;
  --a2c-type-display: clamp(1.75rem, 1.2rem + 1.6vw, 2.625rem);

  --a2c-lh-tight: 1.25;
  --a2c-lh-body: 1.5;
  --a2c-lh-prose: 1.6;

  /* Weights. Light for display and numbers, regular for body, semibold for
     the few places that must not be missed. Nothing is ever bolder than 600. */
  --a2c-weight-light: 300;
  --a2c-weight-regular: 400;
  --a2c-weight-label: 500;
  --a2c-weight-strong: 600;

  --a2c-label-tracking: 0.02em;

  /* Carbon spacing scale, 01..09. */
  --a2c-space-01: 0.125rem;
  --a2c-space-02: 0.25rem;
  --a2c-space-03: 0.5rem;
  --a2c-space-04: 0.75rem;
  --a2c-space-05: 1rem;
  --a2c-space-06: 1.5rem;
  --a2c-space-07: 2rem;
  --a2c-space-08: 2.5rem;
  --a2c-space-09: 3rem;

  /* Carbon is square. These stay small on purpose; 2px reads as "software",
     12px reads as "consumer app". */
  --a2c-radius: 2px;
  --a2c-radius-lg: 4px;

  /* Carbon motion, productive. Anything slower feels broken in a tool that
     streams subprocess output. */
  --a2c-ease-productive: cubic-bezier(0.2, 0, 0.38, 0.9);
  --a2c-ease-entrance: cubic-bezier(0, 0, 0.38, 0.9);
  --a2c-ease-exit: cubic-bezier(0.2, 0, 1, 0.9);
  --a2c-dur-fast: 70ms;
  --a2c-dur: 110ms;
  --a2c-dur-slow: 240ms;

  /* Status as TEXT.
     Carbon's --cds-support-* are icon and fill colours. Used as text they fail
     WCAG AA on a light layer: success 3.35:1, warning 1.68:1, caution 2.46:1.
     These four are the text-safe equivalents, above 5:1 in both themes.
     Rule: --cds-support-* colours shapes, --a2c-text-* colours words. */
  --a2c-text-success: var(--cds-tag-color-green);
  --a2c-text-info: var(--cds-link-primary);
  --a2c-text-caution: var(--a2c-amber);
  --a2c-text-danger: var(--cds-text-error);

  /* Model confidence, as a graphical mark (box strokes, legend swatches).
     Deliberately NOT the error token at the low end: low confidence is not a
     failure, and colouring it red teaches people to distrust a working run. */
  --a2c-confidence-high: var(--cds-support-success);
  --a2c-confidence-medium: var(--a2c-amber);
  --a2c-confidence-low: var(--cds-text-secondary);
  --a2c-confidence-unknown: var(--cds-text-placeholder);

  /* The same scale as text. High is the only one that has to change: green at
     3.35:1 is fine for a 2px stroke and not fine for a 12px label. */
  --a2c-confidence-high-text: var(--a2c-text-success);
  --a2c-confidence-medium-text: var(--a2c-amber);
  --a2c-confidence-low-text: var(--cds-text-secondary);
  --a2c-confidence-unknown-text: var(--cds-text-helper);

  /* Bounding-box overlay over a user-supplied image. The image underneath is
     arbitrary, so boxes carry their own contrast and never rely on the page
     background. */
  --a2c-overlay-scrim: rgba(0, 0, 0, 0.34);
  --a2c-overlay-halo: rgba(0, 0, 0, 0.55);
  --a2c-overlay-stroke: 2px;

  --a2c-shadow-raised: 0 2px 6px var(--cds-shadow);
  --a2c-shadow-overlay: 0 12px 32px var(--cds-shadow);

  /* Layout. */
  --a2c-header-height: 3rem;
  --a2c-sidebar-width: 20rem;
  --a2c-content-max: 96rem;
  --a2c-prose-max: 68ch;
}}
'''
    return head, len(names), len(dark_names)


def copy_licenses(work: Path) -> None:
    pairs = [
        (work / "x" / "carbon-styles" / "package" / "LICENSE", VENDOR / "carbon" / "LICENSE"),
        (work / "x" / "ibm-plex-sans" / "package" / "LICENSE.txt", VENDOR / "plex" / "LICENSE-plex.txt"),
    ]
    for src, dst in pairs:
        if src.exists():
            shutil.copy2(src, dst)
        else:
            # Not fatal, but say so: redistributing without the licence text is
            # the kind of thing that gets a submission disqualified.
            print(f"WARNING: {src.name} not found in the package; copy it manually "
                  f"into {dst} before shipping.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="check the vendored tree, download nothing, write nothing")
    args = parser.parse_args()

    if args.verify:
        return verify()

    with tempfile.TemporaryDirectory(prefix="a2c-vendor-") as tmp:
        work = Path(tmp)
        npm_pack(work)

        (VENDOR / "carbon").mkdir(parents=True, exist_ok=True)
        (VENDOR / "plex").mkdir(parents=True, exist_ok=True)

        carbon_css, n_faces, faces_bytes = build_carbon_css(work)
        (VENDOR / "carbon" / "carbon.css").write_text(carbon_css, encoding="utf-8")
        print(f"carbon.css: stripped {n_faces} @font-face blocks ({faces_bytes} bytes)")

        for pkg, stem, _family, _weight in PLEX_FACES:
            src = (work / "x" / pkg / "package" / "fonts" / "complete" /
                   "woff2" / f"{stem}.woff2")
            if not src.exists():
                fail(f"{src} is missing; the Plex package layout changed.")
            shutil.copy2(src, VENDOR / "plex" / f"{stem}.woff2")

        (VENDOR / "plex" / "plex.css").write_text(build_plex_css(), encoding="utf-8")
        copy_licenses(work)

        tokens, n10, n100 = build_tokens_css(work)
        TOKENS_CSS.write_text(tokens, encoding="utf-8")
        print(f"tokens.css: {n10} g10 tokens, {n100} g100 tokens")

    return verify()


def verify() -> int:
    """Fail if anything in the vendored tree would reach for the network."""
    problems: list[str] = []
    if not VENDOR.exists():
        problems.append(f"{VENDOR} does not exist; run this script without --verify.")
    for css in sorted(VENDOR.rglob("*.css")) + [TOKENS_CSS]:
        if not css.exists():
            problems.append(f"missing: {css}")
            continue
        text = css.read_text(encoding="utf-8")
        for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", text):
            if url.startswith(("http://", "https://", "//")):
                problems.append(f"{css.relative_to(REPO_ROOT)} reaches the network: {url}")
    for _pkg, stem, _f, _w in PLEX_FACES:
        face = VENDOR / "plex" / f"{stem}.woff2"
        if not face.exists():
            problems.append(f"missing font face: {face}")

    total = sum(p.stat().st_size for p in VENDOR.rglob("*") if p.is_file())
    if problems:
        for p in problems:
            print(f"FAIL {p}", file=sys.stderr)
        return 1
    print(f"OK: vendored tree is self-contained, {total:,} bytes "
          f"({total / 1024:.1f} KB) in {VENDOR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
