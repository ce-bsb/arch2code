# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# PRE hook — the 'plugin pre' / guardrails / PII box in the drawing. Wired into
# the Crew as step_callback, it runs on every agent step BEFORE the step's output
# is accepted, so it is where input validation and policy enforcement belong.
#
# [INF] Mapping decision: CrewAI's pre-step hook is step_callback. crewai is NOT
# installed here, so the callback signature is documentation-level.
"""
Pre-invoke guardrail for the crew generated from AIR {{run_id}}.

This is a real guardrail, not a pass-through. Where the actual check needs a
governance service the drawing only named, it delegates to the placeholder in
governance/openscale_monitor.py, which raises loudly — a governance requirement
is never silently skipped.
"""

from typing import Any

# The real check lives behind the governance placeholder — no invented endpoint.
# from governance.openscale_monitor import OpenScaleMonitor


def guardrails_pre(step_output: Any) -> None:
    """Inspect each agent step before its result is accepted.

    Args:
        step_output: the step's action/observation from CrewAI. Treat it as the
            unit to validate (tool input, intermediate reasoning, etc.).

    Raises:
        The turn should be BLOCKED (raise) on a violation — never silently
        allowed. A guardrail that always passes is the failure this profile
        refuses to generate.
    """
    raise NotImplementedError(
        "AIR {{component_id}}: implement the '{{guardrail_source_label}}' pre-guardrail "
        "(PII / policy / input validation) the drawing shows. Delegate the real "
        "check to governance/openscale_monitor.py — do not return a stub that "
        "always passes."
    )
