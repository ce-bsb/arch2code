# arch-validator mode rules

## Evidence or nothing

VERIFIED requires execution output pasted into the report. With no output, the status is
INCONCLUSIVE. A report without evidence is an opinion in report formatting.

## A refutation is a good result

Finding out in the prototype that the drawing does not add up is the whole reason the
prototype exists. Report the refutation with the same clarity you would report a success —
and, more important: with a concrete recommendation back to the drawing.

The anti-pattern to avoid is tuning the test until it passes. That turns stage 5 into a
ceremony that always approves, and a gate that always approves is not a gate.

## Limit of the fix loop

**Allowed**: wrong import, wrong port, missing dependency, startup race, forgotten
environment variable. Up to 3 attempts per failure.

**Not allowed**: adding a component, changing a connection, altering a contract, relaxing
the hypothesis. If the fix requires any of that, STOP and report: the AIR is wrong, and the
repair belongs in stage 2.

## Format

`.arch/run/<run>/validation.md`:

    # Validation of run <run-id>
    ## Summary
    <n> hypotheses: <n> verified, <n> refuted, <n> inconclusive
    ## h1 — <statement>
    Status: VERIFIED | REFUTED | INCONCLUSIVE
    Command: <exact command>
    Output:
    ```
    <real output, trimmed>
    ```
    Implication: <what this says about the original drawing>
    ## What the drawing did not anticipate
    <findings that should go back into the diagram>

## The section that matters most

"What the drawing did not anticipate" is the return path to the human architect. Without
it, the prototype validates and the learning dies in the repository. It is the difference
between generating code and closing the loop with the person who drew it.
