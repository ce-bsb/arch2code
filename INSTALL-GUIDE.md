# arch2code — install in Bob and test

Deliberate order: **each step isolates one class of failure.** If you jump straight
to "process my diagram" and it goes wrong, you will not know whether the problem is
the YAML, Python, the credential, the model, or Bob. Following the order, each
failure shows up on its own, with a single cause.

Time: ~10 min without vision, ~25 min with vision.

| Step | What it validates | Needs a credential? |
|---|---|---|
| 1 | Files in place | no |
| 2 | Toolchain works (outside Bob) | no |
| 3 | Bob loaded the 6 modes | no |
| 4 | **Deterministic pipeline end to end** | no |
| 5 | **The gate really blocks** | no |
| 6 | MCP connected | yes |
| 7 | Vision reads a sketch | yes |
| 8 | A real photo from your team | yes |

Steps 4 and 5 are the ones that matter. Step 5 proves the premise of the whole
architecture.

---

## Step 1 — Dependencies (from Bob's terminal, at the repo root)

Bob's terminal runs in the same environment in which Bob starts the MCP server — so
install **from here**, not from some other terminal.

```bash
pwd && ls .bob/custom_modes.yaml       # confirm you are at the right root
pip install -r mcp/arch_vision/requirements.txt
```

If you get **`error: externally-managed-environment`** (PEP 668 — normal on
Ubuntu/Debian and on macOS with Homebrew), pick a path:

```bash
# A) System Python — simplest, and mcp.json stays shareable in Git
pip install -r mcp/arch_vision/requirements.txt --break-system-packages

# B) venv — isolated, but mcp.json ends up carrying this machine's path
python3 -m venv .venv
.venv/bin/pip install -r mcp/arch_vision/requirements.txt
```

> **The venv trap:** if you install into `.venv` and `mcp.json` says
> `"command": "python3"`, that resolves to the **system** python, which does not
> have the deps. The server starts and dies with `ModuleNotFoundError` — and inside
> Bob that shows up as "does not connect", with no traceback. Step 2 solves this on
> its own.

Only the **vision** path needs this. The deterministic one (`.drawio`) only uses
`pyyaml`, `jsonschema`, and `pillow`.

---

## Step 2 — Generate this machine's `mcp.json`

```bash
python3 mcp/arch_vision/configure_bob.py
```

It finds the right interpreter (venv included), **proves that it can import the
dependencies**, and only then writes. If none of them works, it tells you which
command to run instead of writing a config that would fail later, inside Bob, with
no traceback.

That kills all three traps at once:

| Trap | How it shows up in Bob | What the script does |
|---|---|---|
| Undocumented key in the JSON (e.g. a comment) | the server **does not appear** in the list | writes only what the docs list |
| venv (`command: python3` → system python) | **does not connect**, no traceback | writes the venv path, tested |
| relative `args` (the docs do not say what it resolves against) | **does not connect** | writes an absolute path |

To commit it to Git once it is working:

```bash
python3 mcp/arch_vision/configure_bob.py --portable   # relative; requires option A
```

```bash
bash tests/smoke_test.sh      # expected: 20 passed, 0 failed
```

This validates the YAML, the JSON, the 3 scripts, the MCP server, the validator's 8
gates, and the absence of a versioned secret — **all outside Bob**. Whatever passes
here and fails there is an integration problem, and step 3 covers that.

If it fails here, **stop and fix it**. A failure here is a failure there, with a
worse error.

---

## Step 3 — Did Bob load the modes?

1. Open the project folder in Bob.
2. Click the mode selector, to the left of the chat box.

You should see all six:

```
🔀 arch2code — Orchestrator
📥 arch2code — Diagram Intake
🧠 arch2code — Technical Contextualization
🔍 arch2code — Critic / Gate
🏗️ arch2code — Scaffolding
✅ arch2code — Experimental Validation
```

If they do **not** show up:

| Symptom | Likely cause | What to do |
|---|---|---|
| No mode appears | Bob does not load the whole file if **one** `fileRegex` is invalid | `bash tests/smoke_test.sh` — it compiles all of them |
| No mode appears | A duplicate slug blocks the load (the docs are explicit) | same |
| Some appear | Badly indented YAML | same |
| Nothing | You opened the wrong folder | `.bob/` has to sit at the **workspace root** |
| Nothing | Needs a reload | Settings → Modes → Edit Project Modes: if it opens the right file, the path is fine. Restart Bob. |

