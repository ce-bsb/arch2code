# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# One CrewAI tool per OPERATION the drawing names — not one per system. An agent
# picks a tool by its name and description, so search_orders and cancel_order are
# chosen correctly far more often than one order_api(action=...).
#
# Contract: crewai.tools.BaseTool subclass with name/description/args_schema/_run,
# or the @tool decorator. [DOC] crewai is NOT installed here.
"""
{{tool_description}}
"""

import os

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class {{tool_class}}Input(BaseModel):
    """Typed arguments for {{tool_class}} — this IS the schema the agent fills in."""

    query: str = Field(..., description="{{arg_description}}")


class {{tool_class}}(BaseTool):
    name: str = "{{tool_name}}"
    # The description is the interface the agent reads to decide whether to call
    # this tool. State exactly what it does and any side effect — the agent has
    # nothing else to go on.
    description: str = "{{tool_description}}"
    args_schema: type[BaseModel] = {{tool_class}}Input

    def _run(self, query: str) -> str:
        """Do the work and return a string the agent reads back.

        Credentials and endpoints come from the environment, never a literal in
        this file. A missing one raises loudly rather than calling nowhere.
        """
        base_url = os.getenv("{{env_prefix}}_URL")
        if not base_url:
            raise NotImplementedError(
                "AIR {{component_id}}: {{env_prefix}}_URL is not set, and this "
                "tool's operation was drawn but its endpoint/payload was not "
                "specified. Set the endpoint and implement the call — do not "
                "return a fabricated result."
            )
        raise NotImplementedError(
            "AIR {{component_id}}: implement the '{{tool_name}}' operation against "
            "{{env_prefix}}_URL. The drawing shows this call but not the request "
            "shape; ask for the contract before guessing field names."
        )
