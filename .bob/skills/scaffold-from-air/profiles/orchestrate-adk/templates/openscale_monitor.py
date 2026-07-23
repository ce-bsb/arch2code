# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# watsonx.governance / OpenScale integration — a PLACEHOLDER by design. The
# drawing names governance (guardrails/PII, monitoring), but the AIR keeps the
# concrete endpoint out of scope, so this must NOT invent an API shape. It is a
# loud NotImplementedError stub naming exactly what to wire and where, so a
# reviewer sees a governance gap instead of a plausible-looking fake.
#
# When implemented, this is where payload logging and the quality/drift/PII
# monitors go, via the watsonx.governance API or the ibm-watson-openscale SDK.
"""
{{governance_description}}

Wire points (fill in against your governance instance):
  - payload logging     : record each agent turn (input, output, model, latency)
  - quality monitor     : drift / accuracy against a labelled feedback set
  - PII / HAP monitor   : the guardrail the '{{governance_source_label}}' box draws
  - fairness (optional) : if a protected attribute is in scope
"""

import os
from typing import Any, Dict


class OpenScaleMonitor:
    """Placeholder client for watsonx.governance / OpenScale.

    Reads its endpoint and key from the environment (never a literal). Every
    method raises until wired, so a governance requirement can never be silently
    skipped.
    """

    def __init__(self) -> None:
        # Set per environment; no secret in code.
        self.url = os.getenv("WXO_GOVERNANCE_URL")
        self.api_key = os.getenv("WXO_GOVERNANCE_APIKEY")

    def log_payload(self, turn: Dict[str, Any]) -> None:
        raise NotImplementedError(
            "{{component_id}}: wire payload logging to watsonx.governance / "
            "OpenScale (ibm-watson-openscale SDK or the governance REST API). "
            "The AIR left the endpoint out of scope; do not invent one."
        )

    def check_pii(self, text: str) -> Dict[str, Any]:
        raise NotImplementedError(
            "{{component_id}}: wire the PII/HAP check the drawing's "
            "'{{governance_source_label}}' box requires. Prefer the native ADK "
            "HAPFilteringConfig for HAP, and this client for governance-side PII."
        )