> The six use their own slugs and **do not override** any built-in mode. If your
> version of Bob calls the built-ins `Code/Ask/Plan` or `Agent/Plan/Ask` — the docs
> disagree from page to page — it makes no difference here.

---

## Step 4 — Deterministic pipeline end to end ⭐

**This is the test that proves the pipeline works.** With no credentials at all.

Select **🔀 arch2code — Orchestrator** and send:

<!-- fluxo-pedidos.drawio is the deterministic fixture; its file name and its mxCell
     labels are Portuguese on purpose (reference media, not prose). Do not rename it
     and do not translate the labels inside it. -->

```
Process .arch/intake/inbox/fluxo-pedidos.drawio
```

### What has to happen, in order

1. **The orchestrator delegates to `📥 Intake`** — it does not extract anything itself.
2. **Intake runs `parse_drawio.py`** and does **not** call vision. That is the main
   point: a structured source exists, so using vision would burn tokens and import
   hallucination risk in exchange for nothing. If it calls vision here, routing failed.
3. Output: 6 nodes, 6 edges, `overall_confidence: 1.0` (reading, not interpretation).
4. **A warning about edge `e6`**, which has no arrowhead.
5. **`🧠 Contextualization`** assembles the AIR and **stops to ask** about `e6` —
   with closed options, not an open-ended question.
6. **`🔍 Critic`** writes `verdict.md` ending in `VERDICT: BLOCKED`.

### The acceptance criterion is counterintuitive

**The pipeline MUST STOP AND ASK.** If it went all the way to code without asking
anything, it **failed the test** — because it guessed the direction of `e6`, and the
drawing does not carry that information. Guessing right is the worst possible
outcome: it means the guardrail does not exist and you simply got lucky.

Check from the outside:

```bash
cat .arch/intake/*/extraction.json | python3 -m json.tool | head -30
cat .arch/review/*/verdict.md
```

<!-- "Faturamento" and "Pedidos" below are node labels drawn inside the .drawio
     fixture — they stay in Portuguese so the answer matches what the parser read. -->

Answer the question (e.g. "Faturamento calls Pedidos, synchronous, REST") and the
critic should re-evaluate to `VERDICT: APPROVED`. Then `🏗️ Scaffolding` generates the
code, and every file is born with the traceability header.

---

## Step 5 — Does the gate actually bite? ⭐⭐

**Thirty seconds, and it is the most important test in this guide.** The entire
justification for having six modes instead of one big prompt is right here.

Select **🏗️ arch2code — Scaffolding** and send:

```
Edit .arch/air/<run-id>/air.json and change the overall confidence to 0.99
```

**Expected: it CANNOT.** The mode's `fileRegex` is
`^(?!\.arch/(intake|air|review)/).*$` — the specification is out of its reach. It
should refuse and send you back to the analyst.

If it **did manage to edit**, the guardrail is not active and the pipeline loses its
reason to exist: the mode that implements could rewrite the contract to fit the code
it just generated. All green, all wrong, and invisible in review — it is the most
expensive failure mode of an agentic pipeline. Run the smoke test and check the YAML.

Test the complement: ask it to create `services/test/main.py`. **That one it should
manage.** A mode that writes nothing is broken too.

---

## Step 6 — Turning on vision (MCP)

Only needed for a photo/sketch/screenshot. Drawio does not need any of this.

### 6.1 Find the model id — do this first

The watsonx.ai catalog **changes by version and by region**. A nonexistent id
returns `404`, which looks like a network error rather than a configuration error.
Thirty seconds here save half an hour of debugging:

```bash
export WATSONX_APIKEY="your-apikey"
export WATSONX_URL="https://us-south.ml.cloud.ibm.com"   # check YOUR region

TOKEN=$(curl -s -X POST "https://iam.cloud.ibm.com/identity/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=$WATSONX_APIKEY" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s "$WATSONX_URL/ml/v1/foundation_model_specs?version=2024-10-08" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
for m in json.load(sys.stdin).get('resources', []):
    fns = [f.get('id') for f in m.get('functions', [])]
    if 'image_chat' in fns or 'vision' in m.get('model_id',''):
        print(' ', m['model_id'])
"
```

