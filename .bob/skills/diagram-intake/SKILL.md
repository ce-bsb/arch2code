---
name: diagram-intake
description: Ingests an architecture drawing (napkin photo, whiteboard, screenshot, PDF or drawio) and produces the traceable raw extraction. Use it whenever there is an image, sketch or diagram file to process, before any specification or code.
---

# Diagram intake (stage 1)

Turns any technical drawing into `extraction.json`: raw structure, with confidence
and evidence per element. You are an extractor, not an architect.

## The decision that sets the quality of everything downstream

Pick the path by **file type**:

| Artifact | Path | Tool | Why |
|---|---|---|---|
| `.drawio`, `.xml` | deterministic | `scripts/parse_drawio.py` | the XML has the exact nodes, edges and direction |
| `.puml`, `.mmd`, `.md` | deterministic | `read_file` | it is text; read it |
| `.pdf` with text | hybrid | `read_file`, vision only if that fails | the text layer is exact |
| `.png/.jpg/photo/scanned PDF` | vision | `arch_vision_extract_architecture` | there is no alternative |

**Using vision where a structured source exists is an engineering error**, not a
preference: you trade an exact read for a probabilistic one, pay tokens and import
hallucination risk in exchange for nothing. `capture_diagram.py --list` warns you
when you are about to do it.

## Steps

<Steps>
<Step>
List what there is to process and see the routing for each artifact:

    python3 .bob/skills/diagram-intake/scripts/capture_diagram.py --list

If the inbox is empty, point the human to it (README, "Capture" section) instead
of inventing an example diagram.
</Step>

<Step>
Set the run id: `YYYYMMDD-HHMM-<short-slug>`. It names every artifact of the run
from here to the final report.
</Step>

<Step>
Register and normalize the artifact:

    python3 .bob/skills/diagram-intake/scripts/capture_diagram.py \
        .arch/intake/inbox/<file> --run <run-id>

This computes the sha256 of the original (traceability), corrects EXIF rotation
and resizes to ≤1568px. A phone photo comes in sideways: a vision model reads a
diagram rotated 90° and produces garbage with high confidence. Do not skip this
step.
</Step>

<Step>
**Deterministic path** — run the parser:

    python3 .bob/skills/diagram-intake/scripts/parse_drawio.py \
        .arch/intake/inbox/<file>.drawio \
        --out .arch/intake/<run-id>/extraction.json

Read the `warnings` in the output: an edge with no arrowhead, a dangling edge and
a node with no shape hint are exactly the decisions the analyst has to make later.
</Step>

<Step>
**Vision path** — extract with the multimodal model via `use_mcp_tool`:

    server: arch_vision
    tool:   arch_vision_extract_architecture
    args:   image_path = <the .normalized.png from step 3>
            source_kind = napkin | whiteboard | screenshot | pdf
            hint = <context the human gave, if any>

Set `source_kind` correctly: `napkin`/`whiteboard` turn on an extra skepticism
instruction in the prompt.
</Step>

<Step>
**Second pass (mandatory on the vision path).** For every id in
`_quality.connections_needing_verification`, call:

    tool: arch_vision_verify_element
    args: image_path = <same image>
          claim = "is there an arrow from <A> to <B>?"   (ONE claim per call)

A `false` or `uncertain` verdict → turn it into `unknowns[]`, never force it to
`true`. This works because `verify` uses a different prompt and a different
framing from `extract`: the error is decorrelated. Repeating `extract` would only
confirm the same bias.
</Step>

<Step>
Write `.arch/intake/<run-id>/extraction.json` and present it to the human: how
many components and connections, the overall confidence, and — above all — the
list of gaps. If there is a blocking unknown, ask now with
`ask_followup_question`, with closed options.
</Step>
</Steps>

## Rules

- Write only inside `.arch/intake/`. The mode's fileRegex blocks everything else.
- All evidence is traceable: normalized `bbox` (vision) or `cell_id` (drawio).
- Do not propose a stack, a pattern or a technology. That belongs to `arch-analyst`.
- Illegible image → ask for another photo. Extracting badly is worse than not
  extracting at all: the error propagates silently through the whole pipeline and
  only surfaces in the generated code.

## Files

- `scripts/capture_diagram.py` — routing, hash, normalization, manifest
- `scripts/parse_drawio.py` — deterministic mxGraphModel parser
