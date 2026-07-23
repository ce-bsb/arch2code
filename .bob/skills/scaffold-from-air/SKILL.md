---
name: scaffold-from-air
description: Generates the code structure, contracts, and local infrastructure from an approved AIR, with traceability from every file back to the diagram element it came from. Use in stage 4, only after the critic's APPROVED verdict.
---

# Scaffolding from the AIR (stage 4)

Generates code derived **exclusively** from the approved AIR.

## Hard preconditions

**1. The critic approved.** Read `.arch/review/<run>/verdict.md`. Without
`VERDICT: APPROVED`, **stop** and hand back to the orchestrator. No file is
generated before that. The gate is not bureaucracy: it is what keeps dozens of
files from being born on top of an arrow that was read backwards.

**2. The target profile accepted the drawing.** Before writing anything:

    python3 .bob/skills/scaffold-from-air/scripts/target_engine.py \
        negotiate .arch/air/<run>/air.json --profile <target>

- exit `0` — proceed.
- exit `1` — **refusals.** The target cannot express part of this drawing. Report
  the refusals verbatim; each one carries the redraw or the target swap that
  would work. Do not generate the rest and hope. Use
  `target_engine.py match .arch/air/<run>/air.json` to see which of the five
  targets does fit.
- exit `2` — **blocking questions.** Every one of them decides code that cannot
  be inferred from the drawing. They go to the human, not to your judgement.

Not sure which profile: `target_engine.py list`. The chosen id is recorded in
`air.meta.target_profile` — the same AIR against a different profile is a
different system.

## The profile is the contract, and it wins

`profiles/<target>/target.yaml` declares, per artifact, the `schema_facts` you may
rely on and the `must_not` list of mistakes already made. **If a field is not in
`schema_facts`, you do not know it exists.** Reaching for a familiar shape instead
is exactly how this repo shipped watsonx Orchestrate agents in Kubernetes YAML —
`apiVersion` / `metadata` / `spec` — that no tenant would ever import.

When the profile does not answer a question the code needs, that is an
`unknowns[]` entry for the human. It is never a guess with a confident comment
above it.

## Generation order

<Steps>
<Step>
**Contracts first.** For each entry in `connections[]`: OpenAPI (http), Protobuf
(grpc), event schema (kafka/amqp), DDL (sql). The contract is the outline of
everything that comes after; generating a service before its contract guarantees
rework.
</Step>

<Step>
**Skeleton for each component** in `components[]`: entrypoint, per-environment
config, `/health`, structured logging. Without `/health`, stage 5 has nothing to
measure.
</Step>

<Step>
**Adapters for each connection**: HTTP client, producer/consumer, repository. One
adapter per `connections[].id` — traceability is 1:1.
</Step>

<Step>
**Local infra**: `docker-compose.yml` with every component, `.env.example`, a
`Makefile` with `up`/`down`/`test`/`logs`. The prototype has to come up with a
single command.
</Step>

<Step>
**Fixtures and seeds**, the minimum for the system to come up end to end and for
the first hypothesis to be testable.
</Step>

<Step>
Write `.arch/build/<run>/manifest.json` mapping `component_id -> [files]`. Without
a manifest nobody can answer "where did this service come from?" — and that
question always shows up in the architecture review.
</Step>
</Steps>

## Mandatory header in every generated file

    # arch2code: generated from AIR <run_id> :: <component_id>
    # source: <path of the original artifact>  evidence: <bbox|cell>
    # DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode

## Quality bar

- Every external dependency has a local stub: the prototype runs offline.
- Zero secrets in code. `.env.example` documents; `.env` stays in `.gitignore`.
- Empty handler: `raise NotImplementedError("AIR <id>: <what is missing>")`, never
  `pass`. A loud failure is honest; a silent stub lies about how complete the
  prototype is, and someone will demo it believing it works.
- Only what the AIR asks for. No "while I'm in here, let me add a cache".

## If the AIR is wrong

You are probably right — but the path is to report to the orchestrator and go back
to `arch-analyst`. The mode's fileRegex already stops you from editing `.arch/air/`.
Bending the spec to fit the code is the most expensive way to be wrong in an
agentic pipeline: the contract stops describing the drawing and starts describing
the accident.

## Before handing back

Run the target's offline gates on what you generated:

    python3 .bob/skills/scaffold-from-air/scripts/target_engine.py \
        check <project_dir> --profile <target>

A failed gate is a **re-prompt of this stage, not a delivery**. A skipped gate is
reported to the human with what it was not able to check — `doctor` shows which
gates this machine can actually run, and a target at `structural-only` must be
described that way in the handoff. Do not promise a compile that nobody ran.

## Files

- `profiles/` — the five platform profiles and the format they obey. Start at
  `profiles/README.md`.
- `scripts/target_engine.py` — discovery, negotiation, offline gates (CLI).
- `scripts/validate_adk.py` — validates generated watsonx Orchestrate YAML
  against the installed ADK models, with no tenant.
- `scripts/check_cobol_structure.py`, `check_jcl_structure.py`,
  `check_robot_structure.py` — the grammars for the languages with no offline
  linter.
- `scripts/selftest.py` — proves all of the above without Bob and without a
  credential.
- `examples/agentic-air.json` — a schema-valid agentic AIR to negotiate against.
- `reference/stack-map.md` — from the AIR's `kind` to technology and template.
  Now the prose companion to `profiles/container-microservice/target.yaml`, which
  is the executable version of the same rules.
