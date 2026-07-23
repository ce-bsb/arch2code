# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# database -> CHECKPOINTER, cache -> STORE. THE mapping that matters most: in an
# agentic drawing a database is almost always the checkpointer (thread /
# short-term state) and a cache is the store (long-term, cross-thread memory).
# They are PERSISTENCE the graph is CONFIGURED WITH — passed to
# builder.compile(checkpointer=..., store=...) — NOT steps in the flow.
#
# The single wrong turn this prevents: a node called `database` with an edge into
# it. That models a query as a step in the conversation and produces a graph that
# looks right and reasons over its own persistence.
#
# The DB handle NEVER travels in the state. These factories are imported by the
# graph modules at compile time; the connection string comes from the
# environment, and MemorySaver is the honest default when it is absent — state
# loss is then obvious instead of hidden behind a database that was never
# provisioned.
#
# [INF]/[DOC] MemorySaver / SqliteSaver / PostgresSaver, langgraph BaseStore /
# InMemoryStore. langgraph is not installed here; nothing below was executed.
"""Checkpointer (short-term thread state) and store (long-term memory) factories."""

from __future__ import annotations

import os

from langgraph.checkpoint.memory import MemorySaver

# CHECKPOINTER selection: env CHECKPOINTER in {memory, sqlite, postgres, none}.
# Default 'memory' — the honest prototype default.
_CHECKPOINTER = os.getenv("CHECKPOINTER", "{{checkpointer}}")
_STORE = os.getenv("STORE", "{{store}}")


def get_checkpointer():
    """Return the checkpointer for builder.compile(checkpointer=...).

    AIR {{component_id}} (the drawing's database) maps here, NOT to a node.
      * memory   -> MemorySaver: in-process, lost on restart. Loss is visible.
      * sqlite   -> SqliteSaver from CHECKPOINTER_DB_URI (durable, single-node).
      * postgres -> PostgresSaver from CHECKPOINTER_DB_URI (durable, shared).
      * none     -> None: the graph runs without persistence (no resume/HITL).
    """
    if _CHECKPOINTER == "none":
        return None
    if _CHECKPOINTER == "memory":
        return MemorySaver()
    uri = os.getenv("CHECKPOINTER_DB_URI", "")
    if not uri:
        # Do not silently fall back to memory when a durable store was asked for —
        # that hides data loss. Fail loud with the fix.
        raise NotImplementedError(
            "AIR {{component_id}}: CHECKPOINTER={_kind} needs CHECKPOINTER_DB_URI "
            "(e.g. postgresql://... or a sqlite path). Set it, or set "
            "CHECKPOINTER=memory for a prototype.".format(_kind=_CHECKPOINTER)
        )
    if _CHECKPOINTER == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        return SqliteSaver.from_conn_string(uri)
    if _CHECKPOINTER == "postgres":
        from langgraph.checkpoint.postgres import PostgresSaver

        return PostgresSaver.from_conn_string(uri)
    raise ValueError(f"Unknown CHECKPOINTER '{_CHECKPOINTER}' for AIR {{component_id}}.")


def get_store():
    """Return the long-term store for builder.compile(store=...), or None.

    AIR {{component_id}} (the drawing's cache) maps here — cross-thread memory,
    NOT a node the flow reads from and writes to.
      * none     -> None (default; most graphs need no long-term store).
      * memory   -> InMemoryStore (prototype cross-thread memory).
      * postgres -> a durable BaseStore from STORE_DB_URI.
    """
    if _STORE == "none":
        return None
    if _STORE == "memory":
        from langgraph.store.memory import InMemoryStore

        return InMemoryStore()
    if _STORE == "postgres":
        uri = os.getenv("STORE_DB_URI", "")
        if not uri:
            raise NotImplementedError(
                "AIR {{component_id}}: STORE=postgres needs STORE_DB_URI. Set it "
                "or use STORE=memory / STORE=none."
            )
        from langgraph.store.postgres import PostgresStore

        return PostgresStore.from_conn_string(uri)
    raise ValueError(f"Unknown STORE '{_STORE}' for AIR {{component_id}}.")
