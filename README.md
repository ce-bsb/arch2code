# arch2code

**An architecture drawing becomes a reviewed, working solution — and every generated file can
be traced back to the region of the drawing it came from.**

You upload a napkin photo, a whiteboard shot, a screenshot or a `.drawio` file. Five gated
stages of an IBM Bob pipeline read it, write down what is actually drawn, argue with
themselves about it, stop and ask a human, and only then generate code. If the drawing does
not say something, the pipeline does not invent it — it asks.

---

## Watch it run

### **https://bob-arch2code.2clgzcr0unhv.us-south.codeengine.appdomain.cloud**

Open that link and watch the real thing run. Drop a drawing on the page and you watch the model reason, tool call by tool call, until it stops at the human gate and asks you to
decide. 

<a href="https://github.com/ce-bsb/arch2code/raw/main/docs/videos/arch2code-github-home-animation.mp4"><img src="docs/videos/arch2code-github-home-animation.gif" alt="arch2code demo — drop a drawing and watch the pipeline run to the human gate" width="100%"></a>

> ▶ The clip above **plays and loops automatically** (≈3× speed). **[Watch the full-length HD recording](https://github.com/ce-bsb/arch2code/raw/main/docs/videos/arch2code-github-home-animation.mp4)** — the same run, in real time, from an empty drop zone to the human gate.

Every run anyone starts spends real quota. Runs are capped per stage and only one pipeline run
executes at a time, which is enough for a demonstration and is not a security model. If the link
does not answer, [running it locally](#run-it-yourself) is one command.

Everything the browser does, the pipeline also does from inside the Bob IDE with the
`🔀 arch2code` orchestrator mode. **The web application is a second front door onto the same
configuration, not a reimplementation of it** — see [`webapp/README.md`](webapp/README.md).

---

## The problem

Almost every technical idea starts as a drawing — a whiteboard, a napkin, a slide in a
workshop deck. Turning that drawing into something that runs is hand work: components,
integrations, contracts, tests, docs. In our own experience it takes about five weeks of one
engineer for a solution the size of the ones in this repository.

The cost is not only the weeks. It is what happens *during* the weeks:

- **The arrow read backwards.** Someone transcribes the diagram and gets a direction wrong.
  Everything downstream is built on it, and it is invisible until integration.
- **The component that was never drawn.** A model — or a person — fills a gap with whatever
  makes sense. What makes sense is usually right, which is exactly what makes the few wrong
  ones undetectable in review.
- **Spec drift.** The implementer hits an inconsistency and edits the specification to match
  the code just written. Every test passes, because the contract and the implementation now
  agree with each other — and neither matches anybody's drawing.
- **No provenance.** Months later nobody can answer *which part of the drawing produced this
  file*, or *which model read the drawing, when, with what prompt, at what confidence*. At a
  regulated client that is not a curiosity; it is an audit finding.

By the time the transcription is done, the momentum that produced the drawing is gone.

arch2code attacks the transcription, not the thinking. The estimate in our challenge
submission is ~200 h → ~8 h for the same deliverable, of which 4.25 h are a human deciding
rather than transcribing. **That figure is our own baseline for our own work, not a measurement
of this repository** — see [ROI and business outcomes](#roi-and-business-outcomes) for how it is
derived and what it does and does not claim.

---

## The core innovation: giving Bob eyes, auditably

IBM Bob is a coding agent. **It cannot see an image.** Its context mentions reject binary files
("binary files not supported"), `read_file` extracts *text* from a `.pdf` or `.docx` but never
interprets pixels, and its tool list — `read / search / list / write / apply_diff /
execute_command / use_mcp_tool / switch_mode / ask_followup_question` — contains no vision tool
at all. Dragging `@drawing.png` into the chat and asking "explain this architecture" simply does
not work.

The **only** supported path for pixels to enter Bob is the `mcp` tool group and `use_mcp_tool`.
So arch2code ships **`arch_vision`**, a local MCP server that exposes watsonx.ai multimodal
inference as four Bob tools. That is the whole novelty, and it has a consequence worth more than
the convenience it replaces:

> Because the drawing is read through an explicit, versioned tool call instead of a chat
> attachment, every reading is **auditable** — a pinned model, a versioned prompt, a confidence
> per element, and a stored record of exactly which bytes the model saw. Dragging an image into
> a chat window can tell you none of that.

**Any diagram, any image, any stack.** The intake stage routes by file type. A structured
source (`.drawio`, `.puml`, `.mmd`, `.xml`, `.md`, `.json`, `.yaml`) takes a **deterministic
parser path — zero tokens, nothing inferred**, and always wins over an image of the same
drawing. A raster (`.png .jpg .jpeg .webp .heic .bmp .tif`) or a scanned PDF goes down the
vision path. And the *output* platform is a contract input, not a hard-coded target: the same
pipeline emits watsonx Orchestrate ADK, LangGraph, CrewAI, a container microservice, mainframe
COBOL or an RPA flow — see [target platforms](#target-platforms).

---

## The architecture of the solution

<img src="docs/images/architecture.svg" alt="arch2code pipeline architecture, left to right: an Input column listing napkin photo, whiteboard, screenshot and a highlighted .drawio/.puml box marked deterministic - 0 tokens, feeding .arch/intake/inbox/; a Two ways in column with the IBM Bob IDE running the arch2code orchestrator mode and the web application; a Use cases tested box; all of it entering a dashed IBM BOB - PROJECT WORKSPACE boundary governed by .bob/custom_modes.yaml. Inside the boundary, an MCP server arch_vision lane across the top exposing arch_vision_list_intake, describe_diagram, extract_architecture and verify_element, wired to a black watsonx.ai box running llama-4-maverick-17b-128e-instruct-fp8, multimodal, 2 passes per arrow. Below it the five gated stages, one card each: 1 Intake arch-intake writing .arch/intake/, 2 Context arch-analyst writing .arch/air/ air.json THE CONTRACT, 3 Critique arch-critic writing .arch/review/ VERDICT APPROVED or BLOCKED, 4 Scaffold arch-scaffold writing everything except .arch/{intake,air,review}, 5 Validate arch-validator writing tests and .arch/run/. A dotted magenta loopback runs from the critic back to the analyst labelled VERDICT: BLOCKED. A foundation strip lists the shared Bob assets committed to Git: .bob/rules/ and rules-slug/, four skills, air.schema.json with eight automatic gates, and .bob/mcp.json. A black callout reads WRITE SCOPE IS A PERMISSION, NOT AN INSTRUCTION and prints the arch-scaffold fileRegex. On the right, an Output box titled whatever the drawing is, listing agentic, enterprise/legacy and cloud-native targets plus contracts, tests, README, Makefile and manifest.json, and under it an EVERY FILE, TRACEABLE block showing the generated arch2code header comment" width="100%">

Read it left to right.

**Input — the left column.** Anything that carries an architecture: a napkin photo, a
whiteboard shot, a screenshot, a PDF page, or a structured source. The highlighted box takes the
deterministic parser path. Everything lands in `.arch/intake/inbox/`.

**Two ways in.** The Bob IDE with the `🔀 arch2code` orchestrator mode, or the web application.
Same modes, same rules, same artifacts on disk. Under them, the box of use cases the pipeline
has actually been run against — card servicing with multiple agents, price research and
analysis, investment advisory, portfolio recommendation — and the note that one of them was
**BLOCKED** by the critic rather than generated.

**The workspace boundary.** The dashed rectangle is one Bob project workspace, governed by a
single file, [`.bob/custom_modes.yaml`](.bob/custom_modes.yaml). That file *is* the product:
six modes, their roles, their rulebooks, their tool groups and their write scopes.

**The MCP lane, across the top.** `arch_vision` — a local stdio MCP server exposing watsonx.ai
multimodal inference as four Bob tools. The solid arrow goes down into intake; the **dotted** one
goes into the critic, labelled *verify, not re-extract* — the second pass the whole project rests
on. [Full detail below.](#the-arch_vision-mcp-server)

**The five gated stages.** One card per stage: the mode slug, what it is for, the tool groups it
was granted, and the directory it is allowed to write. They chain **by file, not by session** —
each stage reads the previous stage's artifact by absolute path, in a clean Bob session. Note the
fourth card: `arch-scaffold` has no `mcp` group at all. **The code-writing mode cannot call
vision**, by construction; it can only read the contract.

**The loopback.** The dotted magenta line from the critic back to the analyst is
`VERDICT: BLOCKED`. It carries concrete questions for the human. *"Approved with caveats"* is
explicitly forbidden in the critic's rubric.

**Shared Bob assets, the strip underneath.** [`.bob/rules/`](.bob/rules/) and
`.bob/rules-<slug>/` (global and per-mode rubrics, auto-loaded), four skills under
[`.bob/skills/`](.bob/skills/), the AIR JSON Schema with its automatic gates, and
[`.bob/mcp.json`](.bob/mcp.json) — which carries no credentials and no absolute paths. All
committed to Git, so the tenth engineer to clone the repository inherits the same pipeline as the
first.

**Output — the right column.** Whatever the drawing is: agentic (Orchestrate ADK, LangGraph,
CrewAI), enterprise and legacy (RPA, mainframe COBOL), cloud-native (container microservice) —
plus, always, contracts, tests, a README, a Makefile and a `manifest.json` mapping component id →
files.

### The five stages, in one table

| # | Stage | Bob mode | What it does | Tool groups | Writes to |
|---|---|---|---|---|---|
| 0 | Orchestrate | `arch2code` | Runs the state machine, reads the gates, records the audit trail. Replaced by the web app when driving from a browser. | `read edit skill` | `.arch/run/*.md` |
| 1 | Intake | `arch-intake` | Reports what **is** drawn, with evidence and a confidence per element. Never completes an arrow. | `read edit command mcp skill` | `.arch/intake/` |
| 2 | Context | `arch-analyst` | Turns raw extraction into the AIR contract, separating observed from inferred from unknown. | `read edit command mcp skill` | `.arch/air/`, `.arch/intake/` |
| 3 | Critique | `arch-critic` | Adversarial review. Re-reads doubtful elements against the drawing. Writes `VERDICT: APPROVED` or `VERDICT: BLOCKED`. | `read edit command mcp skill` | `.arch/review/` |
| 4 | Scaffold | `arch-scaffold` | Implements the AIR and nothing but the AIR. Every file carries a traceability header. | `read edit command skill` | everything **except** `.arch/{intake,air,review}/` |
| 5 | Validate | `arch-validator` | Runs the experiment and reports which hypotheses survived. | `read edit command skill` | `tests/`, `.arch/run/` |

Run ids are canonical: `YYYYMMDD-HHMM-<short-slug>`, matching `^[0-9]{8}-[0-9]{4}-[a-z0-9-]+$`.
The same id names `.arch/{intake,air,review,build,run}/<run>/`, which is what makes an audit
trail navigable a year later.

---

## The `arch_vision` MCP server

The heart of the innovation, in [`mcp/arch_vision/`](mcp/arch_vision/). It is **1,413 lines**
across four programs — the server itself and three diagnostics — because a tool that will not run
inside Bob without a working watsonx.ai account, a reachable model and a correct interpreter has
to be able to *tell you which one is broken*.

| File | Lines | What it is |
|---|---|---|
| [`server.py`](mcp/arch_vision/server.py) | 616 | The stdio MCP server: 4 tools, pinned model, versioned prompts, quality gate |
| [`preflight.py`](mcp/arch_vision/preflight.py) | 384 | 6 diagnostic gates + empirical multimodal probing of your own catalog |
| [`run_image.py`](mcp/arch_vision/run_image.py) | 254 | End-to-end smoke test over a real MCP handshake, against a ground truth |
| [`configure_bob.py`](mcp/arch_vision/configure_bob.py) | 159 | Writes `.bob/mcp.json` with an interpreter *proven* to import `mcp` |

### The four tools

| Tool | Purpose | Used by |
|---|---|---|
| `arch_vision_list_intake` | Lists artifacts in the inbox and the extraction path chosen for each (deterministic vs vision) | intake |
| `arch_vision_describe_diagram` | Free-form technical description of an image (exploration only) | — |
| `arch_vision_extract_architecture` | **Structured** extraction: components, connections, evidence, per-element confidence, quality flags | intake / analyst |
| `arch_vision_verify_element` | Independent verification of **one** claim, under a sceptical prompt | critic |

**The split between `extract` and `verify` is the mechanism.** `extract` asks *"what do you
see?"* — a framing that rewards completing the picture. `verify` asks *"someone may have read this
wrong; does this arrow really exist?"* — a framing that rewards doubt. Different prompt, different
framing, **decorrelated error.** Asking the same question twice only confirms the same bias, which
is why "tell the model to review its own answer" pays so little. The critic therefore calls
`verify`, never `extract` again.

### What makes each reading auditable

- **Pinned model.** `meta-llama/llama-4-maverick-17b-128e-instruct-fp8` by default
  (`WATSONX_VISION_MODEL_ID`), talking to the watsonx.ai chat API (`version=2024-10-08`) — not
  "whatever the chat happened to route to".
- **Versioned prompts.** Every structured result is stamped `extract@1.1`; every verification is
  stamped `verify@1.1`. Change the prompt, change the version, and old artifacts still say which
  prompt produced them.
- **Per-element confidence** and an explicit `_quality` block naming the connections that need a
  second pass, the broken references, and the action required.
- **Self-configuration, no secret in a versioned file.** The server reads the `.env` sitting next
  to it and derives the project root from `__file__`. Consequence: `.bob/mcp.json` carries **no
  credential and no machine-specific path**, so it is safe to commit and share.
- **Guardrails before the token is spent.** A `>5 MB` image is refused with the reason; a wrong
  `model_id` returns a 404 that *names the catalog endpoint to consult*; a timeout says which knob
  to turn. Non-image extensions never reach the model at all.

### Prove it on your own account

```bash
python3 mcp/arch_vision/preflight.py            # 6 gates: credentials, scope, IAM, catalog, model, images
python3 mcp/arch_vision/preflight.py --probe    # which catalogued models actually accept an image
python3 mcp/arch_vision/preflight.py --extract  # run the fixture against tests/ground-truth-example.json
```

`--extract` is the only test that answers *does vision work in MY catalog*. It scores the
extraction against a ground truth that includes a **trap line** — a bare line with no arrowhead —
and asserting a direction on it is a critical failure, not a warning. `configure_bob.py` then
writes `.bob/mcp.json` pointing at an interpreter that has *proven* it can import `mcp`, `httpx`
and `pydantic` — using `.absolute()` and never `.resolve()`, because resolving follows a venv
symlink to the system `python3` that cannot see the venv's packages.

---

## What each stage delivers

A stage is not "a prompt that ran". It is an artifact on disk, an exit criterion that artifact
has to meet, and a defined thing the next stage is allowed to read. That is what makes the
pipeline restartable at any stage, and auditable after the fact.

| Stage | The artifact it produces | Exit criterion | What the next stage receives |
|---|---|---|---|
| **1 · Intake**<br>`arch-intake` | `.arch/intake/<run>/extraction.json` + `capture-manifest.json` | Every element carries a `confidence` and an `evidence` object — a `bbox` for vision, an mxCell id for `.drawio`. Anything illegible becomes an entry in `unknowns[]`, **never a low-confidence guess**. On a hand-drawn source, every connection under 0.8 confidence has been through a second pass | The raw structure as drawn, plus the provenance of the reading itself: which model, which prompt version, which extraction path, and the sha256 and rescaling of the image the model actually saw |
| **2 · Context**<br>`arch-analyst` | `.arch/air/<run>/air.json` — **the contract** | Validates against `air.schema.json` (JSON Schema 2020-12, `additionalProperties: false` at every level). Observed, inferred and absent are in three separate arrays. Every assumption declares an `impact`. `experiment_plan` carries at least one falsifiable hypothesis and `out_of_scope` is not empty | One file. From here on, nothing downstream reads the drawing again — the AIR is the single source of truth, and stage 4 is permitted to read nothing else |
| **3 · Critique**<br>`arch-critic` | `.arch/review/<run>/verdict.md` | The last non-empty line is **exactly** `VERDICT: APPROVED` or `VERDICT: BLOCKED`. No third value, no *"approved with caveats"* — a caveat has to become an `unknowns[]` entry instead. The critic may not edit the AIR it is reviewing | Either permission to generate, or a ranked list of findings — each naming the element id and the correction it wants — plus closed questions routed back to the analyst and to the human |
| **4 · Scaffold**<br>`arch-scaffold` | The code tree + `.arch/build/<run>/manifest.json` | Refuses to write a single file unless `verdict.md` says `APPROVED`. Every generated file opens with the traceability header. The manifest maps `component_id → [files]`. Every external dependency has a local stub, so the tree runs offline. An unimplemented handler raises `NotImplementedError` naming the AIR id — never a silent `pass` | A tree that comes up on its own and can be executed, and a manifest that answers *"where did this file come from?"* without asking anybody |
| **5 · Validate**<br>`arch-validator` | `.arch/run/<run>/validation.md` | Each hypothesis from the AIR is marked VERIFIED, REFUTED or INCONCLUSIVE, with the exact command and the real output pasted in. Changing the architecture to make a test pass is forbidden — if the fix needs a new component, the AIR is wrong and the run goes back to stage 2 | The human architect, who gets back the list of things the prototype discovered that the original drawing did not account for.<br>**Not yet demonstrated — see [what has not been verified](#what-has-not-been-verified)** |

---

## The last successful run: a napkin becomes a working watsonx Orchestrate solution

Run `20260721-1759-supervisor-precos`. A ballpoint sketch on the left, the contract the pipeline
derived from it in the middle, and the code it generated on the right.

![Three panels. Panel 1, WHAT THE HUMAN MADE, source_kind napkin: a photographed ballpoint sketch of a stick figure talking to an Agente Supervisor box that contains plugin pre, LLM and plugin post; arrows fan out to two sub-agents, Agente de pesquisa de precos with the tool tool_google_search, and Agente Analista with tool_cria_tabela_comparativa and tool_gera_report_de_analise; three governance boxes sit underneath, wx gov guardrails/PII, LLM-Judge and redaction precos. Panel 2, THE CONTRACT BOB DERIVES, air.json: an OBSERVED section with 13 components and 9 connections each carrying evidence; an INFERRED section where every assumption declares its blast radius; and a MISSING section where the pipeline stopped and asked a human, showing u_protocol_agents with blocking true and the human answer "watsonx Orchestrate collaborator (native)". Panel 3, WHAT BOB GENERATED, watsonx Orchestrate ADK: the tree agents/supervisor-precos/ with three native agent YAMLs, five Python tools including plugin_pre_guardrails.py and plugin_post_redaction.py, six test modules 17 passing offline, a Makefile wired to the real orchestrate CLI, and requirements.txt, .env.example and README](docs/images/solution-example.png)

**Panel 1 — what the human made.** A photograph of a ballpoint sketch. A stick figure, a
supervisor box holding *plugin pre / LLM / plugin post*, two sub-agents with their tools, and
three governance boxes underneath. There is no schema in it, and roughly half of what the
generated code needs is simply not on the paper. That gap is the entire problem.

**Panel 2 — the contract Bob derived** ([`.arch/air/20260721-1759-supervisor-precos/air.json`](.arch/air/20260721-1759-supervisor-precos/air.json)),
split into three categories that never mix:

- **Observed** — **13 components and 9 connections**, each with an `evidence` object naming the
  bounding box it was read from. `agente_supervisor` at confidence 0.99, with its box.
- **Inferred** — **4 assumptions**, each declaring its blast radius. That the target platform is
  watsonx Orchestrate carries the impact *"If wrong, the agent YAML format and the tool import
  mechanism both change"* — the sentence that tells a reviewer whether to care.
- **Missing** — **5 unknowns**. `u_protocol_agents` was `blocking: true` and **stopped the run**
  until a person answered *"What protocol calls the sub-agents?"* with *"watsonx Orchestrate
  collaborator (native)"*. Four more were non-blocking and **shipped open rather than guessed**.

`overall_confidence` is **0.91**. The pipeline's most valuable output on this run was a question.

**Panel 3 — what Bob generated**, committed at [`agents/supervisor-precos/`](agents/supervisor-precos/):
three flat ADK-native agent YAMLs, five Python tools including the two governance plugins, six
test modules, a Makefile driving the real `orchestrate` CLI, `requirements.txt`, `.env.example`
and a README — **19 files**, mapped component → file in
[`.arch/build/20260721-1759-supervisor-precos/manifest.json`](.arch/build/20260721-1759-supervisor-precos/manifest.json).
The supervisor's `instructions` block encodes the order the human drew — guardrails first,
redaction last, never skip either — because that order was in the drawing and therefore in the
contract. The tests run with no credentials and no network:

```bash
cd agents/supervisor-precos && python3 -m pytest tests -q   # 17 passed
```

That is the whole argument. A drawing on paper, a contract that says what it knows and what it
does not, and code that traces back to the box it came from.

---

## When the gate bites: a run that was correctly BLOCKED

The counter-example matters as much as the success, because a pipeline that always generates
code has no gate. Run `20260722-0528-modeb2`, executed end to end **through the web app**.

Input: `exemplo-rascunho.png`, a hand-drawn sketch with five boxes and four lines — one drawn as
a bare line labelled `?`, with no arrowhead.

| Stage | Mode | Exit | NDJSON lines | Duration |
|---|---|---|---|---|
| 1 intake | `arch-intake` | 0 | 1,525 | 127 s |
| 2 analyst | `arch-analyst` | 0 | 1,084 | 114 s |
| 3 critic | `arch-critic` | 0 | 1,309 | 111 s |
| 4 scaffold | `arch-scaffold` | — | — | **never started** |

Stage 1 read the trap line as a directed connection and gave it `confidence: 0.45`. Stage 2
carried it into the AIR with `verified_by_second_pass: false`. Stage 3 issued one claim to
`arch_vision_verify_element`:

> **Claim tested:** *"Is there a directed arrow (with an arrowhead) from Svc Pedidos to Notificacao?"*
> **Verdict:** FALSE · **Confidence:** 1.0
> **Observed:** *"There is a line connecting Svc Pedidos to Notificacao, but it is labeled with '?' and does not have a clear arrowhead."*

The other three connections were verified TRUE at confidence 1.0 in the same pass. The critic
wrote `VERDICT: BLOCKED` as the last line of
[`.arch/review/20260722-0528-modeb2/verdict.md`](.arch/review/20260722-0528-modeb2/verdict.md),
and **the pipeline stopped before generating a single line of code.** Cost of finding out:
**1,184,071 input tokens, 14,955 output tokens, 2.9976 Bobcoin, 336 s of stage time, 27 tool
calls.**

The full run — events, timeline, gate decision — is browsable under
[`webapp/runs/20260722-0528-modeb2/`](webapp/runs/20260722-0528-modeb2/). **The pipeline stopping
and asking is the product working.** Guessing right would have been the worst outcome, because it
teaches you to trust the guess.

---

## The AIR contract

Everything downstream derives from one file: `.arch/air/<run>/air.json`, the **Architecture
Intermediate Representation**, validated against a JSON Schema 2020-12 document with
`additionalProperties: false` at every level
([`.bob/skills/air-normalizer/air.schema.json`](.bob/skills/air-normalizer/air.schema.json)).

Its whole job is to keep three categories from mixing:

| Category | Goes to | Mandatory field | Admission question |
|---|---|---|---|
| **Observed** — it is in the drawing | `components[]`, `connections[]` | `evidence` — a normalised `bbox` or a drawio `cell_id` | *"which pixel or cell is this?"* No answer, no entry. |
| **Inferred** — the model deduced it | `assumptions[]` | `impact` (min 10 chars) | *"what breaks in the code if this is wrong?"* No answer, it becomes an unknown. |
| **Absent** — nobody knows | `unknowns[]` | `question` + `options` + `blocking` | It becomes a closed question for a human. Never a guess. |

Migrating an item between the three in order to "complete" the AIR is prohibited in five
separate files. This is the mechanism that turns *"the model is usually right"* from a comfort
into a measurable risk: an inference is labelled as one, forever, in the artifact that generates
the code.

[`validate_air.py --gate`](.bob/skills/air-normalizer/scripts/validate_air.py) applies **eight
automatic blocks**: (1) an open blocking unknown; (2) `overall_confidence` below 0.75; (3) a
connection referencing a component that does not exist; (4) an assumption with no declared impact;
(5) a hand-drawn connection under 0.85 confidence never second-passed; (6) a synchronous cycle in
the connection graph; (7) no falsifiable hypothesis; (8) an empty `out_of_scope`.

```bash
python3 .bob/skills/air-normalizer/scripts/validate_air.py \
        .bob/skills/air-normalizer/example-air.json           # exit 0 — schema and semantics
python3 .bob/skills/air-normalizer/scripts/validate_air.py \
        .bob/skills/air-normalizer/example-air.json --gate    # exit 1, one ERROR — the missing arrow
```

`jsonschema` being absent degrades the schema layer to a warning that does not affect the exit
code, which is how the two oldest runs in this repository passed with structurally invalid AIRs.
Install it.

**When a stage degrades instead of failing.** If the Bob budget runs out, the backend stops
answering with no error and the stall watchdog kills the stage. If that hits stage 2, the run
used to die holding a perfectly good `extraction.json`.
[`webapp/app/air_fallback.py`](webapp/app/air_fallback.py) instead derives the part of the AIR
that is a **pure transform** of what is in hand and refuses to author the rest: `assumptions[]`
stays `[]`, a blocking unknown states nothing was reasoned about, and `meta.extractor` says in
words that the analyst did not run. Verified: **schema-valid (exit 0) and rejected by the gate
(exit 1)**. Detail in [`webapp/README.md`](webapp/README.md#the-degraded-stage-2-path).

---

## Why five modes and not one prompt

Because a prompt cannot revoke a capability, and a file permission can.

Every mode's `edit` group carries a `fileRegex` that pins it to its own stage's artifact. These
are the literal patterns in [`.bob/custom_modes.yaml`](.bob/custom_modes.yaml):

| Mode | `fileRegex` |
|---|---|
| `arch2code` | `^\.arch/run/.*\.md$` |
| `arch-intake` | `^\.arch/intake/.*\.(json\|md)$` |
| `arch-analyst` | `^\.arch/(air\|intake)/.*\.(json\|md\|ya?ml)$` |
| `arch-critic` | `^\.arch/review/.*\.md$` |
| `arch-scaffold` | `^(?!\.arch/(intake\|air\|review)/).*$` |
| `arch-validator` | `^(tests?/\|.*[._-](test\|spec)\.\|\.arch/run/.*\.md$\|docker-compose\|Makefile)` |

The scaffold pattern is the interesting one: **a negative lookahead anchored at position 0.** It
grants the whole repository *except* three directories. The mode that writes the code cannot edit
the specification it implements, and cannot edit the review that approved it. That is the barrier
against spec drift, and it is the reason the pipeline is six modes rather than one long prompt.

**The honest limit of that claim:** `fileRegex` constrains the mode's **edit tools**, not the
`command` group, and the scaffold has `command`. A shell command can write anywhere the process
can. The barrier is real against the failure that actually happens — a model deciding the spec is
wrong and adjusting it — and it is not a sandbox. It is stated here rather than in a footnote
because a tool whose argument is traceability does not get to overstate its own guarantees.

Two more consequences: the orchestrator has neither `command` nor `mcp`, so it verifies nothing
on its own; and Bob raises no error for an unknown group name — a mode that asks for a group that
does not exist silently loses that access. Bob found that defect in our own configuration:
`arch-analyst` was told to run the AIR validator but had never been granted `command`. The stage
would have "passed" without validating anything.

---

## Target platforms

The generator does not know what watsonx Orchestrate is. It reads a **profile**: a declarative
capability contract stating what a target can express, what it refuses and why, which artifact
each AIR component kind becomes, what it must ask the human, and what can be validated offline.
Adding a target means [adding a directory](.bob/skills/scaffold-from-air/profiles/). It never
means editing the engine.

| Profile | Status | Offline validation | Refuses |
|---|---|---|---|
| [`orchestrate-adk`](.bob/skills/scaffold-from-air/profiles/orchestrate-adk/) | verified | full | infrastructure — databases, queues and storage go behind a tool |
| [`langgraph`](.bob/skills/scaffold-from-air/profiles/langgraph/) | documented | full | UI, brokers, schedules; a database is a checkpointer, not a node |
| [`crewai`](.bob/skills/scaffold-from-air/profiles/crewai/) | documented | full | same agentic family, different runtime |
| [`container-microservice`](.bob/skills/scaffold-from-air/profiles/container-microservice/) | verified | full | almost nothing — the fallback when a specialised target says no |
| [`mainframe-cobol`](.bob/skills/scaffold-from-air/profiles/mainframe-cobol/) | documented | structural-only | caches, functions, object storage, every protocol that is not a file |
| [`rpa`](.bob/skills/scaffold-from-air/profiles/rpa/) | documented | structural-only | infrastructure, and direct database access on principle |
| [`llm-finetuning`](.bob/skills/scaffold-from-air/profiles/llm-finetuning/) | documented | structural-only | anything that is not a data/eval pipeline |

`status` is a claim about the artifact contract; `validation` is a claim about what can be proven
with no platform attached. `orchestrate-adk` is `verified` because its field lists were read by
introspection out of an installed `ibm-watsonx-orchestrate 2.12.0`, not transcribed from docs —
every statement in a profile is tagged `[VER]`, `[DOC]`, `[INF]` or `[NV]`.

Before any code is generated, `negotiate` intersects the drawing with the target and returns one
of four verdicts: **REFUSAL** (with the redraw that would work), **QUESTION**, **DOWNGRADE** (it
will be generated, but part of the intent becomes documentation), or **RESOLVED**. There is
deliberately no fifth verdict for *"generate something plausible"*.

```bash
S=.bob/skills/scaffold-from-air/scripts
python3 $S/target_engine.py list                                # every profile
python3 $S/target_engine.py doctor                              # what THIS machine can validate
python3 $S/target_engine.py negotiate <air.json> --profile rpa  # refuse before spending a token
```

---

## ROI and business outcomes

The task arch2code addresses is the routine, repeated one at the front of every solution
engagement: **turn an approved architecture drawing into a working solution someone can run.**

| | Before Bob | With Bob |
|---|---|---|
| Hands-on effort per solution | **~200 h** (≈5 weeks of one engineer) | **~8 h** |
| Of which, human *thinking* | most of it | **4.25 h** — deciding, not transcribing |
| Reduction | — | **~96 %** |

**How the 8 hours break down** (machine time is minutes; the rest is a human): capture the
drawing 0.25 h · intake 0.5 h · **answer the blocking unknowns 2.0 h** · read the verdict and
decide the gate 0.5 h · scaffold 1.5 h · run the experiment 1.0 h · **review the artifacts
2.25 h**.

**How the 200-hour baseline was derived.** It is our own measured effort for a solution the size
of the ones in this repository — a supervisor plus two or three collaborator agents, five tools,
governance and redaction plugins, a test suite, a Makefile and a README — once
architecture-review cycles, rework from a misread diagram, and documentation are counted. 200 h
was chosen from a range of 80–320 h as the figure matching our actual experience.

**Scaled out**, at roughly one solution per week and a US$100/h fully-loaded cost:

| Task (same pipeline, different moment) | Before | After | Per year | Saved / yr |
|---|---|---|---|---|
| **Solution build** — drawing → running solution | 200 h | 8 h | 48 | **9,216 h** |
| Re-sync after the client changes the diagram | 40 h | 4 h | 24 | 864 h |
| Handover / audit — "where did this file come from?" | 3 h | 0.1 h | 48 | 139 h |
| Proposal / SOW technical annex *(estimate)* | 12 h | 0.5 h | 24 | 276 h |
| **Per team, per year** | | | | **≈10,500 h ≈ US$1.05M** |

**Beyond the hours:** deal velocity (the client's own diagram runs the same week it was drawn,
before a build team is staffed); the technical annex writes itself as a pipeline by-product;
provenance that answers *which model read the drawing, when, at what confidence* is an audit
answer, not an archaeology project; and the pipeline is a folder in Git, so **the tenth team to
adopt it costs what the first did.**

> These are estimates against our own baseline, not an instrumented measurement of this
> repository. They are stated that way everywhere they appear.

---

## What has actually been verified

Everything in this section was executed. Reproduce any line of it.

| Claim | Evidence |
|---|---|
| The harness is internally consistent | `bash tests/smoke_test.sh` → **21 passed**, on an interpreter with `jsonschema`, `mcp`, `httpx` |
| The web app behaves as documented | `cd webapp && python -m pytest tests -q` → **350 tests**, no network, no credentials |
| The platform profiles load, refuse and validate what they claim | `python3 .bob/skills/scaffold-from-air/scripts/selftest.py` |
| The gate bites on a real run | run `20260722-0528-modeb2`: three stages exit 0, `VERDICT: BLOCKED`, stage 4 never started. Artifacts under [`.arch/{intake,air,review}/20260722-0528-modeb2/`](.arch/review/20260722-0528-modeb2/) |
| Second-pass verification contradicts the extraction when it should | same run: verdict FALSE at confidence 1.0 on the one connection that was not an arrow |
| The generated solution runs | `cd agents/supervisor-precos && python3 -m pytest tests -q` → **17 passed**, offline |
| The contract behind it says what the panel says | [`.arch/air/20260721-1759-supervisor-precos/air.json`](.arch/air/20260721-1759-supervisor-precos/air.json): 13 components, 9 connections, 4 assumptions, `overall_confidence` 0.91, one blocking unknown answered |
| Generated files are traceable to components | [`.arch/build/20250717-1200-investment-advisor/manifest.json`](.arch/build/20250717-1200-investment-advisor/manifest.json) maps component id → files → bbox evidence |
| The pipeline runs unattended | Bob driven headless as `bob --chat-mode <slug> --output-format stream-json`, one subprocess per stage |

---

## Run it yourself

### The web application, locally

```bash
git clone https://github.com/ce-bsb/arch2code && cd arch2code

python3 -m venv .venv && source .venv/bin/activate    # Python 3.10 or newer
pip install -r webapp/requirements.txt

export ARCH2CODE_PYTHON="$(command -v python)"        # the interpreter you just installed into
./webapp/run.sh                                       # http://127.0.0.1:8765
```

`run.sh` refuses to start if that interpreter cannot import `fastapi` and `uvicorn`, and prints
the exact `pip` line. It binds to loopback and pins one worker, deliberately.

Two of the four prerequisites are optional: the `bob` CLI is needed only to drive the pipeline,
and watsonx.ai credentials only for the vision path — `.drawio`, `.puml` and `.mmd` run
deterministically with neither. The app probes its own environment at startup and **never refuses
to start**; it names the failing check instead. Full prerequisite table, every environment
variable and a symptom → cause → fix table: [`webapp/README.md`](webapp/README.md).


### From inside the Bob IDE

```bash
bash tests/smoke_test.sh              # 21 checks, no network — run this first
cp ~/Desktop/whiteboard.jpg .arch/intake/inbox/
```

Then select **🔀 arch2code — Orchestrator** and tell it which artifact to process. Full
step-by-step with its own symptom → cause → fix table: [`INSTALL-GUIDE.md`](INSTALL-GUIDE.md).

---

## Repository layout

```
.bob/                                   the pipeline — this is the product
  custom_modes.yaml                     6 modes: role, rules, tool groups, fileRegex
  rules/                                loaded in every mode (the 7 guardrails)
  rules-arch-<slug>/                    loaded automatically only in that mode
  skills/
    diagram-intake/                     EXIF-correct capture, .drawio base64+deflate parser
    air-normalizer/                     air.schema.json + validate_air.py + the blocked fixture
    scaffold-from-air/
      profiles/                         7 platform contracts — add a directory, not code
      scripts/                          negotiate, target_engine, validate_adk, selftest
    experiment-harness/                 stage 5: run it, report what was refuted
  mcp.json                              arch_vision registration — no credentials, no absolute paths

mcp/arch_vision/                        the MCP server: 4 tools over stdio to watsonx.ai
  server.py                             pinned model, versioned prompts, quality gate
  preflight.py                          6 diagnostic gates + empirical multimodal probing
  configure_bob.py                      writes .bob/mcp.json with an interpreter proven to work
  run_image.py                          end-to-end smoke test over a real MCP handshake

webapp/                                 the abstraction layer — FastAPI + hand-written ES modules
  app/                                  see webapp/README.md for the module map
  static/                               no build step, no CDN, no bundler
  tests/                                350 tests, no network

deploy/                                 IBM Code Engine: Dockerfile, deploy.sh, DEPLOYMENT.md

tests/smoke_test.sh                     21 checks that must be green before opening Bob
tests/ground-truth-example.json         what the fixture really contains, including the trap

.arch/                                  the audit trail — evidence, not documentation
  intake/  air/  review/  build/  run/  one directory per run id, per stage
  README.md                             what each historical run did, and every defect in it

agents/                                 generated solutions, checked in as evidence
  supervisor-precos/                    the successful run above — 17 tests pass, offline
  investment-advisor/                   a second example, with its manifest (tests partial)

docs/images/                            architecture.svg, solution-example.png, the screenshots
docs/videos/                            the landing-page demo
INSTALL-GUIDE.md                        8 ordered steps, each isolating one class of failure
AGENTS.md                               the short contract for any agent working in this repo
```

