# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# Contract: ibm_watsonx_orchestrate.agent_builder.tools.tool (ADK 2.12.0).
#
# This template is deliberately VALID Python, not a placeholder soup: it is
# compiled by the profile self-test, so the shape below is known to import.
# Rename the module, the functions and the app_id; keep the structure.
#
#   ConnectionType.API_KEY_AUTH  <- correct
#   ConnectionType.API_KEY       <- does not exist, AttributeError on import
#   @expect_credentials          <- does not exist in any installed version
"""One tool module per system the agent talks to.

Rules that decide whether the agent uses these tools correctly:

* One function per OPERATION, not one function per system. The model picks a
  tool by name and docstring, so `create_incident` and `close_incident` are
  chosen correctly far more often than `incident_api(action=...)`.
* The docstring and the type hints ARE the schema the platform publishes.
  An argument with no type hint is an argument the model has to guess.
* Credentials come from the connection named by `app_id`, resolved at call
  time. Nothing secret is ever written in this file.
"""

import os

import requests
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import ToolPermission, tool

# The connection has to exist in the tenant before `orchestrate tools import`.
# `orchestrate connections add -a external_system` creates it.
APP_ID = "external_system"

BASE_URL = os.getenv("EXTERNAL_SYSTEM_URL", "")
TIMEOUT_SECONDS = 30


@tool(
    name="lookup_record",
    description="Fetch one record by its identifier.",
    permission=ToolPermission.READ_ONLY,
    expected_credentials=[
        {"app_id": APP_ID, "type": ConnectionType.API_KEY_AUTH},
    ],
)
def lookup_record(record_id: str) -> str:
    """Fetch a single record from the external system by identifier.

    Args:
        record_id: The record identifier, exactly as the user gave it.

    Returns:
        A JSON string with the record fields, or an explanation of why the
        record could not be read. The agent shows this text to the user, so it
        has to be readable on its own.
    """
    if not BASE_URL:
        # Loud, actionable, and it names the AIR element that asked for this.
        raise NotImplementedError(
            "AIR {{component_id}}: EXTERNAL_SYSTEM_URL is not set. "
            "Set it in the tool's environment before importing the tool."
        )
    response = requests.get(
        f"{BASE_URL}/records/{record_id}",
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return f"No record found with id {record_id}."
    response.raise_for_status()
    return response.text


@tool(
    name="create_record",
    description="Create a record and return its new identifier.",
    permission=ToolPermission.READ_WRITE,
    expected_credentials=[
        {"app_id": APP_ID, "type": ConnectionType.API_KEY_AUTH},
    ],
)
def create_record(summary: str, description: str) -> str:
    """Create a record in the external system.

    Args:
        summary: One-line summary of the request.
        description: The full description, in the user's own words.

    Returns:
        A JSON string containing the identifier of the created record.
    """
    raise NotImplementedError(
        "AIR {{component_id}}: the drawing shows this call but not the payload "
        "the external system expects. Blocking unknown — ask for the API "
        "contract before implementing, do not guess the field names."
    )