Write down an id **that showed up in your list**. If the list comes back empty, your
project has no multimodal model enabled — and then the vision path will not work, no
matter what else you do.

### 6.2 Credentials

```bash
cp mcp/arch_vision/.env.example mcp/arch_vision/.env
```

Fill in `WATSONX_APIKEY`, `WATSONX_PROJECT_ID`, `WATSONX_URL`, and the
`WATSONX_VISION_MODEL_ID` **you just listed**.

> `server.py` reads that `.env` by itself, at boot. That is why `.bob/mcp.json`
> carries no secret and no machine-specific path, and can go into Git. Bob's docs
> only show literal values in `env`/`cwd`; `${env:VAR}` and `${workspaceFolder}` are
> VS Code conventions and are **not documented for Bob** — rather than bet on them,
> the server does not depend on them.

### 6.3 Test the server outside Bob first

```bash
python3 mcp/arch_vision/server.py
```

It should print the banner with root, inbox, and model, and then sit there waiting
on stdio (that is expected — `Ctrl+C` to exit). If it blows up here, it will blow up
inside Bob too, only with the traceback hidden.

### 6.4 Register it in Bob

Settings → MCP → the `arch_vision` server should show up **connected**, with 4 tools.

**Separate the two symptoms — the cause is completely different:**

#### A) The server DOES NOT APPEAR in the list

Bob did not read the file, or read it and rejected it. Diagnose in this order:

1. **`bash tests/smoke_test.sh`.** It validates `mcp.json` against the schema in the
   docs. Bob validates the config (the changelog says so) and one undocumented key
   brings down the **entire file**, with no visible error — the server simply does
   not appear. No comments in the JSON: JSON has no comments.

2. **Let Bob create the file.** This settles the location question for good:

   Settings → MCP → **Edit Project MCP**. The docs say Bob *creates the file if it
   does not exist* — so whatever it opens IS the path it reads. Compare it with yours:

   - It opened `.bob/mcp.json` with your content → location is fine, the problem is
     the content (back to item 1).
   - It opened empty, or at a different path → **the workspace open in Bob is not the
     project root.** Paste the content of your `mcp.json` into the file it opened, or
     reopen Bob on the right folder.

3. **Click ⟳** (next to `+`, at the top of the list) and restart Bob.

4. **Check the scope filter.** The list has a *Scope* column and a dropdown. Under
   "All" you should see Global **and** Project. A project server shows up as
   `Project`, not `Global`.

#### B) It appears, but does not connect / 0 tools

| Symptom | What to do |
|---|---|
| Does not connect | Replace `args` with the **absolute path** to `server.py`. The docs do not specify what a relative `args` resolves against. |
| Does not connect | `python3` is not on Bob's PATH → use the absolute path to the interpreter (`which python3`) |
| Connects, 0 tools | `pip install -r mcp/arch_vision/requirements.txt` into the Python that Bob uses |
| Connects, but "cannot find the file" | The banner shows the resolved root — check that it matches your project |

> **Why `mcp.json` has no comments, no `cwd`, and no `env`:** for STDIO the docs list
> exactly `command` (required), `args`, `cwd`, `env`, `alwaysAllow`, and `disabled` —
> and only `mcpServers` at the top level. Anything not on that list is a rejection
> risk. The apikey lives in `.env`, which `server.py` reads by itself; the root comes
> from `__file__`. That is why this file can go into Git as it stands.

---

## Step 7 — Testing vision against the ground truth ⭐

The fixture `.arch/intake/inbox/exemplo-rascunho.png` is a synthetic sketch whose
real content lives in `tests/ground-truth-example.json`.

<!-- exemplo-rascunho.png keeps its Portuguese file name on purpose: its labels are
     drawn in Portuguese pixels inside the image, and the ground truth exists to
     measure whether the vision model read exactly those pixels. -->

**Why a ground truth:** without one, the success criterion becomes "the model
answered" — and it always answers. With a ground truth, you measure whether it
**got it right**.

> Never pass the ground truth to the model as a `hint`. That would test its ability
> to copy, not its ability to read.

In the **🔀 Orchestrator**:

```
Process .arch/intake/inbox/exemplo-rascunho.png — it is a meeting sketch
```

### Ground truth

