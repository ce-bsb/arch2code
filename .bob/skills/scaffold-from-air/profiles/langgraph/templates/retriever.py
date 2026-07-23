# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# knowledge_base -> RETRIEVER TOOL. A knowledge base is a vector store exposed as
# a tool the agent CALLS — not a graph node with an edge into it. The retriever
# returns the retrieved chunks as text the model reasons over; retrieval QUALITY
# is scored separately in eval/ragas/evaluate_rag.py.
#
# The vector-store client and the embeddings model come from the environment.
# Never invent a collection name or an embeddings id the drawing did not give,
# and never ship a retriever over an empty/unprovisioned index and call it done —
# an empty index answers every question with silence and the model gets blamed.
#
# [DOC] langchain vector store .as_retriever / @tool wrapping similarity_search.
# langchain is not installed here; nothing below was executed.
"""Retriever tool for knowledge base {{component_id}}: {{kb_description}}"""

from __future__ import annotations

import os

from langchain_core.tools import tool

# Index coordinates from the environment; empty until provisioned.
VECTOR_STORE_URI = os.getenv("{{env_prefix}}_VECTOR_URI", "")
COLLECTION = os.getenv("{{env_prefix}}_COLLECTION", "")
EMBEDDINGS_MODEL = os.getenv("{{env_prefix}}_EMBEDDINGS", "")
TOP_K = 4


@tool
def search_{{kb_name}}(query: str) -> str:
    """{{kb_docstring}}

    Search the {{component_id}} knowledge base for passages relevant to the query
    and return them as text for the model to ground its answer in.

    Args:
        query: the user's question, or a focused sub-question to retrieve for.

    Returns:
        The top passages as a single text block. If nothing is retrieved, say so
        plainly so the model does not fabricate an answer.
    """
    if not (VECTOR_STORE_URI and COLLECTION and EMBEDDINGS_MODEL):
        raise NotImplementedError(
            "AIR {{component_id}}: the vector index is not configured. Set "
            "{{env_prefix}}_VECTOR_URI / {{env_prefix}}_COLLECTION / "
            "{{env_prefix}}_EMBEDDINGS and provision the index before use. Do NOT "
            "guess a collection name or embeddings model, and do not return a fake "
            "passage — an unprovisioned retriever must fail loudly."
        )
    # Wire the concrete client (Milvus / Elasticsearch / pgvector / ...) the
    # drawing named, build a retriever with k=TOP_K, and join the page_content of
    # the results. The exact client is a dependency in pyproject.toml.
    raise NotImplementedError(
        "AIR {{component_id}}: implement retrieval against the configured index "
        "and return the joined passages. The store type comes from the drawing."
    )
