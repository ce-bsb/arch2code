"""The exact prompt text handed to each arch2code stage mode.

Two rules govern everything here.

**Chaining is per file, never per session.** Each stage is a clean Bob session:
there is no ``--resume`` between stages and no shared context. The only thing
that carries state forward is the artifact the previous stage wrote, referenced
by path in the next prompt. That is a feature -- a stage cannot be contaminated
by what an earlier stage was thinking, only by what it actually produced.

**Paths are expressed relative to the Bob working directory.** The modes'
``fileRegex`` patterns are anchored at the workspace root (``^\\.arch/intake/``
and friends), so a prompt that names an absolute path invites the model to
write somewhere the mode cannot write. The source artifact is given both ways:
relative for Bob to act on, absolute so a human reading the timeline knows
exactly which file was processed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .pipeline import StageSpec

__all__ = [
    "PromptContext",
    "build_prompt",
    "intake_prompt",
    "analyst_prompt",
    "critic_prompt",
    "scaffold_prompt",
    "validator_prompt",
    "rel",
]


@dataclass(frozen=True)
class PromptContext:
    """Everything a stage prompt needs, resolved once per run."""

    run_id: str
    project_root: Path
    source_path: Path
    source_kind: str
    hint: str | None
    intake_path: Path
    air_path: Path
    verdict_path: Path
    manifest_path: Path
    validation_path: Path
    pipeline_log_path: Path
    gate_feedback: str | None = None


def rel(path: Path, root: Path) -> str:
    """Express ``path`` relative to ``root`` when it sits underneath it.

    Falls back to the absolute path rather than emitting ``../../..``: a
    relative path that climbs out of the workspace is worse than an honest
    absolute one, because it silently fails the mode's ``fileRegex``.
    """
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(path)


def build_prompt(stage: "StageSpec", ctx: PromptContext) -> str:
    """Dispatch to the prompt builder for ``stage``.

    Raises:
        KeyError: if a Bob-backed stage has no prompt. That is a programming
            error in the stage table, and failing loudly here beats sending an
            empty prompt and paying for it.
    """
    builder = _BUILDERS.get(stage.id)
    if builder is None:
        raise KeyError(
            f"No prompt is defined for stage {stage.id!r}. "
            "Every Bob-backed stage in PIPELINE_STAGES needs an entry in prompts._BUILDERS."
        )
    return builder(ctx)


# --------------------------------------------------------------------------- #
# stage 1 -- intake
# --------------------------------------------------------------------------- #


def intake_prompt(ctx: PromptContext) -> str:
    source_rel = rel(ctx.source_path, ctx.project_root)
    out_rel = rel(ctx.intake_path, ctx.project_root)
    hint = _hint_block(ctx.hint)
    return f"""\
Stage 1 of the arch2code pipeline. Run id: {ctx.run_id}

Process this single artifact and produce the raw extraction. Do nothing else.

  artifact (relative to the workspace root): {source_rel}
  artifact (absolute, for reference):        {ctx.source_path}
  declared source kind:                      {ctx.source_kind}

Required output, at exactly this path and nowhere else:

  {out_rel}

Both paths above are exact and have already been checked for you. The artifact
exists and its output directory exists and is empty. Do not list directories to
confirm either one, and do not go looking for the file elsewhere if a listing
surprises you — read the artifact at the path given and write the output at the
path given. Every listing you make is shown to the person watching this run, so
searching for something you were handed reads as the tool being lost.

How to do it:

1. Route by file type, not by habit. A structured source (.drawio, .xml, .puml,
   .mmd, .md, .json, .yaml) goes down the DETERMINISTIC path with
   .bob/skills/diagram-intake/scripts/parse_drawio.py or a direct read. Using
   vision when a structured source exists is forbidden. A raster image
   (.png, .jpg, .jpeg, .webp, .heic) or a scanned PDF goes down the VISION
   path: normalise with .bob/skills/diagram-intake/scripts/capture_diagram.py
   and then call arch_vision_extract_architecture on the normalised image.
2. Follow the protocol in .bob/skills/diagram-intake/SKILL.md. If the skill does
   not auto-load, read that file directly; run the scripts by explicit path.
3. Every element carries a confidence in 0.0-1.0 and an evidence entry:
   {{"kind":"bbox","value":[x,y,w,h]}} plus the literal label text for vision,
   {{"kind":"cell","value":"<mxCell id>"}} for drawio.
4. Anything you cannot read becomes an entry in unknowns[], never a guess with a
   low confidence attached. Set unknowns[].blocking = true when the gap would
   stop code generation.
5. On a hand-drawn artifact the second pass is mandatory: call
   arch_vision_verify_element for every connection with confidence < 0.8.
{hint}
Hard limits:

- Write nothing outside .arch/intake/.
- Propose no stack, no pattern and no technology. That is stage 2's job.
- If the image is illegible, say so in unknowns[] and stop. Extracting badly is
  worse than not extracting: the error propagates silently down the pipeline.

You are running non-interactively. Do not ask a follow-up question -- record the
question in unknowns[] with blocking set appropriately and finish the stage.

