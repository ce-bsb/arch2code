# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# The graph STATE — the single contract every node shares. Each key a node writes
# is a field here; the graph merges each node's returned partial dict into it.
#
# The one decision that is invisible in a drawing and silently breaks the demo:
# which keys ACCUMULATE (a reducer) versus OVERWRITE (last-writer-wins). The
# `messages` channel MUST use add_messages or every node overwrites the history
# and the agent forgets the conversation. This is the home of question
# q_state_shape.
#
# Contract: typing.TypedDict + typing.Annotated reducers, langgraph.graph.message
# .add_messages. [DOC] LangGraph documentation — langgraph is not installed here.
"""State schema for the {{graph_id}} graph."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class State(TypedDict, total=False):
    """What flows between the nodes of the {{graph_id}} graph.

    Reducer rules (do not "simplify" these away):
      * ``messages`` uses ``add_messages`` — it APPENDS and de-duplicates by id.
        A plain type here would overwrite history on every node and the agent
        would forget the conversation. Silent, always blamed on the model.
      * a growing list that is not chat history uses ``Annotated[list, operator.add]``.
      * a plain field (no Annotated) is last-writer-wins — the right choice for a
        scalar the latest node should own (a route decision, a final answer).
    """

    # Chat history — accumulates. NEVER give this a plain (non-Annotated) type.
    messages: Annotated[list, add_messages]

    # Accumulating evidence / retrieved context — appends across nodes.
    context: Annotated[list, operator.add]

    # Last-writer-wins scalars. Add one field per state key the drawing named
    # (from question q_state_shape); keep only what nodes actually read/write.
    route: str  # the branch a gateway node chose; consumed by a conditional edge
    answer: str  # the terminal node's final answer to the caller

    # Free-form scratch a node may stash for a later node. Prefer explicit keys.
    scratch: dict[str, Any]
