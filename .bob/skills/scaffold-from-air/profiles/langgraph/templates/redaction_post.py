# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# REDACTION POST — a 'redaction' / 'plugin post' / 'inline LLM-Judge' box drawn
# AFTER an agent's LLM is a wrapper NODE placed after the model node. It reshapes
# or redacts the model output in the state BEFORE it reaches the user (PII
# masking, price/PII redaction). Mirrors the ADK agent_post_invoke plugin.
#
# It returns a partial state update to the output/messages channel; it does NOT
# call the model again unless the drawing shows a judge-and-revise loop. And it
# never lets a redaction gap pass silently: if the rule depends on a service not
# in scope, it raises rather than returning the raw output.
#
# [INF] Composition of LangGraph primitives, not a framework feature. langgraph is
# not installed here.
"""Post-model redaction node for AIR {{component_id}}: {{redaction_description}}"""

from __future__ import annotations

from governance.openscale_monitor import OpenScaleMonitor
from src.state import State

_monitor = OpenScaleMonitor()


def redaction_post(state: State) -> dict:
    """Run AFTER the model. Redacts / reshapes the output before the user sees it.

    Args:
        state: the shared graph state (see src/state.py); the model's output is
            the last message / the answer channel.

    Returns:
        A partial state update carrying the REDACTED output. Never return the raw
        model output when the drawing required redaction.
    """
    # Redact against the real governance client. Do NOT ship a pass-through that
    # returns the raw output — that silently drops the compliance requirement.
    raise NotImplementedError(
        "AIR {{component_id}}: implement the post-model redaction this box draws "
        "(PII / price masking) against the governance client, then return the "
        "redacted output in the messages/answer channel. governance/"
        "openscale_monitor.py is a placeholder until wired — do not return the "
        "raw output as if it were redacted."
    )
