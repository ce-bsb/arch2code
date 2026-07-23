# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# THE MULTI-LLM ROUTER — the 'API gateway for multiple LLMs' from the drawing,
# expressed as a Runnable, NOT a graph node the agents route through. get_model()
# builds the chat model with init_chat_model and attaches fallbacks with
# .with_fallbacks([...]), so an agent survives one provider being down.
#
# Architectural default: groq/openai/gpt-oss-120b — init_chat_model(
# 'openai/gpt-oss-120b', model_provider='groq'). NOT a legacy watsonx granite id.
# Fallbacks are the backup models the drawing names. Every model is created from
# ENVIRONMENT credentials; init_chat_model reads provider keys from the env — no
# API key is ever written here.
#
# [DOC] langchain.chat_models.init_chat_model, Runnable.with_fallbacks. langchain
# is not installed here; the signature comes from documentation only.
"""Multi-LLM router: one primary model with fallbacks, built from the environment."""

from __future__ import annotations

import os

from langchain.chat_models import init_chat_model

# Architectural default. Overridable per deployment via the environment, but the
# fallback IS the default, never a legacy granite id.
PRIMARY_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
PRIMARY_PROVIDER = os.getenv("LLM_PROVIDER", "groq")

# Optional backups the drawing named (a primary+backup or an LLM-gateway box).
# Comma-separated "provider:model" pairs, e.g. "openai:gpt-4o-mini,anthropic:claude-3-5-haiku".
FALLBACK_SPEC = os.getenv("LLM_FALLBACKS", "")

# Shared generation settings; keep temperature 0 for routing/tool-use reliability.
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))


def _build(provider: str, model: str):
    """Build one chat model. Credentials are read from the environment by
    init_chat_model based on the provider (e.g. GROQ_API_KEY, OPENAI_API_KEY)."""
    return init_chat_model(model, model_provider=provider, temperature=TEMPERATURE)


def get_model():
    """Return the chat model the agents use: the primary with fallbacks attached.

    Returns:
        A Runnable chat model. Calling it tries the primary first and falls back
        to each backup in order if a provider errors — the multi-LLM gateway,
        expressed as a Runnable rather than a component the graph calls.
    """
    primary = _build(PRIMARY_PROVIDER, PRIMARY_MODEL)
    fallbacks = []
    for pair in (p.strip() for p in FALLBACK_SPEC.split(",") if p.strip()):
        provider, _, model = pair.partition(":")
        if not (provider and model):
            raise ValueError(
                f"AIR {{component_id}}: LLM_FALLBACKS entry '{pair}' must be "
                "'provider:model'. Fix the env var; do not guess a model id."
            )
        fallbacks.append(_build(provider, model))
    return primary.with_fallbacks(fallbacks) if fallbacks else primary
