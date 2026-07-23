# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# GUARDRAILS PRE — a 'plugin pre' / 'guardrails' / 'PII' box drawn around an
# agent's LLM is a wrapper NODE placed BEFORE the model node. It validates or
# enriches the state and may BLOCK the turn by routing to a refusal / END instead
# of the model. Mirrors the ADK agent_pre_invoke plugin.
#
# It is a node ON the path, NEVER a tool the model may choose to skip. And it
# never fakes a "safe" verdict: where it depends on a real classifier / governance
# service the drawing only named, it calls governance/openscale_monitor.py and
# raises loudly.
#
# For a human-in-the-loop checkpoint prefer interrupt_before=[<model_node>] at
# compile() over a busy-wait node.
#
# [INF] Composition of LangGraph primitives, not a framework feature. langgraph is
# not installed here.
"""Pre-model guardrail node for AIR {{component_id}}: {{guardrail_description}}"""

from __future__ import annotations

from langgraph.graph import END  # noqa: F401  # used by the blocking route below

from governance.openscale_monitor import OpenScaleMonitor
from src.state import State

_monitor = OpenScaleMonitor()

# The route a blocked turn takes. Wire this into the graph as a conditional edge
# out of the guardrail node: safe -> the model node, blocked -> a refusal or END.
BLOCKED = "blocked"
ALLOWED = "allowed"


def guardrail_pre(state: State) -> dict:
    """Run BEFORE the model. Returns a partial state update carrying the verdict.

    Args:
        state: the shared graph state (see src/state.py).

    Returns:
        A delta setting state['route'] to ALLOWED or BLOCKED. A conditional edge
        reads it: ALLOWED continues to the model, BLOCKED short-circuits to a
        refusal or END. Never let an unsafe turn through silently.
    """
    # The guardrail this box draws (PII / HAP / jailbreak) is enforced against the
    # real governance client, which is a loud placeholder until wired. Do NOT
    # return {'route': ALLOWED} unconditionally — a guardrail that always passes
    # is worse than none, because it reads as compliant.
    last = state.get("messages", [])[-1] if state.get("messages") else None
    _monitor.check_pii(getattr(last, "content", "") if last else "")
    raise NotImplementedError(
        "AIR {{component_id}}: implement the pre-model guardrail this box draws "
        "(PII / HAP / jailbreak) against the governance client, then return "
        "{'route': ALLOWED} or {'route': BLOCKED}. governance/openscale_monitor.py "
        "is a placeholder until its endpoint is wired — do not fake a verdict."
    )


def route_guardrail(state: State) -> str:
    """Conditional-edge function: send ALLOWED turns to the model, BLOCKED to END.
    Every returned value must be a key of the mapping passed to
    add_conditional_edges (e.g. {ALLOWED: '<model_node>', BLOCKED: END})."""
    return state.get("route", BLOCKED)
