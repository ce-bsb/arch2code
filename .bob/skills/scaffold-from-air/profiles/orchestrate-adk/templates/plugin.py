# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# An ADK agent PLUGIN — a pre- or post-invoke hook, NOT a tool and NOT a service.
# A "plugin pre" / "plugin post" box drawn around an agent's LLM is
# AgentSpec.plugins.agent_pre_invoke / agent_post_invoke. It runs on every turn,
# before or after the model call, and is where guardrails, PII handling and
# output redaction belong. Wire it into the agent YAML under `plugins:`.
#
# Contract: ibm_watsonx_orchestrate.agent_builder plugins (ADK 2.12.0).
"""
{{plugin_description}}

Hook: {{hook}}   # one of: agent_pre_invoke | agent_post_invoke
"""

from typing import Any, Dict


def {{plugin_name}}(context: Dict[str, Any]) -> Dict[str, Any]:
    """{{plugin_docstring}}

    A pre-invoke hook receives the request context before the model is called
    and may validate, enrich or BLOCK it (raise to stop the turn). A post-invoke
    hook receives the model's output and may redact or reshape it.

    Args:
        context: the turn context — messages, user input, and (post-invoke) the
            model output. Treat it as the single source of truth for the turn.

    Returns:
        The (possibly modified) context. Never silently swallow a violation:
        block loudly so the guardrail is visible in the trace.
    """
    # NOTE: this is a governance/guardrail hook. It must not be a plausible-
    # looking fake. Where it depends on a service the drawing only named
    # (watsonx.governance / OpenScale / an LLM-Judge), call the placeholder in
    # governance/ or tools/ and raise NotImplementedError naming the AIR id and
    # the missing endpoint — never invent an API shape.
    raise NotImplementedError(
        "{{component_id}}: implement the {{hook}} hook. "
        "It maps to the '{{plugin_source_label}}' box in the drawing and must "
        "enforce it (e.g. PII/HAP filtering, price redaction, an LLM-Judge check) "
        "against the real governance client, not a stub that always passes."
    )
