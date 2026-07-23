# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# gateway -> CONDITIONAL EDGE. A gateway does NOT become a node that calls the
# next service itself; that would hide the branch from the graph and the
# checkpointer could not resume mid-decision. It becomes a routing FUNCTION plus
# builder.add_conditional_edges(source, route_fn, mapping).
#
# The routing function returns a KEY of the mapping; the mapping resolves that key
# to the next node name (or END). EVERY value the function can return must be a
# key of the mapping, or LangGraph raises `Found edge starting at unknown node`.
#
# [INF] gateway -> conditional edge is a profile mapping decision. Not executed
# against the framework (langgraph is not installed here).
"""Routing for gateway {{component_id}}: {{router_description}}"""

from __future__ import annotations

from langgraph.graph import END

from src.state import State

# The branches this gateway can take, one per outgoing edge in the drawing.
# Enumerate them from the drawing; the routing function may return only these.
# Map each branch KEY to the next node NAME (or END). Wire it at build time:
#
#   builder.add_conditional_edges("{{component_id}}", route_{{router_name}}, ROUTE_MAP)
#
ROUTE_MAP: dict[str, str] = {
    # "branch_key": "next_node_name",
    # "done": END,   # END is imported above; a terminal branch routes here
}


def route_{{router_name}}(state: State) -> str:
    """Decide the next hop from the current state.

    Args:
        state: the shared graph state (see src/state.py).

    Returns:
        A KEY of ROUTE_MAP. Returning anything not in ROUTE_MAP raises at this
        hop, so the set of return values and the keys of ROUTE_MAP must match
        exactly.
    """
    # The condition[] on the gateway's outgoing connections decides the branch.
    # Read the fields the drawing named off `state`; do not invent a rule the
    # drawing did not show.
    raise NotImplementedError(
        "AIR {{component_id}}: implement the routing decision and return one of "
        f"{sorted(ROUTE_MAP)}. Populate ROUTE_MAP from the gateway's outgoing "
        "edges first; a returned key that is not in ROUTE_MAP crashes the run."
    )
