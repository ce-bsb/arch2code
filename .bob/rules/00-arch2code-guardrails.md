# arch2code — project rules (they apply to ALL modes)

Rules apply to every conversation, regardless of the mode, and a custom mode does
not override them. That is why what is cross-cutting lives here, and what is
stage-specific lives in `.bob/rules-<slug>/`.

## 1. Missing information never turns into a silent assumption

If the data is not in the drawing and the human did not say it, it belongs in
`unknowns[]` or in `assumptions[]` with a declared `impact`. Never in
`components[]` or `connections[]`.

This is the axis of the whole pipeline. A model reading a sketch will fill a gap
with whatever "makes sense" — and what makes sense is usually right, which makes
the few times it is wrong practically undetectable in review.

## 2. Every claim about the drawing carries evidence

A normalized `bbox` (vision) or a `cell_id` (drawio). Without evidence it is not
an observation: it is inference, and it goes to `assumptions[]`.

## 3. A structured source takes precedence over vision

If a `.drawio`/`.puml`/`.mmd` exists, the path is the parser. Vision is the last
resort, for when there is no alternative.

## 4. The gates between stages are blocking

No mode approves its own work. No mode edits the artifact of the previous
stage — the fileRegex enforces that structurally, not by good will.

## 5. A prototype is an experiment, not a product

The goal is to falsify hypotheses about the architecture. `out_of_scope` matters
as much as the scope. Real authentication, HA, tuning, and actual tax-invoice
issuance are out by default — unless they are the hypothesis being tested.

## 6. No secrets in a versioned artifact

No apikey, token, or credential in `.bob/mcp.json`, in code, or in the AIR. Only
`.env.example` and a reference to an environment variable.

## 7. Language

Documents, comments, and conversation in English. Code identifiers, slugs, JSON
keys, and file names in English.