Exit criterion: {out_rel} exists, is valid JSON, and every element in it carries
confidence and evidence.
"""


# --------------------------------------------------------------------------- #
# stage 2 -- analyst
# --------------------------------------------------------------------------- #


def analyst_prompt(ctx: PromptContext) -> str:
    in_rel = rel(ctx.intake_path, ctx.project_root)
    out_rel = rel(ctx.air_path, ctx.project_root)
    source_rel = rel(ctx.source_path, ctx.project_root)
    feedback = _feedback_block(ctx.gate_feedback)
    return f"""\
Stage 2 of the arch2code pipeline. Run id: {ctx.run_id}

Read the extraction produced by stage 1 and turn it into the AIR, the versioned
technical contract every generated line of code will derive from.

  input  (read this file, it is the only state carried from stage 1): {in_rel}
  output (write exactly here):                                        {out_rel}
  original artifact, for evidence cross-checks:                       {source_rel}
{feedback}
Rules:

1. Conform to .bob/skills/air-normalizer/air.schema.json. Run
   .bob/skills/air-normalizer/scripts/validate_air.py {out_rel} before you
   declare the stage done. An invalid schema means the stage is not done.
2. Keep the three categories rigorously separate and never merge them:
   - observed  -> components[] / connections[], each with its evidence
   - inferred  -> assumptions[] with made_by "model", a rationale and an impact
   - missing   -> unknowns[], as a question, never as a guess
3. Every assumption declares its impact: what breaks in the code if it is wrong.
   An assumption with no declared impact is a hidden assumption.
4. You may name a concrete technology when the drawing names the category but
   not the product ("queue" -> Kafka or RabbitMQ). It becomes an assumptions[]
   entry with the alternatives listed; the final call belongs to the human.
   See .bob/skills/scaffold-from-air/reference/stack-map.md.
5. You may not invent a component that is not in the drawing, create a
   connection nobody drew, or assume a non-functional requirement (SLA,
   throughput, compliance) that nobody wrote down.
6. Fill in experiment_plan with 2 to 5 falsifiable hypotheses, the proposed
   stack, and an aggressive out_of_scope. A prototype that tries to do
   everything validates nothing.

You are running non-interactively. Do not ask a follow-up question -- every
question for the human goes into unknowns[], ordered by how much it unblocks,
with blocking set to true only when it genuinely prevents generating code.

Exit criterion: {out_rel} exists and validate_air.py exits 0 on it.
"""


# --------------------------------------------------------------------------- #
# stage 3 -- critic (the gate)
# --------------------------------------------------------------------------- #


def critic_prompt(ctx: PromptContext) -> str:
    air_rel = rel(ctx.air_path, ctx.project_root)
    out_rel = rel(ctx.verdict_path, ctx.project_root)
    source_rel = rel(ctx.source_path, ctx.project_root)
    return f"""\
Stage 3 of the arch2code pipeline: the adversarial gate. Run id: {ctx.run_id}

Review this AIR as the last line of defence before dozens of files get generated
on a false premise. Your incentive is to find the defect that got through, not
to approve the work.

  AIR under review:      {air_rel}
  original artifact:     {source_rel}
  write the verdict to:  {out_rel}

Procedure:

1. Validate the AIR against the schema with
   .bob/skills/air-normalizer/scripts/validate_air.py {air_rel}. A failure is a
   BLOCK.
2. Cross-verify against the source drawing: for every connections[] entry with
   confidence < 0.85, call arch_vision_verify_element with the claim written out
   in natural language. That verification uses a different prompt and a
   different pass, which is what makes it independent rather than the same bias
   twice. Any divergence between the AIR and the verification is a BLOCK.
3. Apply the rubric in .bob/rules-arch-critic/01-review-rubric.md in full.

Automatic, non-negotiable blocking criteria:

- any open unknowns[] entry with blocking == true
- meta.overall_confidence < 0.75
- a component with no connection at all and no explicit justification
- a connection pointing at a component that does not exist
- an assumptions[] entry with no declared impact
- a synchronous cycle between services (A -> B -> A with sync)
- an empty experiment_plan.hypotheses

What you do not do: you do not fix the AIR (you point, the analyst fixes -- the
fileRegex already stops you), and you do not approve "with caveats". A caveat is
an unknowns[] entry and a trip back to stage 2.

Report format for {out_rel}: the cross-verification, the findings (severity,
element, what is wrong, what to do), and the questions that block.

THE LAST NON-EMPTY LINE OF {out_rel} MUST BE EXACTLY ONE OF:

VERDICT: APPROVED
VERDICT: BLOCKED

Nothing after it -- no closing sentence, no trailing table, no signature. That
line is read mechanically by the pipeline and shown to a human for the decision.
A verdict file whose last line is neither of those two strings is reported as a
defective run, and it will not be treated as an approval.
"""


# --------------------------------------------------------------------------- #
# stage 4 -- scaffold
# --------------------------------------------------------------------------- #


def scaffold_prompt(ctx: PromptContext) -> str:
    air_rel = rel(ctx.air_path, ctx.project_root)
    verdict_rel = rel(ctx.verdict_path, ctx.project_root)
    manifest_rel = rel(ctx.manifest_path, ctx.project_root)
    source_rel = rel(ctx.source_path, ctx.project_root)
    return f"""\
