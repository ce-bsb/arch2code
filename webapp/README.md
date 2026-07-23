# arch2code — the web application

The pipeline described in [`../README.md`](../README.md) is a folder of Bob configuration.
Running it by hand means installing Bob, holding a licence, wiring an MCP server, exporting
watsonx credentials and knowing which chat mode to select. That is a barrier only an engineer
can clear.

**This application is the abstraction layer that removes the barrier.** It drives the same six
Bob modes as subprocesses, streams what the model is doing while it does it, stops at the human
gate, and hands back the artifacts. To the person using it, arch2code is a page with a drop
zone.

---

## Who it is for: anyone who can draw the idea

The web app exists so that turning a drawing into a working solution no longer requires the
person who can *write the platform's code*. It requires only the person who *has the idea*.

| If you are… | You do this | You get back |
|---|---|---|
| a **solution architect / AI engineer** | drop the reference architecture, answer the blocking questions | a running implementation with a manifest tying every file to a box on your drawing |
| a **technical seller / Client Engineering** | drop the client's own workshop diagram | a demo the client can drive, the same week — before a build team is staffed |
| an **executive or product owner** | drop a slide from the deck | something demonstrable within a day, so a funding decision rests on what runs |
| a **designer or business analyst** | drop a hand sketch or a `.drawio` | the idea expressed as real components, tests and docs — and honest questions where the sketch was silent |
| a **non-technical stakeholder** | drop a photo of a whiteboard | a working prototype, and a plain-language record of every assumption made on your behalf |

Nobody picks a mode, writes a prompt, or holds a credential. One gesture — drop a file — starts
a governed, five-stage pipeline built on cutting-edge tooling (watsonx.ai multimodal inference,
an MCP server, a schema-validated contract, adversarial review) and hands the result back as a
page you can read and a zip you can run.

[`../README.md`](../README.md) is about the solution: the architecture, what each stage
delivers, and the contract they pass between them. **This file is about the application**: every
screen, every accepted format, the HTTP surface, every environment variable, the health probes,
how to deploy it, and what to do when something breaks.

### **https://bob-arch2code.2clgzcr0unhv.us-south.codeengine.appdomain.cloud**

<sub>A single shared instance on IBM Code Engine. If the link does not answer, running it locally is one command.</sub>

```bash
./run.sh          # then open http://127.0.0.1:8765
```

That is the whole install. No `npm`, no bundler, no CDN, no database, no queue. The front end is
hand-written HTML, CSS and ES modules served straight off disk, so the app comes up on a machine
with no internet and stays up through a demonstration.

---

## The three screens

### 1 · Upload — one gesture, no mode picker

<a href="https://github.com/ce-bsb/arch2code/raw/main/docs/videos/arch2code-github-home-animation.mp4"><img src="../docs/videos/arch2code-github-home-animation.gif" alt="arch2code demo — drop a drawing and watch the pipeline run to the human gate" width="100%"></a>

