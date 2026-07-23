# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# watsonx.governance / OpenScale integration — a PLACEHOLDER by design. The
# drawing names governance (guardrails / PII / monitoring), but the AIR keeps the
# concrete endpoint out of scope, so this must NOT invent an API shape. It is a
# loud NotImplementedError stub naming exactly what to wire and where, so a
# reviewer sees a governance gap instead of a plausible-looking fake. The
# guardrail nodes (src/guardrails_pre.py, src/redaction_post.py) call into it.
#
# When implemented, this is where payload logging and the quality/drift/PII
# monitors go, via the watsonx.governance API or the ibm-watson-openscale SDK.
#
# [INF] Placeholder mirroring the orchestrate-adk governance stub. Nothing here is
# executed against a real governance instance.
"""watsonx.governance / OpenScale placeholder for AIR {{component_id}}."""

from __future__ import annotations

import os
from typing import Any


class OpenScaleMonitor:
    """Placeholder client for watsonx.governance / OpenScale.

    Reads its endpoint and key from the environment (never a literal). Every
    method raises until wired, so a governance requirement can never be silently
    skipped by the guardrail nodes that call it.
    """

    def __init__(self) -> None:
        # Set per environment; no secret in code.
        self.url = os.getenv("WXG_GOVERNANCE_URL")
        self.api_key = os.getenv("WXG_GOVERNANCE_APIKEY")

    def log_payload(self, turn: dict[str, Any]) -> None:
        """Record one graph turn (input, output, model, latency) for drift/quality
        monitoring. Raises until wired to the real governance instance."""
        raise NotImplementedError(
            "AIR {{component_id}}: wire payload logging to watsonx.governance / "
            "OpenScale (ibm-watson-openscale SDK or the governance REST API). "
            "The AIR left the endpoint out of scope; do not invent one."
        )

    def check_pii(self, text: str) -> dict[str, Any]:
        """The PII / HAP check the drawing's governance box requires. Called from
        src/guardrails_pre.py. Raises until wired — it must never return a
        default 'safe' verdict, which would read as compliant while checking
        nothing."""
        raise NotImplementedError(
            "AIR {{component_id}}: wire the PII/HAP check the governance box "
            "requires. Return the classifier's verdict; do not default to 'safe'."
        )