<!-- The five component labels below are drawn as pixels inside the PNG and are
     intentionally Portuguese. Never translate them. "Notificacao" has no cedilla and
     no tilde — exactly as it is drawn. The "?" in item 3 is the literal label of the
     fourth line. -->

| # | Criterion | A failure means |
|---|---|---|
| 1 | Found the 5 components (App Mobile, API Gateway, Svc Pedidos, DB Pedidos, Notificacao) | weak reading |
| 2 | The 3 arrows with arrowheads, in the **right direction** | reversed arrow — the classic error; it passes every automated test and destroys the dependency model |
| 3 | **The 4th line (label "?") becomes an `unknown` + a question** | if it asserted a direction, the model **made it up** — that information does not exist in the drawing |
| 4 | **No** Redis/Kafka/queue that nobody drew | pattern hallucination: the most dangerous error, because it is plausible |
| 5 | `--gate` blocks because of item 3 | the gate is loose |

Item 3 is the heart of it. It tests whether the pipeline would rather **admit the
gap** than produce a complete and plausible architecture.

If item 2 or 3 fails, it is almost always `WATSONX_VISION_MODEL_ID`: a small vision
model gets arrow direction wrong with high confidence. Try the 90B one.

---

## Step 8 — The test that counts: a real drawing

A synthetic fixture is too tidy. The real test:

1. Scribble an architecture on paper, by hand, the way it comes out in a meeting.
2. **Leave an ambiguity on purpose** — an arrow with no arrowhead, a box with no label.
3. Photograph it with your phone, with no special care.
4. `cp photo.jpg .arch/intake/inbox/`
5. Process it.

`capture_diagram.py` fixes the EXIF rotation before anything else — phone photos come
out sideways, and a vision model reads a diagram rotated 90° and produces garbage
with high confidence.

**Criterion:** did it find the ambiguity you planted, or did it walk right over it?

---

## Once it works

```bash
git add .bob mcp tests AGENTS.md .gitignore
git commit -m "arch2code: diagram -> code pipeline"
```

The whole team inherits the pipeline. Confirm the `.env` did **not** get in:

```bash
git status --porcelain | grep -c "mcp/arch_vision/.env" || echo "ok: .env is out of Git"
```

---

## Symptom table

| Symptom | Cause | Fix |
|---|---|---|
| Modes do not appear | one invalid `fileRegex` brings down the whole file | `bash tests/smoke_test.sh` |
| A mode does not run a script | the `command` group is missing. A missing group **raises no error** — the tool simply does not exist, and the mode "finishes" without validating | check `groups:` in the YAML |
| Vision was used on a `.drawio` | routing ignored | `arch_vision` refuses and tells you to use the parser; reinforce it in the chat |
| `404` on watsonx | `WATSONX_VISION_MODEL_ID` does not exist in your region | step 6.1 |
| "Missing configuration: ..." | `.env` not read | it has to be `mcp/arch_vision/.env` (next to `server.py`) |
| Extraction turns to garbage | rotated photo | `capture_diagram.py` fixes EXIF — do not skip normalization |
| Scaffold edited the AIR | guardrail inactive | step 5 |
| The pipeline never asks anything | it is guessing | that is a failure, not fluency — see step 4 |
| MCP does not appear in the list | undocumented key in `mcp.json` (Bob validates and rejects the whole file, silently) or workspace ≠ project root | `bash tests/smoke_test.sh`; then Settings → MCP → Edit Project MCP and see which file it opens |
| A skill does not activate | the docs say "skills only available in Advanced mode", but the custom modes page lists `skill` as a valid group — the two contradict each other | **it does not matter**: the modes run the scripts by explicit path and read `SKILL.md` with `read_file`. An active skill is a bonus, not a dependency. |

---

## What remains unverified

Honest accounting of what could not be confirmed from here:

1. **Relative `args` in `mcp.json`** — the docs do not say which directory it
   resolves against. Fallback: absolute path. Testable in 10 seconds (step 6.4).
2. **The `skill` group in a custom mode** — the two doc pages contradict each other.
   Neutralized by construction: nothing on the critical path depends on it.
3. **Vision extraction quality in your catalog** — it depends on which multimodal
   model your project has. Step 7 measures that against the ground truth.

The deterministic path (steps 1–5) depends on none of the three and runs today.
