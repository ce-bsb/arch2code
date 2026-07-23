---
name: air-normalizer
description: Turns the raw extraction of a diagram into a validated AIR (Architecture Intermediate Representation), separating what was observed from what was inferred and from what is missing. Use it in stage 2, after intake, before any code generation.
---

# Normalization to AIR (stage 2)

Turns `extraction.json` into the technical contract every line of code will be
derived from: `.arch/air/<run>/air.json`, per `air.schema.json`.

## The three categories — never mix them

| What | Where it goes | Rule |
|---|---|---|
| **Observed** in the drawing | `components[]`, `connections[]` with `evidence` | must carry evidence |
| **Inferred** by you | `assumptions[]` with `made_by: "model"` | needs a declared `impact` |
| **Missing** | `unknowns[]` | becomes a closed question, never a guess |

This separation is the most important thing in the stage. Once the three blur
together, nobody can tell any more whether a component came from the drawing or
from the model's imagination — and that is exactly where the pipeline starts
producing systems that are plausible and wrong.

## Steps

<Steps>
<Step>
Read `.arch/intake/<run>/extraction.json` and the `capture-manifest.json`. Pay
special attention to the `warnings`: they are the decisions that were left for you.
</Step>

<Step>
Map nodes → `components[]`. The parser's `shape_hint` is a **hint, not the truth**:
a cylinder can be a database or a bucket. No technology label →
`"tech": null`. Guessing here contaminates every line of generated code.
</Step>

<Step>
Map edges → `connections[]`. An edge with no arrowhead → `"sync": "unknown"` plus an
`unknowns[]` entry describing the ambiguity. A dangling edge (`dangling`) → never
invent the missing node; ask.
</Step>

<Step>
Record every inference in `assumptions[]` with an `impact`: *what breaks in the code
if this assumption is wrong*. An assumption with no declared impact is a hidden
assumption, and the critic blocks it.
</Step>

<Step>
Write the `experiment_plan`: 2 to 5 **falsifiable** hypotheses, the proposed stack and
`out_of_scope`. Test each hypothesis with the question "what result would refute it?".
If there is no answer, it is not a hypothesis — it is an opinion, and you should not
write it down. Be aggressive in `out_of_scope`: a prototype that tries to do
everything validates nothing.
</Step>

<Step>
Validate before declaring the stage done:

    python3 .bob/skills/air-normalizer/scripts/validate_air.py .arch/air/<run>/air.json

An invalid schema means the stage is not done. Do not pass it downstream on the
theory that "the critic will catch it".
</Step>

<Step>
Take the unknowns to the human: at most 5 per round, ordered by how much they
unblock, with concrete options. "Which protocol between A and B: synchronous REST,
gRPC or an asynchronous event?" unblocks; "how does A talk to B?" just buys you
another round.
</Step>
</Steps>

## Agentic drawings

The schema carries an agentic vocabulary, and it exists because two real runs
invented the same words independently before anyone wrote them down.

| Element | Use it when |
|---|---|
| `kind: agent` | the box is an LLM that decides — it has instructions and picks what to do next |
| `kind: tool` | a deterministic callable an agent invokes. Goes **inline** in `components[].tools[]`, not as its own component: a tool has no life outside its agent |
| `kind: knowledge_base` | a retrieval corpus the agent searches |
| `protocol: internal` | an in-platform hand-off with no wire protocol — agent to collaborator agent, agent to knowledge base |
| `protocol: external_chat` | a conversational hand-off to an agent on another platform |
| `boundaries[].kind: external_system` | the box drawn around what the team does not own |

A plain REST service is `kind: service`, not `kind: agent`. The distinction is not
cosmetic: it decides whether the generated artifact has instructions and a model,
or routes and a Dockerfile.

`components[].tools[]` entries are objects — `{id, name, tech, note, evidence}` —
because `tech` is what tells the generator *which system the tool talks to*, and a
bare string throws that away. Tool ids share the id namespace with components:
`validate_air.py` fails on a collision, since every agentic profile turns an id
into a file name.

## Two fields the generator reads

- **`meta.output_language`** (default `en`) — the language the generated system
  **speaks to end users**: agent instructions, descriptions, user-facing strings.
  Code, identifiers and comments stay English regardless. Set `pt-BR` when the
  client's users read the agent's answers.
- **`meta.target_profile`** — the id of the platform profile this AIR is being
  generated for (`orchestrate-adk`, `langgraph`, `container-microservice`,
  `mainframe-cobol`, `rpa`). Leave it `null` if the human has not chosen yet;
  `target_engine.py match .arch/air/<run>/air.json` ranks the five against the
  drawing. Record it once chosen — the same AIR against a different profile is a
  different system, and without this the build is not reproducible.

## Enrichment: what you may and may not do

**You may**: propose a concrete technology when the drawing gives you the category
but not the product ("queue" → Kafka or RabbitMQ). It goes into `assumptions[]` with
alternatives, and the final call belongs to the human. See
`.bob/skills/scaffold-from-air/reference/stack-map.md`.

**You may not**: invent a component that is not there; create a connection that was
not drawn; assume a nonfunctional requirement nobody wrote down. At a financial
services client the temptation to assume LGPD, PCI or an SLA is strong — do not
assume. Ask.

## Files

- `air.schema.json` — the contract (JSON Schema 2020-12)
- `example-air.json` — a complete, worked example of a napkin sketch.
  <!-- Its component names and evidence.label_text values are deliberately kept in
  Portuguese: they are literal readings off the artifact, and the contract forbids
  normalizing what was read. Never translate them. -->
- `scripts/validate_air.py` — schema + semantics (`--gate` also applies the critic's gates)