> ▶ The clip above **plays and loops automatically** (≈3× speed). **[Watch the full-length HD recording](https://github.com/ce-bsb/arch2code/raw/main/docs/videos/arch2code-github-home-animation.mp4)** — the same run, in real time, from an empty drop zone to the human gate.

**What this screen solves: the cost of the first attempt.** Dropping a file uploads it, creates
the run and starts it. Create and start stay two calls on the wire so a browser retry cannot pay
twice, but they are one gesture for the user, because the default intent is always to advance.
There used to be two front-door choices — "read the diagram" and "diagram to code" — and nobody
opens this app to have a diagram described back to them, so the vision preview became a *stage*
of the one journey instead of a fork on the landing page.

The specialist knobs live behind **Advanced options** and are read at fire time, so opening the
disclosure first and dropping second does the right thing, and never opening it at all does the
right thing too:

| Knob | Effect |
|---|---|
| How the drawing was made | `napkin` / `whiteboard` make the extractor more conservative: it prefers recording an unknown over guessing. `screenshot` / `pdf` do not |
| Run name | lowercase, digits, hyphens — becomes `YYYYMMDD-HHMM-<name>`, the id every artifact directory is named after |
| Hint for the model | context it is told to use, *and told not to treat as something it saw on the page* |
| Run under a PTY | only for the case where a stage exits 0 with no output — see [Troubleshooting](#troubleshooting) |

`route_artifact()` on the server is the authority on formats; the browser's `accept` list is
mirrored from it so the picker can never hide a format the server supports. A file the picker
accepts and the server refuses comes back as a 415 stating the exact reason.

| Group | Extensions | What happens to it |
|---|---|---|
| Photographs and screenshots | `.png .jpg .jpeg .webp .heic .heif .bmp .tif .tiff` | EXIF-corrected and normalized to a 1568 px long edge, then read by a watsonx.ai vision model. The original bytes are kept untouched alongside the copy the model saw |
| Structured sources | `.drawio .xml .puml .plantuml .mmd .mermaid .md .json .yaml .yml` | Parsed exactly — **no tokens spent, nothing inferred**. A structured source always wins over an image of the same drawing |
| PDF | `.pdf` | Text and geometry first; the vision path only if the page is a pure image |

The health banner is deliberate: **a failing probe never prevents startup**, because a UI you
cannot reach cannot tell you what is broken. It has a Retry button that re-runs every probe, so
fixing an environment variable never requires restarting the server. The two capabilities are
gated independently — a machine with a broken Bob install can still read a drawing, and a machine
with no watsonx credentials can still run the pipeline on a structured source.

### 2 · Execution — the timeline, the boxes, and the gate

![The execution screen at the stage-3 gate: the run bar shows run 20260722-0528-modeb2 on exemplo-rascunho.png "waiting for your gate decision", an AWAITING INPUT chip, an Execution/Deliverables tab switcher, meters reading 1,199,026 tokens · cost 3 · 5m 54s elapsed · 27 tool calls, and a Download everything button; the body is titled "STAGE 3 · THE HUMAN GATE" with the headline "The critic blocked this architecture" and a VERDICT: BLOCKED chip; the left column, WHAT THE CRITIC CONTESTED, lists a CRITICAL finding "Invented Connection Direction" on element conn_pedidos_to_notificacao with a four-step correction, a HIGH finding on the same element for confidence 0.45 with verified_by_second_pass false, and a MEDIUM finding naming two blocking unknowns, followed by the executive summary and the critic's blocking questions printed verbatim; the right column, YOUR DECISION, offers approve, block and send-back radio options with a reason field written to gate/decision.json](../docs/images/ui-02-execution.png)

The **Execution** tab is a two-column workspace:

- **Left — the drawing, with the boxes the model drew on it.** This panel answers the first
  question anyone asks of this product: *did it actually read my diagram, or is it making things
  up?* The bounding boxes are the evidence. Two sources feed one panel: the server's
  already-normalized copy (0..1, clamped) when it has one, otherwise the raw `extraction.json`
  artifact, normalized against the image's natural size. The heuristic is blunt on purpose — any
  coordinate above 1.5 means the payload is in pixels — because trusting a mixed payload puts
  boxes in plausible but wrong places, which is worse than drawing none.
- **Right — "What the model did, step by step."** The model's reasoning as readable prose while
  it streams, and every tool call as one collapsible block: tool name, parameters, result,
  duration, token cost. A call is two events separated in time, so the block is patched in place
  when the result lands — the timeline does not grow a second row, and you watch the call
  complete.

**What this screen solves: making governance a place rather than a policy.** When the run reaches
stage 3 the workspace is topped by the gate card. The critic wrote `VERDICT: BLOCKED`, the run
stopped, and no code exists. Each finding names the element it is about and the correction it
wants, so the decision is made against specifics rather than a summary. Approving from here
overrides a machine decision and is recorded as such: the reason field is *optional when you
agree with the verdict, mandatory when you do not*, and it is written to `gate/decision.json`.
**Send back** re-runs from the stage you pick with your reason attached as feedback; **block** is
terminal but not a failure — the audit trail under `.arch/` is kept either way.

The screenshot is run `20260722-0528-modeb2`, executed end to end. Its critical finding is
genuine: stage 1 read a bare line labelled `?` as a directed arrow, the critic's second-pass
verification returned FALSE at confidence 1.0, and stage 4 never started.

### 3 · Deliverables — everything the run produced

![The deliverables screen for run 20260721-2202-image-24, status SUCCEEDED: the section "Everything this run produced" holds three cards — "The whole solution" (9 files, 522 KB) with a Download button; "Just the code", carrying an amber notice reading "This run generated no code to download" that explains the run stopped before generation by design; and "The whole project tree" (12 files, 998 KB). Below, a FILES tree lists the intake directory with capture-manifest.json and the normalized PNG, and the viewer on the right renders capture-manifest.json with syntax highlighting, showing run id, source sha256, source kind screenshot, extraction path vision, byte count, an empty warnings array, and the normalization from 2372×1107 to 1568×732 at scale 0.661](../docs/images/ui-03-deliverables.png)

Three archives, **one planner**, so a preview can never disagree with the download it describes.
Every file is browsable in place before it is downloaded, with Copy and "Download this file" per
artifact — the pane on the right is showing `capture-manifest.json`, the record of what was
captured, its sha256, and exactly how the image was rescaled before any model saw it.

**What this screen solves: the archive somebody opens months from now.** The run shown here read
the drawing and stopped before generation, so the code card says exactly that, in words, with the
reason — instead of handing over an empty zip. A download in this app never implies work that was
not done.

---

## How it works

**FastAPI drives Bob as a subprocess.** One clean Bob session per stage:
`bob --chat-mode <slug> --output-format stream-json`, with the previous stage's artifact
referenced by absolute path in the next stage's prompt. **Stages chain by file, not by session.**
The MCP vision server is spoken to directly over stdio for the preview path.

**The webapp is the orchestrator.** Bob's own `arch2code` orchestrator mode is never spawned —
the runner drives the five stages itself, because the stage-3 gate has to be a human decision in
a browser rather than a model deciding to switch modes. The health check still asserts the
`arch2code` slug exists: its absence proves the working directory is wrong, which is the same
root cause that would break the other five.

**The working directory is part of the contract, twice.** Bob resolves `--chat-mode` from the
`.bob/custom_modes.yaml` of its *working directory* — from the repo root it offers ten chat
modes, from anywhere else four. And `capture_diagram.py` writes `.arch/intake/<run>/` relative to
*its* working directory, which the design exploits: the vision preview points it at a scratch
workspace, the pipeline points it at the repo root where the artifacts belong.

**The event log is the primary store, not a cache.** `runs/<run_id>/events.jsonl` is append-only.
Every event gets a monotonic id and is fsynced *before* any in-memory subscriber is notified, so
there is no in-memory queue that can drop one. SSE replays from the file starting after
`Last-Event-ID`, then tails; a browser refresh at any moment during a run loses nothing, and
`GET /api/runs/<id>/events?after=` serves the identical envelope to anything that cannot hold a
stream open. A run can sit at the gate across a server restart and still resume, because the
entire state is on disk.

**The gate never parks a coroutine.** On reaching stage 3 the task writes `status=awaiting_input`
plus the parsed verdict, emits its events, and **returns**. The task is gone. Your decision
starts a fresh one.

**The exit code is the only source of truth.** Bob's pre-flight failures — invalid auth, an
unaccepted licence, an unknown chat-mode slug — write **zero bytes to stdout**, plain text to
stderr, and exit 1. The NDJSON stream is narration, not an error channel. So stderr is drained by
a task *independent of* stdout (reading stdout to EOF before touching stderr is a textbook pipe
deadlock), captured for every stage regardless of outcome, and classified into a coded remedy —
"exit 1, no output" must never reach a user as a blank panel.

**The NDJSON parser is defensive by construction.** Bob's `stream-json` shapes were read out of
its bundle, not observed against a contract. The parser never raises, never indexes, always uses
`.get()`, always keeps the raw line. An unrecognised `type` becomes `bob.unknown` and renders as
a generic timeline entry with the raw JSON one click away. If Bob's shapes drift, the timeline
degrades to raw lines rather than breaking the run.

**The model's reasoning is merged before it is stored.** Bob streams assistant output one token
per line: one real run produced **3,858 `bob.message` events against 27 tool calls**, a 143:1
noise ratio. `app/ndjson.py:MessageCoalescer` merges consecutive deltas into one event per
readable block *before* the event log — closing a block when a tool call takes the floor, or after
~1200 characters, 0.75 s or 400 fragments, whichever comes first, so a live reader still watches
the model think. The same run replays as **32** message events. Nothing is discarded: `data.text`
is what a human reads, `data.raw` carries every original line, and `data.aggregated` says how many
were folded in.

**Every error states a next action.** Every failure carries `code`, `title`, `detail` and
`remedy`, and the UI renders the remedy as the primary text. No condition in this app is allowed
to end in a spinner.

### The stage table

| # | Stage | Chat mode | Approval | Writes |
|---|---|---|---|---|
| 1 | Intake | `arch-intake` | `auto_edit` | `.arch/intake/<run>/extraction.json` |
| 2 | Analyst | `arch-analyst` | `auto_edit` | `.arch/air/<run>/air.json` |
| 3 | Critic | `arch-critic` | `auto_edit` | `.arch/review/<run>/verdict.md` — **blocking gate** |
| 4 | Scaffold | `arch-scaffold` | `--yolo` | code + `.arch/build/<run>/manifest.json` |
| 5 | Validator | `arch-validator` | `auto_edit` | `.arch/run/<run>/validation.md` |

The approval column is a correctness requirement, not a preference — see
[Troubleshooting](#troubleshooting).

### Reading the gate

The runner reads the **last non-empty line** of `verdict.md` and matches it against
`VERDICT: APPROVED` or `VERDICT: BLOCKED`. There is a third outcome and it is first class:
**`absent`**. Both pre-migration runs in `.arch/` express the decision in prose and contain no
gate string at all, and stage 4 ran on both anyway — which means the gate was satisfied by a
person rather than by the mechanism. `absent` is therefore surfaced as a *defect of the run* that
demands a human decision. It never defaults to approve.

### The degraded stage-2 path

When the Bob account budget runs out, the backend stops answering with no error, no stderr and no
exit; the stall watchdog kills the stage after 180 s of silence. If that happens to the analyst,
the run used to die holding a perfectly good `extraction.json`.

[`app/air_fallback.py`](app/air_fallback.py) is a pure, in-memory transform (no I/O, no
subprocess, no model) that derives from the extraction only what is mechanically derivable —
components, connections, boundaries, evidence, the extraction's own unknowns, plus one
non-blocking unknown per low-confidence connection — and **refuses to author** what an analyst
adds:

- `assumptions[]` is `[]`. Empty is the truthful value; a plausible-sounding assumption with a
  made-up impact is the single most dangerous thing this module could emit, because stage 4
  generates code from it.
- `meta.extractor` names the file and says, in words, that the analyst did not run.
- A blocking unknown, `contextualization_incomplete`, carries the reason and three closed options.
- Unrecognised vocabulary becomes `"unknown"`, never a guess. A missing confidence becomes `0.0` —
  the only value that cannot be mistaken for a measurement.

Three refusals keep it from ever becoming the normal path: it applies to no stage other than the
analyst, it does nothing without an `extraction.json`, and it will not overwrite an `air.json`
already on disk. The stage is **not** marked succeeded — it keeps status `failed` with the code
`analyst_fallback_applied`. Verified: the derived AIR validates against the schema (exit 0) and is
**rejected** by `validate_air.py --gate` (exit 1). That rejection is the designed outcome — the
run reaches the human gate carrying an honest artifact instead of ending in silence.

### Where a run writes, and why that cannot be avoided

The app's own source lives entirely under `webapp/`. A pipeline run **necessarily causes Bob to
write into the repository's `.arch/` tree**: the custom modes' `fileRegex` patterns are anchored
at the workspace root, and that audit trail is the entire value of the pipeline. That is the
pipeline executing, not the app editing your repo.

- Point a demonstration at a scratch clone with `ARCH2CODE_BOB_CWD=/path/to/clone`.
- `DELETE /api/runs/<id>` removes `webapp/runs/<id>/` **only**. Deleting a run never deletes its
  `.arch/` artifacts — the audit trail outlives the UI record.

---

## Traceability — what is actually auditable

The claim this project makes is *"which model read this drawing, when, with which prompt, at what
confidence, and which file did it produce."* Concretely, for every run:

| Recorded | Where |
|---|---|
| **The model's reasoning**, block by block, in order | `runs/<id>/events.jsonl` → `bob.message`, with `data.raw` preserving every original NDJSON line and `data.aggregated` the fold count |
| **Every tool call** with its full parameters and result | `bob.tool_call` / `bob.tool_result` events, rendered as expandable blocks carrying duration and token cost |
| **Which model read the drawing, with which prompt version** | `_provenance` in the extraction: model id, `prompt_version`, `extraction_path` |
| **What was captured and how it was rescaled** | `.arch/intake/<id>/capture-manifest.json`: sha256 of the original, byte count, original and normalized dimensions, EXIF rotation, scale factor |
| **Per-element confidence**, and which elements the model flagged as doubtful | `_quality.connections_needing_verification`, `broken_refs`, `action_required` |
| **Each second-pass verification**: claim, verdict, confidence, contradiction | the critic's `verdict.md`, and `/api/runs/<id>/vision/verifications` for preview-path checks |
| **Cost per stage**: tokens in/out, coins, duration, exit code, stdout line count | `runs/<id>/run.json` → `stages[]` and `totals` |
| **The human decision** and the reason given | `runs/<id>/gate/decision.json` |
| **stderr for every stage**, success or failure | `runs/<id>/stages/<stage>/stderr.txt` |
| **The artifacts themselves**, versioned in the repo | `.arch/{intake,air,review,build,run}/<id>/` |

The stage-detail endpoint returns environment variable **names** (`env_keys`), never values. The
watsonx health probe reports presence only. **No credential reaches `run.json`, `events.jsonl`, an
artifact, a log line or an API response.**

### The HTTP surface

Everything is under `/api`. The UI uses nothing the API does not expose.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` · `POST /api/health/recheck` | the probe report; recheck re-runs every probe without a restart |
| `GET /api/uploads/formats` · `POST /api/uploads` | what is accepted, and the upload itself |
| `POST /api/runs` · `POST /api/runs/<id>/start` | create, then start — two calls so a retry cannot pay twice |
| `GET /api/runs` · `GET /api/runs/<id>` · `DELETE /api/runs/<id>` | list, detail, and removing the UI record only |
| `GET /api/runs/<id>/stream` | SSE, `Last-Event-ID` aware |
| `GET /api/runs/<id>/events?after=<id>` | the polling fallback, identical envelope |
| `POST /api/runs/<id>/gate` | approve · block · send back, with the reason |
| `POST /api/runs/<id>/cancel` | stop a running stage |
| `GET /api/runs/<id>/stages/<stage>` | per-stage detail, including `env_keys` (names only) |
| `GET /api/runs/<id>/artifacts[/<artifact_id>]` | the file tree and one file, path-traversal guarded |
| `GET /api/runs/<id>/image` · `/vision` · `/vision/raw` · `/vision/verifications` · `POST /vision/verify` | the drawing, the normalized boxes, the raw extraction, and second-pass checks |

---

## Export

| Endpoint | What is in it |
|---|---|
| `GET /api/runs/<id>/export` | The whole solution: `code/`, `audit/`, both images, and a `MANIFEST.md` written at download time |
| `GET /api/runs/<id>/export/code` | Only what the scaffold generated, hoisted to the archive root — for someone who wants to run it, not review it |
| `GET /api/runs/<id>/export/project` | Every file the run wrote **anywhere under the project root**, at its real path |
| `GET /api/runs/<id>/export/preview?kind=full\|code\|project` | The same plan as JSON. Downloads nothing |

**The MANIFEST is the product.** The code is reproducible; the reasoning is not. Its twelve
sections state which drawing this came from, what read it and how sure it was, the components and
connections as read, every second-pass verification, the assumptions made, the gaps left open,
the human decision, what each stage did, the generated code, how to run it, and a full inventory
with sha256 per file. When a section is empty it says why. Section 12 unions three sources — a
filesystem snapshot at run start, the stage-4 manifest, and the run-named directories — and prints
the whole heuristic **including what it cannot prove**. See [`app/projectdiff.py`](app/projectdiff.py).

---

## Running locally

```bash
python3 -m venv ../.venv && source ../.venv/bin/activate   # Python 3.10 or newer
pip install -r requirements.txt
export ARCH2CODE_PYTHON="$(command -v python)"
./run.sh                                                   # http://127.0.0.1:8765
```

Four prerequisites, and each one is a real failure mode rather than a formality. Only the first is
unconditional:

| Requirement | Needed for | Why |
|---|---|---|
| Python **3.10+** with `fastapi`, `uvicorn`, `pydantic`, `mcp`, `httpx`, `pillow` | everything | Point `ARCH2CODE_PYTHON` at it. The system `python3` on macOS is 3.9.6 and has none of them, which otherwise surfaces as an opaque MCP handshake timeout |
| **Node 26** | the pipeline | Bob re-executes itself with `--disable-sigusr1`; Node 20 rejects the flag and **every stage dies at exit 9 with an empty stdout**. Bob's own `engines` field says `>=20`, and it is wrong for the runtime |
| The `bob` CLI on `PATH` | the pipeline | The vision path and the whole UI work without it — the health report enables and disables the two capabilities independently |
| watsonx.ai credentials in `../mcp/arch_vision/.env` | the vision path | Copy `../mcp/arch_vision/.env.example` and fill it in. `.drawio` / `.puml` / `.mmd` run deterministically with no credentials and no tokens |

The health banner names whichever of those is missing and never blocks startup.

`run.sh` refuses to start if the interpreter cannot import `fastapi` and `uvicorn`, and prints the
exact `pip` line. It pins `--workers 1`, deliberately: run coordination is a per-run
`asyncio.Lock` inside one process, and a second worker would not see the first's locks. It binds
to loopback — Bob's licence (clause 53d) permits use by the licensee, its employees and its
contractors, and forbids providing hosting or a commercial service to third parties.

Credentials are read from two git-ignored files, purely to populate the environment of the child
processes the app spawns. Neither is ever written to, and no value from them leaves the process:

| File | Holds | Template |
|---|---|---|
| `../mcp/arch_vision/.env` | `WATSONX_APIKEY`, `WATSONX_PROJECT_ID` or `WATSONX_SPACE_ID`, `WATSONX_URL`, `WATSONX_VISION_MODEL_ID` | `../mcp/arch_vision/.env.example` |
| `webapp/.env` | `BOBSHELL_API_KEY`, plus any `ARCH2CODE_*` override | `webapp/.env.example` |

An `.env.example` is a versioned template and **must never carry a value**. This is not
hypothetical: a real IAM apikey once sat in a versioned `.env.example`. The smoke test now sweeps
for it, and [`.gitignore`](../.gitignore) excludes both `.env` files by name.

### Environment variables

Every knob is an environment variable, and the real process environment always wins over a `.env`
file, so exporting in your shell is enough.

| Variable | Default | Effect |
|---|---|---|
| `ARCH2CODE_PROJECT_ROOT` | parent of `webapp/` | Repo root. Everything resolves against it; `run.sh` exports it |
| `ARCH2CODE_BOB_CWD` | `ARCH2CODE_PROJECT_ROOT` | Working directory for every Bob subprocess. **Load-bearing** — it decides which chat modes exist. Point it at a scratch clone to keep run artifacts out of this checkout |
| `ARCH2CODE_BOB_BIN` | `bob` on `PATH` | `bob`, or `node /abs/path/bob.js`. A bare `.js` path gets `node` prepended. Never run through a shell |
| `ARCH2CODE_BOB_PTY` | `0` | Force the PTY output strategy instead of pipes |
| `ARCH2CODE_BOB_MAX_COINS` | unset | Passed through as `--max-coins`; Bob exits 1 when exceeded. Empty omits the flag |
| `ARCH2CODE_BOB_ACCEPT_LICENSE` | `1` | Pass `--accept-license`. A non-interactive run cannot answer the prompt; without it the process exits 1 with empty stdout |
| `ARCH2CODE_BOB_AUTH_METHOD` | `api-key` | Value for `--auth-method`. Empty omits the flag |
| `ARCH2CODE_PYTHON` | `/opt/anaconda3/bin/python` | Interpreter for the MCP server and every helper script |
| `ARCH2CODE_MAX_UPLOAD_MB` | `25` | Upload ceiling; over it returns 413 |
| `ARCH2CODE_STAGE_TIMEOUT_S` | `1800` | Wall clock per Bob stage |
| `ARCH2CODE_VISION_TIMEOUT_S` | `180` | Wall clock per MCP vision call |
| `ARCH2CODE_SSE_HEARTBEAT_S` | `15` | Seconds between SSE heartbeat comments |
| `ARCH2CODE_MAX_CONCURRENT_PIPELINE_RUNS` | `1` | One expensive subprocess at a time, by policy. Vision preview runs are not limited |
| `ARCH2CODE_HOST` | `127.0.0.1` | Bind address |
| `ARCH2CODE_PORT` | `8765` | Port |
| `ARCH2CODE_LOG_LEVEL` | `INFO` | Server log verbosity |

Independently of the table above, a Bob stage that emits no line for **180 s** is killed by the
stall watchdog. That is a separate ceiling from `ARCH2CODE_STAGE_TIMEOUT_S`, and it is the one
that fires when the account budget is exhausted.

### Health check

| Probe | Blocks | Checks |
|---|---|---|
| `project_root` | both | `.bob/` and `mcp/` are where they should be |
| `runs_writable` | both | `runs/` and `uploads/` are writable |
| `bob_binary` | pipeline | Bob executes at all |
| `bob_version` | — | informational; verified against 1.0.6 |
| `bob_chat_modes` | pipeline | **all six `arch2code` slugs appear in `--help` with `cwd=bob_cwd`** |
| `gate_string` | — | warns if `VERDICT: APPROVED`/`BLOCKED` vanished from the harness |
| `python_interpreter` | vision | `ARCH2CODE_PYTHON` imports `mcp, httpx, pydantic, PIL` |
| `pillow` | vision | image normalization is possible |
| `watsonx_env` | vision | credentials present — **names only, never values** |
| `mcp_server` | vision | stdio handshake plus the four `arch_vision` tools |
| `deterministic_scripts` | vision | the three helper scripts are readable |

`bob_chat_modes` is the most informative probe in the app: it proves in one shot that the binary
runs, that the working directory is right, and that `custom_modes.yaml` is loading.

### Tests

```bash
cd webapp && python -m pytest tests -q      # 350 tests
```

Run them from `webapp/`, not from the repository root — the suites import `app.*` and rely on that
being the working directory. No plugins beyond `pytest`, no network, no fixtures needing
credentials. They cover NDJSON tolerance (truncated lines, unknown types, non-JSON noise),
`--help` parsing against recorded fixtures from both working directories, gate parsing including
the historical verdicts that lack the string entirely, the event log's id/replay/concurrency
guarantees, the export planner, format ingestion, bbox normalization, and the fallback AIR —
validated against the schema on disk and then run through the real `validate_air.py --gate` as a
subprocess, requiring exit 1.

---

## Layout

```
webapp/
  run.sh              single entry point; pins --workers 1
  app/
    main.py           the FastAPI app factory and startup wiring
    config.py         env → frozen Settings; bob binary parsing; dotenv loading
    models.py         every pydantic shape, shared by both sides
    errors.py         AppError hierarchy; code/title/detail/remedy on everything
    eventlog.py       append-only JSONL, monotonic ids, replay + async tail
    sse.py            text/event-stream framing, Last-Event-ID, heartbeats
    health.py         the probes above
    store.py          run ids, run directories, uploads
    bobcli.py         --help probing, argv construction, approval policy
    bobproc.py        subprocess driver: pipe and pty strategies, stall watchdog
    ndjson.py         tolerant stream-json normalizer + the delta coalescer
    prompts.py        the exact prompt sent to each stage
    pipeline.py       stage table, gate parsing, the runner
    air_fallback.py   the deterministic degraded AIR — pure, no I/O, no model
    vision.py         MCP stdio client for arch_vision
    scripts.py        wrappers for the deterministic helper scripts
    artifacts.py      artifact resolution with a path-traversal guard
    export.py         the three archives, streamed, with MANIFEST.md
    projectdiff.py    fs snapshot at run start; the diff the project export uses
    ingest/           format adapters: image/HEIC, PDF, drawio, SVG, Visio (vsdx), textual
    routing.py        mirrors capture_diagram.route()
    api/              the HTTP surface, all under /api
  static/
    index.html        the screens, no framework
    js/views/         landing, run, health
    js/components/    diagram (the bounding boxes), reasoning, toolcall, gate,
                      stagetrack, artifact, overlay
    css/  vendor/     hand-written styles, vendored fonts, no build step
  tests/              350 tests, no network
  runs/               run state (git-ignored)
  uploads/            uploaded diagrams (git-ignored)
```
