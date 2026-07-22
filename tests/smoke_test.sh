#!/usr/bin/env bash
# smoke_test.sh — validates arch2code WITHOUT Bob and WITHOUT a watsonx credential.
#
#   bash tests/smoke_test.sh
#
# Why run this before opening Bob: when something fails inside Bob, you cannot
# tell whether the problem is your YAML, your Python, your environment or Bob
# itself. This script eliminates three of the four. Whatever passes here and
# fails there is a Bob integration problem — and the guide has a table for that.
#
# Exit: 0 = all green. 1 = something is broken, with the reason.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0; fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n     -> %s\n' "$1" "$2"; fail=$((fail+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head_ "0. Environment"
python3 --version >/dev/null 2>&1 && ok "python3 present" || bad "python3" "install Python 3.9+"
python3 -c "import yaml" 2>/dev/null && ok "pyyaml" || bad "pyyaml" "pip install pyyaml --break-system-packages"
python3 -c "import jsonschema" 2>/dev/null && ok "jsonschema" || bad "jsonschema" "pip install jsonschema --break-system-packages"
python3 -c "import PIL" 2>/dev/null && ok "pillow" || bad "pillow" "pip install pillow --break-system-packages"

head_ "1. Bob configuration"
python3 - <<'EOF' 2>/dev/null && ok "custom_modes.yaml: 6 modes, valid groups, fileRegex compile" \
  || bad "custom_modes.yaml" "invalid YAML or broken fileRegex — Bob does NOT load the whole file if this fails"
import yaml, re, sys
m = yaml.safe_load(open('.bob/custom_modes.yaml'))['customModes']
assert len(m) == 6, f"expected 6 modes, found {len(m)}"
VALID = {'read','edit','browser','command','mcp','skill'}   # official Bob doc
slugs = set()
for x in m:
    assert x['slug'] not in slugs, f"duplicate slug: {x['slug']}"   # a duplicate blocks the load
    slugs.add(x['slug'])
    assert re.fullmatch(r'[a-z0-9-]+', x['slug']), x['slug']
    for k in ('name','roleDefinition','groups'):
        assert k in x, f"{x['slug']} missing {k}"
    gs = set()
    for g in x['groups']:
        if isinstance(g, str): gs.add(g)
        else:
            gs.add(g[0]); re.compile(g[1]['fileRegex'])
    assert not gs - VALID, f"{x['slug']}: invalid group {gs - VALID}"
sys.exit(0)
EOF

python3 - <<'EOF' 2>/dev/null && ok "mcp.json: matches the documented schema (only mcpServers at the top)" \
  || bad "mcp.json" "undocumented key — Bob validates the config and may REJECT the whole file silently: the server simply never shows up in the list"
import json, sys
d = json.load(open('.bob/mcp.json'))
# Doc: "Both files use JSON format with an mcpServers object". That and nothing else at the top.
# JSON has no comments: a "_comment" here takes the entire file down.
if set(d.keys()) != {"mcpServers"}:
    sys.exit(1)
# Doc: table "STDIO transport / Configuration parameters"
PERMITIDO = {"command","args","cwd","env","alwaysAllow","disabled","url","httpURL","headers","type"}
for nome, cfg in d["mcpServers"].items():
    if set(cfg) - PERMITIDO: sys.exit(1)
    if not ({"command","url","httpURL"} & set(cfg)): sys.exit(1)
sys.exit(0)
EOF

python3 - <<'EOF' 2>/dev/null && ok "no secret in a versioned file" || bad "VERSIONED SECRET" "remove the credential; use mcp/arch_vision/.env"
import json, re, sys
raw = open('.bob/mcp.json').read()
if re.search(r'"(WATSONX_APIKEY|apikey)"\s*:\s*"[A-Za-z0-9_\-]{15,}"', raw): sys.exit(1)
sys.exit(0)
EOF

# These three paths are literal. If the rule files are renamed again, this list
# and custom_modes.yaml:253/333 have to be updated in the same commit.
for f in .bob/rules/00-arch2code-guardrails.md .bob/rules-arch-critic/01-review-rubric.md \
         .bob/rules-arch-scaffold/01-codegen-standards.md; do
  [ -f "$f" ] && ok "rule present: $f" || bad "$f" "missing — the mode loses its constraints"
done

python3 - <<'EOF' 2>/dev/null && ok "all 4 skills have name+description frontmatter" \
  || bad "SKILL.md" "frontmatter missing — a skill without a description is IGNORED by Bob"
import re, glob, yaml, sys
fs = glob.glob('.bob/skills/*/SKILL.md')
assert len(fs) == 4, f"expected 4 skills, found {len(fs)}"
for p in fs:
    m = re.match(r'^---\n(.*?)\n---\n', open(p).read(), re.S)
    assert m, p
    fm = yaml.safe_load(m.group(1))
    assert fm.get('name') and fm.get('description'), p
sys.exit(0)
EOF

head_ "2. Deterministic path (this is what runs without a credential)"
if python3 .bob/skills/diagram-intake/scripts/parse_drawio.py \
     .arch/intake/inbox/fluxo-pedidos.drawio --out /tmp/_smoke_extraction.json >/dev/null 2>&1; then
  python3 - <<'EOF' 2>/dev/null && ok "parse_drawio: compressed drawio -> 6 nodes, 6 edges, confidence 1.0" \
    || bad "parse_drawio" "it ran but extracted no nodes/edges"
