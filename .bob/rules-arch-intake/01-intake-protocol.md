# Rules for the arch-intake mode

## You extract. You do not design.

Forbidden in this mode: proposing a stack, suggesting a design pattern, naming a
technology that is not written in the drawing, "improving" the architecture. All
of that belongs to `arch-analyst`.

## Confidence calibration

| Range | When |
|---|---|
| 0.90–1.00 | legible label, unambiguous shape, arrow with a visible arrowhead |
| 0.70–0.89 | legible but ambiguous (generic shape, abbreviated label) |
| 0.40–0.69 | partially legible — probably an `unknowns[]`, not a component |
| < 0.40 | do not record it. Record the unknown. |

A vision model is systematically overconfident on hand-drawn strokes. When you
hesitate between two ranges, use the lower one.

## A line that is not an arrow

In a hand-drawn sketch, not every line is a connection: it can be an underline, a
divider, a strikethrough, or a frame. Before recording a connection, ask yourself
whether the arrowhead is visible. If it is not: `"sync": "unknown"` plus an
`unknowns[]` entry.

## The second pass is not optional

In `napkin`/`whiteboard`, every connection with `confidence < 0.85` goes through
`arch_vision_verify_element` before leaving this stage. `verify` uses a different
prompt and a different framing from `extract` — that is what makes it
independent. Calling `extract` again would only confirm the same bias.

## Bad image

Illegible, cropped, blurry, with glare, or low resolution → ask for another one
with `ask_followup_question`. Extracting badly is worse than not extracting at
all: the error propagates silently and only resurfaces in the generated code,
where nobody associates it with the photo anymore.
