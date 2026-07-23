---
name: experiment-harness
description: Brings up the generated prototype, tests every hypothesis in the AIR's experiment_plan, and produces the validation report with execution evidence. Use in stage 5, after the scaffolding.
---

# Experiment harness (stage 5)

Proves or refutes the AIR's hypotheses with real execution. The question is not
"does the code run?", it is "does the architecture as drawn hold up?".

## Steps

<Steps>
<Step>
Read `experiment_plan.hypotheses` from the AIR. They are the script. Do not invent
extra ones and do not skip the ones that are hard work — the laborious one is
usually the one that matters.
</Step>

<Step>
For each hypothesis, write a test that can **fail**. If no result could refute it,
it is not a hypothesis: report that as a finding and move on.
</Step>

<Step>
Run the full cycle:

    bash .bob/skills/experiment-harness/scripts/run_experiment.sh <run-id>

build → up → health → tests → end-to-end flow → teardown. The output lands in
`.arch/run/<run-id>/harness.log`.
</Step>

<Step>
Write `.arch/run/<run-id>/validation.md`. For each hypothesis: the status
(**VERIFIED / REFUTED / INCONCLUSIVE**), the exact command, the real output
trimmed to the relevant lines, and the implication for the original drawing.
</Step>

<Step>
Close with **"What the drawing did not anticipate"**: the findings that should go
back into the diagram. That section is the return trip to the human architect —
without it the prototype validates and the learning is lost.
</Step>
</Steps>

## Honesty rule

Never mark VERIFIED without execution output pasted into the report. Never tune
the test until it passes.

A refuted hypothesis is the most valuable result the pipeline can produce:
finding out in the prototype that the drawing does not hold is exactly why a
prototype exists. A report with 3 out of 5 hypotheses refuted and evidence is a
good report. One with 5 out of 5 verified and no pasted output is suspicious.

## Fix loop (strict scope)

**You may fix**: a wrong import, a swapped port, a missing dependency, a startup
race. Up to 3 attempts per failure.

**You may not**: change a component, a connection, or a contract to make the test
pass. If the fix requires that, **stop** — it is a sign the AIR is wrong, and the
path is back to stage 2, not around it.

## Files

- `scripts/run_experiment.sh` — build/up/health/test/teardown cycle