import json, sys
d = json.load(open('/tmp/_smoke_extraction.json'))
# The deterministic path emits structure PER PAGE (drawio has tabs); the vision
# path emits components/connections at the top level. The formats diverge on
# purpose: the extraction is raw and specific to the path. The AIR is where the
# two converge — that is what the air-normalizer skill does, hence its name.
assert d['pages'], "no page was read"
nodes = sum(len(p['nodes']) for p in d['pages'])
edges = sum(len(p['edges']) for p in d['pages'])
assert nodes == 6, f"expected 6 nodes, found {nodes}"
assert edges == 6, f"expected 6 edges, found {edges}"
assert d['overall_confidence'] == 1.0, "a structural read should have confidence 1.0"
assert d['source_sha256'], "no hash: traceability back to the original file is lost"
sys.exit(0)
EOF

  python3 - <<'EOF' 2>/dev/null && ok "parse_drawio: detects the edge with no arrowhead (becomes an unknown in the AIR)" \
    || bad "parse_drawio" "it did not warn about the edge with no arrowhead"
import json, sys
d = json.load(open('/tmp/_smoke_extraction.json'))
# Load-bearing substring: it must match the warning produced by parse_drawio.py.
assert any('no arrowhead' in w for w in d['warnings']), "the indeterminate-direction warning is gone"
sys.exit(0)
EOF
else
  bad "parse_drawio" "failed to read the example .drawio"
fi

python3 .bob/skills/diagram-intake/scripts/capture_diagram.py --list >/dev/null 2>&1 \
  && ok "capture_diagram --list: routes the inbox artifacts" \
  || bad "capture_diagram --list" "failed"

head_ "3. Validation gates (the heart of the pipeline)"
python3 .bob/skills/air-normalizer/scripts/validate_air.py \
  .bob/skills/air-normalizer/example-air.json >/dev/null 2>&1 \
  && ok "validate_air: the example AIR passes the schema" \
  || bad "validate_air (schema)" "the example should be valid against the schema"

python3 .bob/skills/air-normalizer/scripts/validate_air.py \
  .bob/skills/air-normalizer/example-air.json --gate >/dev/null 2>&1
[ $? -eq 1 ] && ok "validate_air --gate: BLOCKS the example (missing arrow) — the gate bites" \
             || bad "validate_air --gate" "it did NOT block. The gate is loose — the whole pipeline loses its point."

# Inject a defect and confirm the gate catches it. A gate that never fails anything is not a gate.
python3 - <<'EOF' >/dev/null 2>&1
import json
d = json.load(open('.bob/skills/air-normalizer/example-air.json'))
d['connections'].append({"id":"c_quebrada","from":"nao_existe","to":"tambem_nao",
    "protocol":"http","sync":"sync","confidence":0.9,
    "evidence":{"kind":"human","value":"teste"}})
json.dump(d, open('/tmp/_smoke_broken.json','w'))
EOF
python3 .bob/skills/air-normalizer/scripts/validate_air.py /tmp/_smoke_broken.json >/dev/null 2>&1
[ $? -ne 0 ] && ok "validate_air: detects the injected broken reference" \
             || bad "validate_air" "it did not detect a connection pointing at a nonexistent component"

head_ "4. MCP server (no credential: structure only)"
python3 -m py_compile mcp/arch_vision/server.py 2>/dev/null \
  && ok "server.py compiles" || bad "server.py" "syntax error"

if python3 -c "import mcp, httpx" 2>/dev/null; then
  python3 - <<'EOF' 2>/dev/null && ok "MCP: 4 tools registered with a flat schema" || bad "MCP" "the tools did not register"
import asyncio, sys
sys.path.insert(0, 'mcp/arch_vision')
import server
async def main():
    ts = await server.mcp.list_tools()
    assert len(ts) == 4, f"expected 4 tools, found {len(ts)}"
    for t in ts:
        props = (t.inputSchema or {}).get('properties', {})
        assert 'params' not in props, f"{t.name}: nested schema — Bob will get the call wrong"
asyncio.run(main())
EOF
else
  printf '  \033[33m–\033[0m mcp/httpx not installed: skipped the server test\n'
  printf '     -> only needed for the VISION path: pip install -r mcp/arch_vision/requirements.txt\n'
fi

head_ "5. Test fixture"
[ -f .arch/intake/inbox/exemplo-rascunho.png ] && ok "synthetic sketch present" || bad "fixture" "missing"
[ -f tests/ground-truth-example.json ] && ok "ground truth present (lets you MEASURE the extraction, not just run it)" || bad "ground truth" "missing"

rm -f /tmp/_smoke_extraction.json /tmp/_smoke_broken.json
printf '\n\033[1m%d passed, %d failed\033[0m\n' "$pass" "$fail"
if [ "$fail" -eq 0 ]; then
  printf '\033[32mGreen. You can open Bob — the guide continues at step 3.\033[0m\n'; exit 0
else
  printf '\033[31mFix this before opening Bob: a failure here is a failure there, with a more confusing error.\033[0m\n'; exit 1
fi