Stage 4 of the arch2code pipeline: scaffolding. Run id: {ctx.run_id}

Hard precondition, check it first:

  Read {verdict_rel}. If it does not contain the line "VERDICT: APPROVED",
  STOP and generate nothing at all. Report why and end the stage.

A human has already reviewed that verdict and released this stage; that release
does not excuse you from the check, it just means the check should pass.

  approved specification:  {air_rel}
  original artifact:       {source_rel}
  manifest to write:       {manifest_rel}

Implement from the specification and from nothing else. Every file you create
traces back to a components[].id or a connections[].id in the AIR. Do not expand
scope, do not add the component that "would make sense", do not anticipate a
requirement nobody asked for. If the implementation needs something the AIR does
not say, stop and report it -- that call belongs to the analyst.

Traceability: every generated file starts with the comment header

    arch2code: generated from AIR {ctx.run_id} :: <component_id>
    source: <path of the original artifact>  evidence: <bbox|cell>
    DO NOT EDIT BY HAND -- regenerate via the arch-scaffold mode

and {manifest_rel} maps component_id -> [files]. Without the manifest nobody can
answer "where did this service come from?", and that question always comes up.

Generation order, dependencies before dependents:

1. contracts first (OpenAPI / Avro / Protobuf / DDL)
2. a skeleton per component: entrypoint, config, healthcheck, logging
3. an adapter per connection: HTTP client, producer/consumer, repository
4. local infra: docker-compose, .env.example, Makefile
5. minimal fixtures and seeds so the system comes up end to end

Quality bar: each component starts on its own and answers /health; every
external dependency has a local stub so the prototype runs offline; zero secrets
in code; an unimplemented handler raises NotImplementedError naming the AIR id
rather than silently passing. Follow
.bob/rules-arch-scaffold/01-codegen-standards.md.

Exit criterion: {manifest_rel} exists and maps every component in the AIR to the
files you generated.
"""


# --------------------------------------------------------------------------- #
# stage 5 -- validator
# --------------------------------------------------------------------------- #


def validator_prompt(ctx: PromptContext) -> str:
    air_rel = rel(ctx.air_path, ctx.project_root)
    manifest_rel = rel(ctx.manifest_path, ctx.project_root)
    out_rel = rel(ctx.validation_path, ctx.project_root)
    return f"""\
Stage 5 of the arch2code pipeline: experimental validation. Run id: {ctx.run_id}

The question is not "does the code run?" but "does the architecture as drawn
hold up?".

  hypotheses to test:  experiment_plan.hypotheses in {air_rel}
  what was generated:  {manifest_rel}
  write the report to: {out_rel}

Procedure:

1. Read experiment_plan.hypotheses. They are the script: invent no extra ones
   and skip none of the inconvenient ones.
2. For each hypothesis write an executable test that could falsify it. A
   hypothesis no test can refute is not a hypothesis, it is an opinion -- say so
   in the report.
3. Run the harness by explicit path:
   bash .bob/skills/experiment-harness/scripts/run_experiment.sh {ctx.run_id}
   It does build -> up -> health -> tests -> teardown. The full protocol is in
   .bob/skills/experiment-harness/SKILL.md.
4. Write {out_rel} with, for each hypothesis: VERIFIED / REFUTED /
   INCONCLUSIVE, the exact command, the real (trimmed) output, and what that
   implies for the original drawing.

Honesty rule: never mark anything VERIFIED without execution output pasted into
the report, and never tune a test until it passes. If a test fails because the
AIR was wrong, the result is REFUTED plus a recommendation to go back to stage
2. That is the pipeline working, not the pipeline failing.

Fix loop, strict scope: you may fix implementation bugs (wrong import, wrong
port, missing dependency, startup race), up to 3 attempts per failure. You may
not change the architecture to make a test pass. If the fix would require
changing a component, a connection or a contract, STOP: the AIR is wrong.

Close the report with "What the drawing did not anticipate": the discoveries the
prototype surfaced that should go back into the original diagram.

Exit criterion: {out_rel} exists and every hypothesis in the AIR has a verdict
backed by pasted execution output.
"""


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

_BUILDERS = {
    "intake": intake_prompt,
    "analyst": analyst_prompt,
    "critic": critic_prompt,
    "scaffold": scaffold_prompt,
    "validator": validator_prompt,
}


def _hint_block(hint: str | None) -> str:
    if not hint or not hint.strip():
        return ""
    return (
        "\nContext supplied by the human who uploaded the artifact. Treat it as a\n"
        "hint about what the drawing means, never as a licence to record something\n"
        "the drawing does not show:\n\n"
        f"  {hint.strip()}\n"
    )


def _feedback_block(feedback: str | None) -> str:
    if not feedback or not feedback.strip():
        return ""
    return (
        "\nThis run was sent back by a human at the stage 3 gate. Address this "
        "before anything else:\n\n"
        f"  {feedback.strip()}\n"
    )
