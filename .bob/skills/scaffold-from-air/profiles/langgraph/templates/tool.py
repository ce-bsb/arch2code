# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# tool -> @tool FUNCTION. A tool is a plain function decorated with @tool from
# langchain_core.tools. The docstring and the type hints ARE the schema the model
# reads to choose and call it — an untyped argument is one the model must guess.
#
# Tools are NOT graph nodes. They are collected into a list and passed to
# create_react_agent(model, tools=[...]) or model.bind_tools([...]); the react
# loop / ToolNode calls them. One function per OPERATION, not one per system:
# `create_order` and `cancel_order` are chosen far more reliably than
# `order_api(action=...)`.
#
# Credentials and endpoints come from the environment at call time, NEVER from a
# literal in this file.
#
# [DOC] langchain_core.tools.tool. langgraph/langchain are not installed here.
"""Tools for {{component_id}}: {{tool_description}}"""

from __future__ import annotations

import os

import requests
from langchain_core.tools import tool

# Endpoint from the environment; empty until the deployment sets it.
BASE_URL = os.getenv("{{env_prefix}}_URL", "")
TIMEOUT_SECONDS = 30


@tool
def {{tool_name}}(record_id: str) -> str:
    """{{tool_docstring}}

    Args:
        record_id: the identifier, exactly as the user gave it.

    Returns:
        Human-readable text the model reasons over. Keep it readable on its own —
        the model may quote it straight back to the user.
    """
    if not BASE_URL:
        # Loud, actionable, names the AIR element that asked for this.
        raise NotImplementedError(
            "AIR {{component_id}}: {{env_prefix}}_URL is not set. Set it in the "
            "environment before running the graph; do not hardcode an endpoint."
        )
    response = requests.get(f"{BASE_URL}/records/{record_id}", timeout=TIMEOUT_SECONDS)
    if response.status_code == 404:
        return f"No record found with id {record_id}."
    response.raise_for_status()
    return response.text


@tool
def {{tool_name}}_write(summary: str, description: str) -> str:
    """{{tool_write_docstring}}

    Args:
        summary: one-line summary of the request.
        description: the full description, in the user's own words.

    Returns:
        Text describing the outcome (e.g. the new identifier).
    """
    # The drawing named this call but not the payload the system expects. Do NOT
    # guess field names — a plausible-but-wrong payload fails at demo time.
    raise NotImplementedError(
        "AIR {{component_id}}: the drawing shows this write but not the request "
        "contract. Ask for the API shape before implementing; do not fabricate a "
        "payload or a return value."
    )
