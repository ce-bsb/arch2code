# arch-scaffold mode rules

## Traceability in every file

    # arch2code: generated from AIR <run_id> :: <component_id>
    # source: <path of the original artifact>  evidence: <bbox|cell>
    # DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode

Plus `.arch/build/<run>/manifest.json` with `component_id -> [files]`.
At a regulated client, "where did this service come from?" is a question with
consequences. With no manifest, the answer is "the AI generated it" — and that
survives no review anywhere.

## Only what the AIR asks for

No extra component, no cache "that would help", no retry "because it is good practice",
no observability nobody asked for. Scope belongs to the AIR. If you think something is
missing, report it to the orchestrator — that call belongs to the analyst and the human,
not to you.

## Honest stub

    def processar_pedido(evento):
        raise NotImplementedError(
            "AIR 20260716-1430-pedidos :: svc_faturamento — "
            "billing rule not specified in the drawing (see unknowns u_cobranca)"
        )

Never `pass`. Never `return {}`. Never a silent `# TODO`.

A silent stub lies about how complete the prototype is: someone brings it up, sees
200 OK, and demos a flow that does not exist to the client. A loud failure carrying the
AIR id says exactly what is missing and where to go ask.

## Runnable from the first commit

- Every component comes up on its own and answers `/health`. Without that, stage 5 measures nothing.
- Every external dependency has a local stub — the prototype runs offline, on a laptop, with no VPN.
- `docker-compose.yml` brings everything up with `make up`.
- `.env.example` documents every variable. `.env` in `.gitignore`. Zero secrets.

## AIR → code mapping

| `kind` | Generates |
|---|---|
| `service` | app + `/health` + config + Dockerfile |
| `ui` | minimal HTTP client (not a real front end — that is out of scope) |
| `gateway` | router with the routes of the OpenAPI derived from the connections |
| `database` | container + schema/DDL + migration + repository |
| `queue`/`topic` | broker container + producer + consumer + event schema |
| `cache` | container + client |
| `external` | **local stub**, never a real call |
| `actor` | test script that exercises the flow |
| `unknown` | **do not generate**. Go back to the analyst. |
