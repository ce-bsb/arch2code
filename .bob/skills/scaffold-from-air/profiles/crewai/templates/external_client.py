# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# An external system the crew talks to becomes a BaseTool holding the client. The
# crew never sees a sub-crew — only the tool. One tool class per OPERATION the
# drawing names, with base URL and credentials read from the environment.
#
# Contract: crewai.tools.BaseTool. [DOC] crewai is NOT installed here.
"""
Client tool for the external system AIR {{component_id}}.
"""

import os

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# Base URL and credential come from the environment — never a literal here.
BASE_URL = os.getenv("{{env_prefix}}_URL", "")
API_KEY_ENV = "{{env_prefix}}_API_KEY"
TIMEOUT_SECONDS = 30


class {{tool_class}}Input(BaseModel):
    record_id: str = Field(..., description="The identifier to fetch, exactly as the user gave it.")


class {{tool_class}}(BaseTool):
    name: str = "{{tool_name}}"
    description: str = "{{tool_description}}"
    args_schema: type[BaseModel] = {{tool_class}}Input

    def _run(self, record_id: str) -> str:
        if not BASE_URL:
            raise NotImplementedError(
                "AIR {{component_id}}: {{env_prefix}}_URL is not set. Configure the "
                "external system's base URL in the environment before running."
            )
        api_key = os.getenv(API_KEY_ENV)
        if not api_key:
            raise NotImplementedError(
                f"AIR {{component_id}}: {API_KEY_ENV} is not set. The credential "
                "comes from the environment; never hardcode it."
            )
        # The path/payload below is a KNOWN shape only if the drawing gave it.
        # If the endpoint was not specified, do not guess — raise instead.
        response = requests.get(
            f"{BASE_URL}/records/{record_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return f"No record found with id {record_id}."
        response.raise_for_status()
        return response.text
