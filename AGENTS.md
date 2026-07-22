# arch2code

An IBM Bob mode pipeline that turns an architecture drawing — from a scribble on
a napkin to a versioned `.drawio` — into executable, experimentally validated
code.

## What this repository is

It is not an application. It is the **configuration of an agentic pipeline**:
modes, skills, rules, and an MCP server. Application code shows up when you run
the pipeline over a diagram; it is the output, not the contents.

## Flow

    diagram → [1 intake]     → extraction.json
            → [2 context]    → air.json
            → [3 critique]   → verdict.md  ←── blocking gate
            → [4 code]       → services + infra
            → [5 validation] → validation.md

Enter the pipeline through the `🔀 arch2code — Orchestrator` mode. It knows the
state and delegates. Do not call the stage modes directly, except to resume a
run.

## Structure

    .bob/custom_modes.yaml     the 6 pipeline modes
    .bob/mcp.json              registration of the arch_vision server
    .bob/rules/                rules that apply in every mode
    .bob/rules-<slug>/         per-mode rules (loaded automatically)
    .bob/skills/               the 4 workflows + scripts
    mcp/arch_vision/           vision MCP server (watsonx.ai)
    .arch/intake/inbox/        <- drop the drawing here
    .arch/{intake,air,review,run,build}/<run-id>/   artifacts per stage

## Invariants (do not break)

1. **Missing information never turns into a silent assumption.** It goes to
   `unknowns[]` or to `assumptions[]` with an `impact`. Never to `components[]`.
2. **A structured source takes precedence over vision.** If a `.drawio` exists,
   the path is `parse_drawio.py`. Vision costs tokens and imports hallucination.
3. **No mode approves its own work.** Each mode's `fileRegex` makes that
   structural, not a matter of discipline.
4. **The AIR is the contract.** `arch-scaffold` cannot edit it — that is how you
   stop the specification from being rewritten to fit the generated code.
5. **A prototype is an experiment.** Falsifiable hypothesis and an aggressive
   `out_of_scope`.

## The platform constraint that explains how the solution is shaped

Bob **does not ingest images through the normal path**: context mentions do not
accept binaries, `read_file` extracts text from `.docx/.pdf/.xlsx` but does not
interpret pixels, and there is no vision tool in the tool list. That is why the
image comes in through **MCP** (`arch_vision` → watsonx.ai). If you swap that
server out, keep the contract of the four tools; the rest of the pipeline depends
on them.

## Environment

    pip install -r mcp/arch_vision/requirements.txt
    cp mcp/arch_vision/.env.example mcp/arch_vision/.env   # fill it in
    export WATSONX_APIKEY=... WATSONX_PROJECT_ID=...

The deterministic path (`.drawio`/`.puml`/`.mmd`) works **with no credentials**.
Only the vision path needs watsonx.

## Conventions

- **This harness is written in English — all of it.** Documents, comments,
  conversation, code identifiers, slugs, JSON keys, and file names.
- **The language of what the pipeline *generates* is not covered by that rule.**
  It is a parameter of the contract: `meta.output_language` in the AIR, default
  `en`. Set it per run when the delivered artifacts have to speak another
  language.
- The split exists because the harness and the deliverable have different
  readers: the harness is maintained by engineers across the US and UK, while a
  generated prototype has to speak to whoever the drawing was made for, so the
  choice belongs in the AIR where it is explicit and auditable — not in the tone
  of the prose.
- Run id: `YYYYMMDD-HHMM-<short-slug>`.
- Zero secrets in a versioned file.
