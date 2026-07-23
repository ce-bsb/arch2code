# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# OBSERVABILITY — LangSmith (env: LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY) or
# Langfuse (a CallbackHandler passed in the run config) trace every graph run.
# Tracing is enabled by ENVIRONMENT; this module only DECIDES whether tracing is
# on and wires the handler. It reads names, NEVER keys, from code.
#
# The 'LLM-Judge' box's runtime home is HERE (an evaluator over traces — a
# LangSmith evaluator or a Langfuse score), NOT a node on the request path.
#
# [DOC] LangSmith env-var contract, Langfuse langfuse.callback.CallbackHandler.
# Neither is installed here; the shapes come from documentation only.
"""Observability wiring for the {{graph_id}} graph: LangSmith / Langfuse traces."""

from __future__ import annotations

import os

# Where traces go: 'none' | 'langsmith' | 'langfuse'. From the observability param.
BACKEND = os.getenv("OBSERVABILITY", "{{observability}}")


def tracing_enabled() -> bool:
    """LangSmith is on purely via the environment (LANGCHAIN_TRACING_V2=true).
    This just reports whether that switch is set, so code can log it once."""
    return os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"


def get_callbacks() -> list:
    """Return the callback handlers to pass in the graph run config, e.g.
    ``graph.invoke(inp, config={'callbacks': get_callbacks()})``.

    Returns:
        A list of handlers. Empty for LangSmith (it hooks in globally via env) and
        for 'none'; one Langfuse CallbackHandler for the langfuse backend.
    """
    if BACKEND == "langfuse":
        # Langfuse reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
        # from the environment itself — no key is written here.
        if not os.getenv("LANGFUSE_PUBLIC_KEY"):
            raise NotImplementedError(
                "AIR {{component_id}}: OBSERVABILITY=langfuse needs LANGFUSE_PUBLIC_KEY "
                "/ LANGFUSE_SECRET_KEY / LANGFUSE_HOST in the environment. Set them "
                "or use OBSERVABILITY=langsmith / none. Do not hardcode a key."
            )
        from langfuse.callback import CallbackHandler

        return [CallbackHandler()]
    # 'langsmith' hooks in globally through LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY;
    # 'none' means no tracing. Either way, no per-run handler is needed.
    return []
