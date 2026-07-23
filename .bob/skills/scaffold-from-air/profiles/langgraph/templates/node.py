# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# service -> NODE. A node is a callable taking the state and returning a PARTIAL
# state update — a dict of ONLY the keys it changed, never the whole state and
# never None. The node's registered name in the graph is the AIR component id, so
# traceability from graph to drawing is 1:1.
#
# What a node is NOT (see must_not in target.yaml):
#   * NOT where the database connection is opened — that is the checkpointer,
#     configured at compile() in src/checkpointer.py.
#   * NOT where a human wait lives — that is interrupt_before / interrupt, not
#     time.sleep.
#   * NOT a place to mutate the state object in place — return the delta dict.
#
# [INF] service -> node is a profile mapping decision. langgraph is not installed
# here, so nothing below was executed against the real framework.
"""Node {{component_id}}: {{node_description}}"""

from __future__ import annotations

from src.state import State


def {{node_name}}(state: State) -> dict:
    """{{node_docstring}}

    Reads what it needs from ``state`` and returns ONLY the keys it changed.
    The graph's reducers merge this delta into the shared state.

    Args:
        state: the shared graph state (see src/state.py).

    Returns:
        A partial ``State`` — the delta, not the whole state.
    """
    # Derived from AIR components[].responsibilities[]; the outgoing connections[]
    # become the calls this node makes (over HTTP, or to a tool the react loop
    # owns). Where the drawing named a call whose contract it did not give, do not
    # invent it:
    raise NotImplementedError(
        "AIR {{component_id}}: implement the work this node does and return the "
        "state keys it changes. The drawing named the step but not its concrete "
        "contract (endpoint / payload) — wire the real call, do not guess a URL "
        "or a response shape."
    )


# Async variant — use this signature instead when the drawing declared async and
# the graph is invoked with ainvoke/astream. Keep ONE of the two, matching the
# sync mode the drawing declared.
#
# async def {{node_name}}(state: State) -> dict:
#     ...
