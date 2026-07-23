# Platform profiles

A profile is a **declarative capability contract** for one generation target. It
says what the target can express, what it refuses and why, which artifact each
AIR component kind becomes, what it must ask the human, and what can actually be
validated offline.

Adding a target means adding a directory. It never means editing the engine.

```
profiles/
  profile.schema.json          the format, enforced on load
  <id>/target.yaml             one target
  <id>/templates/              optional; only where the contract is verified
```

## Why this exists

The first generation of this repo's watsonx Orchestrate templates was wrong in a
way nobody caught until import time: they emitted `apiVersion` / `metadata` /
`spec`, the Kubernetes shape, because the generator had no declared contract and
reached for the most familiar YAML it knew. The real ADK spec is flat.

An engine without declared capabilities does not make that mistake once. It makes
it in five platforms instead of one.

So every profile carries:

- **`provenance`** — why anyone should believe its artifact contract, marked
  `[VER]` (checked against an installed toolchain), `[DOC]` (vendor
  documentation), `[INF]` (a design decision made here) or `[NV]` (not
  verified). `orchestrate-adk` is `[VER]` because the field lists were read out
  of `ibm-watsonx-orchestrate 2.12.0` by introspection.
- **`schema_facts`** per artifact — the statements a template is allowed to rely
  on, and nothing else.
- **`must_not`** per artifact — the mistakes that have already been made.

## The four verdicts

`negotiate` intersects a drawing with a target the way an MCP handshake
intersects two peers: the session proceeds only over what both sides declare.

| Verdict | Meaning |
|---|---|
| **REFUSAL** | the target cannot express this, with the redraw that would work |
| **QUESTION** | the drawing is ambiguous, or the target needs a fact nobody stated |
| **DOWNGRADE** | it will be generated, but part of the intent becomes documentation |
| **RESOLVED** | a parameter the AIR answered on its own — nobody was asked |

There is deliberately no fifth verdict for "generate something plausible". That
is the only one that produces code which looks right and is wrong.

Refusal happens **before** `bob --chat-mode=arch-scaffold`. One trivial Bob stage
in this repo measured 37,154 tokens; refusing here costs nothing.

## Commands

```bash
S=.bob/skills/scaffold-from-air/scripts

python3 $S/target_engine.py list                       # every profile
python3 $S/target_engine.py show orchestrate-adk       # one in full
python3 $S/target_engine.py doctor                     # what THIS machine can validate
python3 $S/target_engine.py match  <air.json>          # which target fits this drawing
python3 $S/target_engine.py negotiate <air.json> --profile orchestrate-adk
python3 $S/target_engine.py check  <project_dir> --profile orchestrate-adk
python3 $S/selftest.py                                 # 35 assertions, no Bob, no credential
```

Exit codes are meant to be branched on: `0` proceed, `1` refused or a gate
failed, `2` blocked on questions for the human, `3` usage.

## The five targets

| id | status | validation | refuses, in one line |
|---|---|---|---|
| `orchestrate-adk` | verified | full | infrastructure — databases, queues, storage go behind a tool |
| `langgraph` | documented | full | UI, brokers and schedules; a database is a checkpointer, not a node |
| `container-microservice` | verified | full | almost nothing — the fallback when a specialised target says no |
| `mainframe-cobol` | documented | structural-only | caches, functions, object storage, and every network protocol that is not a file |
| `rpa` | documented | structural-only | infrastructure, and direct database access on principle |

`status` is about the artifact contract; `validation` is about what can be proven
offline. They are different claims and both are shown in the UI.

## Validation without the platform

The Terraform model: configuration validation runs offline, with no provider
configured. Here that means no mainframe, no watsonx Orchestrate tenant, no
cluster.

Two levels:

- **Level 0 — capability negotiation.** Pure data, instant, zero dependencies.
- **Level 1 — syntax gates** on the generated code: `validate_adk.py` (real ADK
  pydantic models), `py_compile`, `ruff`, `javac -proc:only`, `node --check`,
  `docker compose config -q`, `cobc -fsyntax-only`, `robot --dryrun`,
  `langgraph build`, plus the structural grammars written here for COBOL, JCL and
  Robot suites where nothing offline exists.

`doctor` reports the level that is true **on this machine**, which is not always
the level the profile declares. A profile can honestly declare `full` and still be
structural-only on a laptop with no compiler; reporting the declared level in that
case is the same lie one layer up.

Measured on the machine this was written on: `python3`, `ruff`, `mypy`, `node`,
`javac`, `docker`, `xmllint` and `ibm-watsonx-orchestrate 2.12.0` present;
`tsc`, `cobc`, `robot`, `dotnet`, `langgraph`, `terraform`, `kubeconform` absent.

## Adding a target

1. `mkdir profiles/<id>` and write `target.yaml`.
2. Declare **every** value of every AIR enum, in `supports` or in `excludes`. The
   loader does not require it, but `list` and `selftest.py` will point at the gap:
   an undeclared capability is refused with a generic message, and a generic
   refusal is where a future maintainer decides to "just add it".
3. Give every exclusion a `reason` written for the person who drew the diagram,
   and a `workaround` whenever one exists. A refusal with no way forward is a
   dead end.
4. Mark `status` honestly. If you have not run the platform's own validator
   against a generated artifact, it is not `verified`.
5. Run `python3 ../scripts/selftest.py`.

Nothing in `scripts/` needs to change.
