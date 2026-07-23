# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# agent -> SUBGRAPH, and (for a supervisor) the router StateGraph over subgraphs.
# The compiled graph is exposed as a MODULE-LEVEL variable named `graph` so
# langgraph.json can point at `src/graphs/{{component_id}}.py:graph`. It is
# compiled at import time — NOT inside a main() the manifest cannot see.
#
# Two shapes live here; keep the one the drawing shows:
#   A) a SPECIALIST agent (one LLM + its tools)  -> create_react_agent(...)
#   B) a SUPERVISOR/orchestrator over specialists -> a StateGraph whose nodes are
#      the specialist subgraphs, wired with conditional edges routed by the
#      supervisor LLM (this is the 'supervisor -> router graph' mapping).
#
# A 'boundary' box grouping several components becomes ONE compiled subgraph added
# as a node in the parent graph. A 'guardrails/plugin-pre' box is a wrapper node
# BEFORE the model (src/guardrails_pre.py); a 'redaction/plugin-post' box is a
# wrapper node AFTER it (src/redaction_post.py); a human-in-the-loop actor is
# interrupt_before=[node] at compile().
#
# [DOC] langgraph.prebuilt.create_react_agent, langgraph.graph.StateGraph.
# langgraph is NOT installed here — nothing below was executed.
"""Graph for agent {{component_id}}: {{graph_description}}"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from llm.router import get_model  # get_model() -> init_chat_model + with_fallbacks
from src.checkpointer import get_checkpointer, get_store
from src.state import State

# --- collect this agent's tools (one import per src/tools/{id}.py) ------------
# from src.tools.{{tool_id}} import {{tool_name}}
TOOLS: list = [
    # {{tool_name}},
]

# The model is NEVER pinned to a literal endpoint here — it comes from the
# multi-LLM router so the agent survives one provider being down. Architectural
# default: groq/openai/gpt-oss-120b (see src/llm/router.py).
MODEL = get_model()


# =============================================================================
# SHAPE A — specialist agent: one LLM that calls its tools.
# create_react_agent returns an ALREADY-COMPILED graph, usable as a subgraph.
# Prefer this when the drawing shows a single agent with tools and no routing.
# =============================================================================
#
# from langgraph.prebuilt import create_react_agent
#
# graph = create_react_agent(
#     MODEL,
#     tools=TOOLS,
#     prompt="{{agent_instructions}}",
#     checkpointer=get_checkpointer(),
#     store=get_store(),
# )


# =============================================================================
# SHAPE B — supervisor over specialists: a router StateGraph.
# Each specialist is a subgraph (its own src/graphs/{id}.py:graph), added as a
# node; the supervisor LLM decides which one runs next via a conditional edge.
# =============================================================================
builder = StateGraph(State)

# Register each specialist subgraph as a node. Import the compiled `graph` from
# its module; the node name is the AIR component id (1:1 traceability).
#   from src.graphs.{{specialist_id}} import graph as {{specialist_id}}_sub
#   builder.add_node("{{specialist_id}}", {{specialist_id}}_sub)


def supervise(state: State) -> dict:
    """Supervisor turn: ask the model which specialist should act next.

    Writes the chosen route into the state; the conditional edge below reads it.
    Returns ONLY the keys it changed (a partial state update).
    """
    raise NotImplementedError(
        "AIR {{component_id}}: implement the supervisor decision. Call MODEL with "
        "the specialists' descriptions and set state['route'] to the id of the "
        "next specialist, or a terminal key. Do NOT hardcode the routing — that "
        "is what the supervisor LLM is for."
    )


def pick_next(state: State) -> str:
    """Map the supervisor's decision to the next node. Every returned value MUST
    be a key of the mapping passed to add_conditional_edges, or the run raises."""
    return state.get("route", "__end__")


builder.add_node("supervisor", supervise)
builder.add_edge(START, "supervisor")

# The mapping enumerates every branch the supervisor can pick, terminal -> END.
# builder.add_conditional_edges(
#     "supervisor",
#     pick_next,
#     {"{{specialist_id}}": "{{specialist_id}}", "__end__": END},
# )
# builder.add_edge("{{specialist_id}}", "supervisor")   # return to the supervisor

# HUMAN-IN-THE-LOOP: if the drawing shows an actor gating an action, stop before
# that node and resume the thread when the human approves:
#   graph = builder.compile(checkpointer=get_checkpointer(), store=get_store(),
#                           interrupt_before=["{{gated_node}}"])
graph = builder.compile(checkpointer=get_checkpointer(), store=get_store())
