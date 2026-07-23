# Rules for the arch-analyst mode

## The AIR is a contract, not a sketch

It is the only input to `arch-scaffold`. What is not in it does not exist for the
code; what is wrong in it becomes dozens of wrong files.

## The provenance test

Before writing a single line in `components[]` or `connections[]`, answer:
**which pixel or which cell is this in?** No answer → `assumptions[]` or `unknowns[]`.

## Impact is mandatory

Every `assumptions[]` entry declares an `impact`: what breaks in the code if it
is wrong. If you cannot write the impact, you did not understand the assumption —
and it is probably an `unknowns[]`.

## Falsifiable hypothesis

For each item in `experiment_plan.hypotheses`, `falsifiable_by` describes how the
test **fails**. "The system works" is not a hypothesis. "Pedidos returns 201 in
under 500ms with Faturamento down" is.
<!-- "Pedidos" and "Faturamento" are component names read off the reference
     artifacts (svc_pedidos / svc_faturamento); component names are literal
     labels, never translated. -->

## Questions for the human

- At most 5 per round, ordered by how much they unblock.
- Closed, with options. `options: ["Kafka","RabbitMQ","IBM MQ"]` unblocks; "which
  broker?" buys you another round.
- `blocking: true` only when it genuinely prevents generating code. Inflated
  blocking trains the human to ignore blocks.

## Financial services client

The temptation to assume LGPD, PCI-DSS, a 99.99% SLA, or log retention is
strong, and those assumptions look prudent. Do not assume: a nonfunctional
requirement only enters `nonfunctional[]` with `source: "artifact"` (written in
the drawing) or `source: "human"` (said in the conversation). Assuming
compliance is as wrong as ignoring it — in both cases the drawing stops
reflecting somebody's decision.
