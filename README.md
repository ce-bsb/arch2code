# arch2code

**An architecture drawing becomes a reviewed, working solution — and every generated file can
be traced back to the region of the drawing it came from.**

You upload a napkin photo, a whiteboard shot, a screenshot or a `.drawio` file. Five gated
stages of an IBM Bob pipeline read it, write down what is actually drawn, argue with
themselves about it, stop and ask a human, and only then generate code. If the drawing does
not say something, the pipeline does not invent it — it asks.

---

## Try it

### **https://bob-arch2code.2clgzcr0unhv.us-south.codeengine.appdomain.cloud**

**Open that link and watch the real thing run.** There is nothing to install: no CLI, no Bob
licence, no IDE, no watsonx credentials of your own. Drop a drawing on the page and you watch
the model reason, tool call by tool call, until it stops at the human gate and asks you to
decide. The link is open — no sign-in, nothing to request — because a stranger should be able
to see the mechanism work in under a minute.

It also means every run anyone starts spends real quota. Runs are capped per stage and only one
pipeline run executes at a time, which is enough for a demonstration and is not a security
model. If the link does not answer, [running it locally](#run-it-yourself) is one command.

![The arch2code landing page: a large drop zone reading "Drop your architecture drawing here", a five-card strip titled WHAT HAPPENS NEXT covering intake, analyst, critic, your gate and scaffold, and an EARLIER RUNS table showing run 20260722-0528-modeb2 on exemplo-rascunho.png at 3/5 with the status AWAITING INPUT](docs/images/ui-01-landing.png)

Everything the browser does, the pipeline also does from inside the Bob IDE with the
`🔀 arch2code` orchestrator mode. The web application is a second front door onto the same
configuration, not a reimplementation of it.

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
of this repository**, and it is stated that way wherever it appears.

---

## The architecture of the solution

<img src="docs/images/architecture.svg" alt="arch2code pipeline architecture, left to right: an Input column listing napkin photo, whiteboard, screenshot and a highlighted .drawio/.puml box marked deterministic - 0 tokens, feeding .arch/intake/inbox/; a Two ways in column with the IBM Bob IDE running the arch2code orchestrator mode and the web application; a Use cases tested box; all of it entering a dashed IBM BOB - PROJECT WORKSPACE boundary governed by .bob/custom_modes.yaml. Inside the boundary, an MCP server arch_vision lane across the top exposing arch_vision_list_intake, describe_diagram, extract_architecture and verify_element, wired to a black watsonx.ai box running llama-4-maverick-17b-128e-instruct-fp8, multimodal, 2 passes per arrow. Below it the five gated stages, one card each: 1 Intake arch-intake writing .arch/intake/, 2 Context arch-analyst writing .arch/air/ air.json THE CONTRACT, 3 Critique arch-critic writing .arch/review/ VERDICT APPROVED or BLOCKED, 4 Scaffold arch-scaffold writing everything except .arch/{intake,air,review}, 5 Validate arch-validator writing tests and .arch/run/. A dotted magenta loopback runs from the critic back to the analyst labelled VERDICT: BLOCKED. A foundation strip lists the shared Bob assets committed to Git: .bob/rules/ and rules-slug/, four skills, air.schema.json with eight automatic gates, and .bob/mcp.json. A black callout reads WRITE SCOPE IS A PERMISSION, NOT AN INSTRUCTION and prints the arch-scaffold fileRegex. On the right, an Output box titled whatever the drawing is, listing agentic, enterprise/legacy and cloud-native targets plus contracts, tests, README, Makefile and manifest.json, and under it an EVERY FILE, TRACEABLE block showing the generated arch2code header comment" width="100%">

Read it left to right.

**Input — the left column.** Anything that carries an architecture: a napkin photo, a
whiteboard shot, a screenshot, a PDF page, or a structured source. The highlighted box is the
important one: `.drawio`, `.puml` and `.mmd` take the **deterministic parser path — zero
tokens, nothing inferred**, and a structured source always wins. Using vision where a parser
exists is treated as an engineering mistake, not a preference. Everything lands in
`.arch/intake/inbox/`.

**Two ways in.** The Bob IDE with the `🔀 arch2code` orchestrator mode, or the web
application. Same modes, same rules, same artifacts on disk. Under them, the box of use cases
the pipeline has actually been run against — card servicing with multiple agents, price
research and analysis, investment advisory, portfolio recommendation — and the note that one of
them was **BLOCKED** by the critic rather than generated.

**The workspace boundary.** The dashed rectangle is one Bob project workspace, governed by a
single file, [`.bob/custom_modes.yaml`](.bob/custom_modes.yaml). That file *is* the product:
six modes, their roles, their rulebooks, their tool groups and their write scopes.

**The MCP lane, across the top.** Bob does not ingest images: context mentions reject binary
files, `read_file` extracts text but does not interpret pixels, and there is no native vision
tool. The only supported path for pixels is the `mcp` tool group. So `arch_vision` is a local
MCP server (stdio transport) exposing watsonx.ai multimodal inference as four Bob tools —
`arch_vision_list_intake`, `arch_vision_describe_diagram`, `arch_vision_extract_architecture`,
`arch_vision_verify_element` — on a pinned model
(`meta-llama/llama-4-maverick-17b-128e-instruct-fp8`) with a versioned prompt stamped into
every result. The solid arrow goes down into intake; the **dotted** one goes into the critic,
and it is labelled *verify, not re-extract* — that is the second pass, and it is the mechanism
the whole project rests on. **The platform constraint is what produced the auditability.**
Dragging an image into a chat window cannot tell you which model read it.

**The five gated stages.** One card per stage: the mode slug, what it is for, the tool groups
it was granted, and the directory it is allowed to write. They chain **by file, not by
session** — each stage reads the previous stage's artifact by absolute path, in a clean Bob
session. Note the fourth card: `arch-scaffold` has no `mcp` group at all. **The code-writing
mode cannot call vision**, by construction; it can only read the contract.

**The loopback.** The dotted magenta line from the critic back to the analyst is
`VERDICT: BLOCKED`. It carries concrete questions for the human. It does not proceed with a
caveat — *"approved with caveats"* is explicitly forbidden in the critic's rubric.

**Shared Bob assets, the strip underneath.** `.bob/rules/` and `.bob/rules-<slug>/` (global and
per-mode rubrics, auto-loaded), four skills under `.bob/skills/`, the AIR JSON Schema with its
automatic gates, and `.bob/mcp.json` — which carries no credentials and no absolute paths, so
it is shareable. All committed to Git, so the tenth engineer to clone the repository inherits
the same pipeline as the first.

**The black callout.** *Write scope is a permission, not an instruction.* It prints the actual
`fileRegex` of the scaffold mode. This is the barrier against spec drift, and
[it has its own section below](#why-five-modes-and-not-one-prompt).

**Output — the right column.** Whatever the drawing is: agentic (Orchestrate ADK, LangGraph),
enterprise and legacy (RPA, mainframe, COBOL), cloud-native (services, IaC) — plus, always,
contracts, tests, a README, a Makefile and a `manifest.json` mapping component id → files. The
platform is a contract input, not an assumption baked into the generator. Under it, the header
that opens every generated file: the run id, the component id, the source artifact, and the
bounding box it came from.

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
| **5 · Validate**<br>`arch-validator` | `.arch/run/<run>/validation.md` | Each hypothesis from the AIR is marked VERIFIED, REFUTED or INCONCLUSIVE, with the exact command and the real output pasted in. Changing the architecture to make a test pass is forbidden — if the fix needs a new component, the AIR is wrong and the run goes back to stage 2. Closes with *"What the drawing did not anticipate"* | The human architect, who gets back the list of things the prototype discovered that the original drawing did not account for.<br>**Not yet demonstrated — see [what has not been verified](#what-has-not-been-verified)** |

---

## What the solution delivers, in practice

This is one real run, end to end: a ballpoint sketch on the left, the contract the pipeline
derived from it in the middle, and the code it generated on the right.

![Three panels. Panel 1, WHAT THE HUMAN MADE, source_kind napkin: a photographed ballpoint sketch of a stick figure talking to an Agente Supervisor box that contains plugin pre, LLM and plugin post; arrows fan out to two sub-agents, Agente de pesquisa de precos with the tool tool_google_search, and Agente Analista with tool_cria_tabela_comparativa and tool_gera_report_de_analise; three governance boxes sit underneath, wx gov guardrails/PII, LLM-Judge and redaction precos. The caption reads: no schema a parser can read, and half of what the code needs is not on the paper. Panel 2, THE CONTRACT BOB DERIVES, air.json: an OBSERVED section with 13 components and 9 connections each carrying evidence, showing agente_supervisor at confidence 0.99 with bbox evidence and tool_cria_tabela_comparativa with its own bbox; an INFERRED section where every guess declares its blast radius, showing a_wxo_platform made_by model with the impact "If wrong, the agent YAML format and the tool import mechanism both change"; and a MISSING section where the pipeline stopped and asked a human, showing u_protocol_agents with blocking true, the question "What protocol calls the sub-agents?" and the human answer "watsonx Orchestrate collaborator (native)", plus u_google_search_api with blocking false and a null answer, shipped open on purpose. The caption reads: overall_confidence 0.91, one blocking unknown halted the run until a person answered it, four non-blocking ones shipped open rather than guessed, and all four assumptions declare an impact. Panel 3, WHAT BOB GENERATED, watsonx Orchestrate ADK: the tree agents/supervisor-precos/ containing agents/ with agente_supervisor.yaml marked kind native, 2 tools, 2 collaborators, react, plus agente_pesquisa_precos.yaml and agente_analista.yaml; tools/ with tool_google_search.py marked @tool, tool_cria_tabela_comparativa.py, tool_gera_report.py, plugin_pre_guardrails.py marked wx.gov and plugin_post_redaction.py marked PII; tests/ with 6 modules, 17 passing, offline; a Makefile wired to the real orchestrate CLI; and requirements.txt, .env.example and README. Underneath, llm ibm/granite-3-3-8b-instruct and the import line from ibm_watsonx_orchestrate.agent_builder.tools import tool. The caption reads: flat ADK-native YAML and an instructions block that enforces the drawing's order — guardrails first, redaction last, never skip either. 17 of 17 tests pass with no credentials. Across the bottom: WHO USES IT — ANYONE WHO CAN DRAW THE IDEA, NOT JUST ANYONE WHO CAN CODE IT, and WHAT IT DELIVERS — ANY ARCHITECTURE THAT CAN BE EXECUTED](docs/images/solution-example.png)

**Panel 1 — what the human made.** A photograph of a ballpoint sketch. A stick figure, a
supervisor box holding *plugin pre / LLM / plugin post*, two sub-agents with their tools, and
three governance boxes underneath. There is no schema in it, nothing a parser can read, and
roughly half of what the generated code needs is simply not on the paper. That gap is the
entire problem, and pretending it does not exist is what produces plausible, wrong code.

**Panel 2 — the contract Bob derives.** The same drawing as `air.json`, split into three
categories that are never allowed to mix:

- **Observed** — 13 components and 9 connections, each with an `evidence` object naming the
  bounding box it was read from. `agente_supervisor` at confidence 0.99, with its box.
- **Inferred** — what the model deduced rather than saw, each declaring its blast radius. The
  assumption that the target platform is watsonx Orchestrate carries the impact *"If wrong, the
  agent YAML format and the tool import mechanism both change"* — which is exactly the sentence
  that tells a reviewer whether to care.
- **Missing** — what nobody knows. `u_protocol_agents` was marked `blocking: true` and **stopped
  the run** until a person answered *"What protocol calls the sub-agents?"* with *"watsonx
  Orchestrate collaborator (native)"*. Four more unknowns were non-blocking and **shipped open
  rather than guessed**, including which Google Search API to use.

`overall_confidence` is 0.91. All four assumptions declare an impact. The interesting number is
the one blocking unknown: the pipeline's most valuable output on this run was a question.

**Panel 3 — what Bob generated.** A real watsonx Orchestrate ADK tree: three flat ADK-native
agent YAMLs, five Python tools including the two governance plugins, six test modules, a
Makefile driving the real `orchestrate` CLI, `requirements.txt`, `.env.example` and a README.
The supervisor's `instructions` block encodes the order the human drew — guardrails first,
redaction last, never skip either — because that order was in the drawing and therefore in the
contract.

The tree is committed at [`agents/supervisor-precos/`](agents/supervisor-precos/) and its tests
run with no credentials and no network:

```bash
cd agents/supervisor-precos && python3 -m pytest tests -q   # 17 passed
```

That is the whole argument. A drawing on paper, a contract that says what it knows and what it
does not, and code that traces back to the box it came from.

---

## Why five modes and not one prompt

Because a prompt cannot revoke a capability, and a file permission can.

Every mode's `edit` group carries a `fileRegex` that pins it to its own stage's artifact.
These are the literal patterns in [`.bob/custom_modes.yaml`](.bob/custom_modes.yaml):

| Mode | `fileRegex` |
|---|---|
| `arch2code` | `^\.arch/run/.*\.md$` |
| `arch-intake` | `^\.arch/intake/.*\.(json\|md)$` |
| `arch-analyst` | `^\.arch/(air\|intake)/.*\.(json\|md\|ya?ml)$` |
| `arch-critic` | `^\.arch/review/.*\.md$` |
| `arch-scaffold` | `^(?!\.arch/(intake\|air\|review)/).*$` |
| `arch-validator` | `^(tests?/\|.*[._-](test\|spec)\.\|.*\.(test\|spec)\.\|\.arch/run/.*\.md$\|docker-compose\|Makefile)` |

The scaffold pattern is the interesting one: **a negative lookahead anchored at position 0.**
It grants the whole repository *except* three directories. The mode that writes the code
cannot edit the specification it implements, and cannot edit the review that approved it.

That is the barrier against spec drift, and it is the reason the pipeline is six modes rather
than one long prompt. An agent that can rewrite its own contract will eventually do it,
quietly, in the direction that makes its output look correct.

**The honest limit of that claim:** `fileRegex` constrains the mode's **edit tools**. It does
not constrain the `command` group, and the scaffold has `command`. A shell command can write
anywhere the process can write, including the three protected directories. The barrier is real
against the failure that actually happens — a model deciding the specification is wrong and
adjusting it — and it is not a sandbox. It is stated here rather than in a footnote because a
tool whose argument is traceability does not get to overstate its own guarantees.

Two more consequences of the same table: the orchestrator has neither `command` nor `mcp`, so
it verifies nothing on its own; and Bob raises no error for an unknown group name — a mode
that asks for a group that does not exist silently loses that access. Bob found that defect in
our own configuration: `arch-analyst` was told to run the AIR validator but had never been
granted `command`. The stage would have "passed" without validating anything.

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
into a measurable risk: an inference is labelled as one, forever, in the artifact that
generates the code.

`validate_air.py --gate` applies eight automatic blocks:

1. An open blocking unknown
2. `overall_confidence` below 0.75
3. A connection referencing a component that does not exist
4. An assumption with no declared impact
5. A hand-drawn connection under 0.85 confidence that was never second-passed
6. A synchronous cycle in the connection graph
7. No falsifiable hypothesis (*"without a hypothesis the prototype is a demo, not an experiment"*)
8. An empty `out_of_scope`

Reproduce the gate on the reference fixture, which is valid against the schema and
deliberately blocked by exactly one gate — the missing arrow on the napkin:

```bash
python3 .bob/skills/air-normalizer/scripts/validate_air.py \
        .bob/skills/air-normalizer/example-air.json           # exit 0 — schema and semantics
python3 .bob/skills/air-normalizer/scripts/validate_air.py \
        .bob/skills/air-normalizer/example-air.json --gate    # exit 1, one ERROR
```

There is a ninth blocking condition — a `boundaries[].contains` id that is not in
`components[]` — that is catalogued nowhere but the code. And `jsonschema` being absent
degrades the schema layer to a warning that does not affect the exit code, which is how the
two oldest runs in this repository passed with structurally invalid AIRs. Install it.

**When a stage degrades instead of failing.** There is one observed failure no retry can fix:
the Bob account budget runs out, the backend stops answering with no error and no exit, and the
stall watchdog kills the stage. If that happens to stage 2, the run used to die holding a
perfectly good `extraction.json`. [`webapp/app/air_fallback.py`](webapp/app/air_fallback.py)
instead derives the part of the AIR that is a **pure transform** of what is already in hand and
refuses to author the rest: `assumptions[]` stays `[]`, because an assumption is precisely what
is *not* drawn; a blocking unknown states that nothing was reasoned about; and `meta.extractor`
says in words that the analyst did not run. Verified: the result is **schema-valid (exit 0) and
rejected by the gate (exit 1)**, and the stage stays `failed` rather than green. The mechanics
are in [`webapp/README.md`](webapp/README.md#the-degraded-stage-2-path).

---

## The two vision passes

This is the mechanism the whole project rests on, so here is a real run rather than a claim.

`extract` asks the model **"what do you see?"** — a framing that rewards completing the
picture. `verify` asks **"someone may have read this wrong; does this arrow really exist?"** —
a framing that rewards doubt. Different prompt, different framing, **decorrelated error.**
Asking the same question twice only confirms the same bias, which is why "tell the model to
review its own answer" pays so little.

The critic therefore calls `arch_vision_verify_element`, never `extract` again.

### Run `20260722-0528-modeb2`, executed end to end through the web app

Input: `exemplo-rascunho.png`, a synthetic hand-drawn sketch with five boxes and four lines —
one of which is deliberately drawn as a bare line labelled `?`, with no arrowhead.

| Stage | Mode | Exit | NDJSON lines | Duration |
|---|---|---|---|---|
| 1 intake | `arch-intake` | 0 | 1,525 | 127 s |
| 2 analyst | `arch-analyst` | 0 | 1,084 | 114 s |
| 3 critic | `arch-critic` | 0 | 1,309 | 111 s |
| 4 scaffold | `arch-scaffold` | — | — | **never started** |

Stage 1 read the trap line as a directed connection `svc_pedidos → notificacao` and gave it
`confidence: 0.45`. Stage 2 carried it into the AIR with `verified_by_second_pass: false`.
Stage 3 issued one claim to `arch_vision_verify_element`:

> **Claim tested:** "Is there a directed arrow (with an arrowhead) from Svc Pedidos to Notificacao?"
>
> **Verdict:** FALSE · **Confidence:** 1.0
> **Observed:** *"There is a line connecting Svc Pedidos to Notificacao, but it is labeled with '?' and does not have a clear arrowhead."*

The other three connections were verified TRUE at confidence 1.0 in the same pass. The critic
wrote `VERDICT: BLOCKED` as the last line of
`.arch/review/20260722-0528-modeb2/verdict.md`, and **the pipeline stopped before generating a
single line of code.** The run then parked at the human gate and asked two closed questions —
does the connection exist at all, and if so in which direction and over what protocol.

Cost of finding out: **1,184,071 input tokens, 14,955 output tokens, 2.9976 Bobcoin, 336 s of
stage time, 27 tool calls.**

**The pipeline stopping and asking is the product working.** A run that goes all the way to
code without asking anything has failed the test: guessing right is the worst possible
outcome, because it teaches you to trust the guess.

---

## Target platforms

The generator does not know what watsonx Orchestrate is. It knows how to read a **profile**: a
declarative capability contract that states what a target can express, what it refuses and
why, which artifact each AIR component kind becomes, what it must ask the human, and what can
be validated offline.

Adding a target means adding a directory. It never means editing the engine.

| Profile | Status | Offline validation | Refuses |
|---|---|---|---|
| `orchestrate-adk` | verified | full | infrastructure — databases, queues and storage go behind a tool |
| `langgraph` | documented | full | UI, brokers, schedules; a database is a checkpointer, not a node |
| `container-microservice` | verified | full | almost nothing — the fallback when a specialised target says no |
| `mainframe-cobol` | documented | structural-only | caches, functions, object storage, every protocol that is not a file |
| `rpa` | documented | structural-only | infrastructure, and direct database access on principle |

`status` is a claim about the artifact contract; `validation` is a claim about what can be
proven with no platform attached. They are different claims and both are shown.
`orchestrate-adk` is `verified` because its field lists were read by introspection out of an
installed `ibm-watsonx-orchestrate 2.12.0`, not transcribed from documentation — every
statement in a profile is tagged `[VER]`, `[DOC]`, `[INF]` or `[NV]`.

Before any code is generated, `negotiate` intersects the drawing with the target and returns
one of four verdicts: **REFUSAL** (with the redraw that would work), **QUESTION**,
**DOWNGRADE** (it will be generated, but part of the intent becomes documentation), or
**RESOLVED**. There is deliberately no fifth verdict for *"generate something plausible"* —
that is the only one that produces code which looks right and is wrong.

```bash
S=.bob/skills/scaffold-from-air/scripts
python3 $S/target_engine.py list                                # every profile
python3 $S/target_engine.py doctor                              # what THIS machine can validate
python3 $S/target_engine.py negotiate <air.json> --profile rpa  # refuse before spending a token
```

This exists because the first generation of ADK templates in this repository was wrong in a
way nobody caught until import time: they emitted `apiVersion` / `metadata` / `spec`, the
Kubernetes shape, because the generator had no declared contract and reached for the most
familiar YAML it knew. An engine with no declared capabilities does not make that mistake once
— it makes it in five platforms instead of one.

---

## Seeing it run

The web application is the second front door onto the same pipeline: it drives the same Bob
modes as subprocesses, streams what the model is doing while it does it, stops at the human
gate, and hands back the artifacts. Two screens are worth showing here.

![The execution screen at the stage-3 gate: the run bar shows 20260722-0528-modeb2 with meters reading 1,199,026 tokens, cost 3, 5m 54s elapsed and 27 tool calls; the headline reads "The critic blocked this architecture" with a VERDICT: BLOCKED chip; the left column lists the contested findings ranked CRITICAL, HIGH and MEDIUM, each naming the element id and the required fix, followed by the executive summary and the critic's blocking questions rendered verbatim; the right column is a decision panel with approve, block and send-back options and a reason field](docs/images/ui-02-execution.png)

**The gate is a place, not a policy.** That is the run described above, meter for meter. On the
left of the workspace, your drawing with the bounding boxes the model drew on it — the answer
to *did it actually read my diagram?* On the right, every tool call as a collapsible block with
its parameters, its result, its duration and its token cost. When the critic blocks, the
findings are ranked and each one names the element id it is about, so the human decides against
specifics. Approving against a `BLOCKED` verdict is allowed and recorded: the reason field is
*optional when you agree with the verdict, mandatory when you do not*.

![The deliverables screen: three download cards — "The whole solution" (9 files, 522 KB), "Just the code", and "The whole project tree" (12 files, 998 KB) — where the code card carries an amber notice reading "This run generated no code to download" and explains why; below them a file tree on the left and the selected artifact, capture-manifest.json, rendered with syntax highlighting on the right showing run id, source sha256, source kind, extraction path and the normalization from 2372×1107 to 1568×732](docs/images/ui-03-deliverables.png)

**A download never implies work that was not done.** The run shown here stopped before
generation, so the code archive says exactly that, in words, with the reason, instead of
handing over an empty zip. Every artifact is browsable in place first — the pane on the right
is `capture-manifest.json`, the record of what was captured, its sha256, and exactly how the
image was rescaled before any model saw it.

Screen-by-screen detail, the accepted formats, the HTTP surface, every environment variable and
a symptom → cause → fix table live in **[`webapp/README.md`](webapp/README.md)**.

---

## What has actually been verified

Everything in this section was executed. Reproduce any line of it.

| Claim | Evidence |
|---|---|
| The harness is internally consistent | `bash tests/smoke_test.sh` → **21 passed, 0 failed**, on an interpreter with `jsonschema`, `mcp` and `httpx`. The stock macOS `python3` scores 19/20 and names `jsonschema` as the failure |
| The web app behaves as documented | `cd webapp && python -m pytest tests -q` → **313 tests**, no network, no credentials |
| The five platform profiles load, refuse and validate what they claim | `python3 .bob/skills/scaffold-from-air/scripts/selftest.py` → **35 passed, 0 failed** |
| The gate bites on a real run | run `20260722-0528-modeb2`: three stages exit 0, `VERDICT: BLOCKED`, stage 4 never started. Artifacts under `.arch/{intake,air,review}/20260722-0528-modeb2/` |
| Second-pass verification contradicts the extraction when it should | same run: verdict FALSE at confidence 1.0 on the one connection that was not an arrow |
| Cost of that run | 1,184,071 in / 14,955 out tokens · 2.9976 coins · 336 s · 27 tool calls |
| The generated solution in the example above runs | `cd agents/supervisor-precos && python3 -m pytest tests -q` → **17 passed**, offline, no credentials |
| The contract behind that example says what the panel says | `.arch/air/20260721-1759-supervisor-precos/air.json`: 13 components, 9 connections, 4 assumptions, `overall_confidence` 0.91, one blocking unknown answered by a human, four non-blocking unknowns left open |
| The degraded AIR is honest by construction | the fallback output is schema-valid (exit 0) and **rejected** by `validate_air.py --gate` (exit 1), with `assumptions[] == []` |
| Observations carry evidence | 3 of the 5 AIRs on disk were produced against the current schema; in those three, **42 of 42** components and connections carry an `evidence` object |
| Inferences declare their blast radius | **22 of 22** assumptions across all five AIRs carry a non-empty `impact` |
| Generated files are traceable to components | `.arch/build/20250717-1200-investment-advisor/manifest.json` maps component id → files → bbox evidence; **10 of 10** paths resolve on disk |
| The pipeline runs unattended | Bob driven headless as `bob --chat-mode <slug> --output-format stream-json`, one subprocess per stage, NDJSON parsed into a live timeline |

### What has *not* been verified

- **Stage 5 has never completed.** `.arch/run/` holds no `validation.md`. The mode, the
  harness script and falsifiable hypotheses exist; no run has produced a report.
- **Stage 4 has not been run through the web app.** The `.arch/build/` directories were
  produced from the Bob IDE. The only browser-driven pipeline run so far is the one that was
  blocked at stage 3 — which is a fair test of the gate and no test at all of the scaffold.
- **The generated Orchestrate artifacts have never been imported into a live tenant.** ADK
  2.12.0 has no `--dry-run`, so offline validation is pydantic-level only.
- **How Bob applies `fileRegex`** — engine, path form, normalisation. See the caveat above.
- **The `command` group bypasses the write scope.** Known, unmitigated inside Bob.
- **The two oldest runs** (`20260716-1548-uci`, `20260721-1129-atendente`) predate the current
  schema, carry no `evidence` anywhere, and fail `validate_air.py --gate`. They are kept as the
  baseline to beat. [`.arch/README.md`](.arch/README.md) catalogues every defect in them, in
  detail, rather than deleting them.
- **The time-saving figures** in the submission are an estimate against our own baseline, not
  an instrumented measurement.

---

## Run it yourself

### The web application, locally

```bash
git clone <this repo> && cd watsonx-challenge-2026

python3 -m venv .venv && source .venv/bin/activate    # Python 3.10 or newer
pip install -r webapp/requirements.txt

export ARCH2CODE_PYTHON="$(command -v python)"        # the interpreter you just installed into
./webapp/run.sh                                       # http://127.0.0.1:8765
```

`run.sh` refuses to start if that interpreter cannot import `fastapi` and `uvicorn`, and prints
the exact `pip` line. It binds to loopback and pins one worker, deliberately.

Two of the four prerequisites are optional: the `bob` CLI is needed only to drive the pipeline,
and watsonx.ai credentials only for the vision path — `.drawio`, `.puml` and `.mmd` run
deterministically with neither. The app probes its own environment at startup and **never
refuses to start**; it names the failing check instead, and enables reading a drawing and
running the pipeline independently. Full prerequisite table, every environment variable and a
symptom → cause → fix table: [`webapp/README.md`](webapp/README.md).

### From inside the Bob IDE

```bash
bash tests/smoke_test.sh              # 21 checks, no network — run this first
cp ~/Desktop/whiteboard.jpg .arch/intake/inbox/
```

Then select **🔀 arch2code — Orchestrator** and tell it which artifact to process. Full
step-by-step with its own symptom → cause → fix table:
[`INSTALL-GUIDE.md`](INSTALL-GUIDE.md).

### Verifying the MCP vision path on your own account

```bash
python3 mcp/arch_vision/preflight.py            # 6 gates: credentials, scope, IAM, catalog, model, images
python3 mcp/arch_vision/preflight.py --probe    # which catalogued models actually accept an image
python3 mcp/arch_vision/preflight.py --extract  # run the fixture against tests/ground-truth-example.json
```

`--extract` is the only test that answers *does vision work in MY catalog*. It scores the
extraction against a ground truth that includes the trap line: asserting a direction on the
line with no arrowhead is a critical failure, not a warning.

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
      profiles/                         the 5 platform contracts — add a directory, not code
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
  tests/                                313 tests, no network

tests/smoke_test.sh                     21 checks that must be green before opening Bob
tests/ground-truth-example.json         what the fixture really contains, including the trap

.arch/                                  the audit trail — evidence, not documentation
  intake/  air/  review/  build/  run/  one directory per run id, per stage
  README.md                             what each historical run did, and every defect in it

agents/                                 generated solutions, checked in as evidence
  supervisor-precos/                    the tree in the example above — 17 tests, offline
  investment-advisor/                   generated from a whiteboard photo, with its manifest

docs/images/                            architecture.svg, solution-example.png, the screenshots
INSTALL-GUIDE.md                        8 ordered steps, each isolating one class of failure
AGENTS.md                               the short contract for any agent working in this repo
```

Application code is the *output* of this repository, not its content. What is committed here
is the configuration that produces it — which is why the tenth team to adopt it costs the same
as the first.
