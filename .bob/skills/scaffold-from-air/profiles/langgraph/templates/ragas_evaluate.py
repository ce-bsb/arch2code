# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# RAGAS evaluation for any knowledge-base / retrieval path in the graph. Scores
# retrieval quality (faithfulness, relevancy, context precision/recall) that
# graph-level evaluation does not. Emit ONLY when the drawing has a knowledge base
# or a retrieval step; otherwise it is noise.
#
# Build the samples from the graph's TRACES over the evaluation set: rows of
# {question, answer, contexts, ground_truth}. A low faithfulness score means the
# agent answered from the model, not from the retrieved context.
#
# Contract: ragas (open framework). [DOC] Requires `pip install ragas datasets`.
"""RAGAS retrieval-quality evaluation for AIR {{component_id}}."""

from __future__ import annotations

# ragas is an optional dependency; fail loudly with the fix if it is missing.
try:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "ragas is not installed. `pip install ragas datasets` to run RAG "
        "evaluation for {{component_id}}."
    ) from exc


def evaluate_rag(samples: list[dict]) -> dict:
    """Score the RAG path on faithfulness, relevancy and context quality.

    Args:
        samples: rows of {question, answer, contexts (list[str]), ground_truth}.
            Build them from the graph's traces over the evaluation set.

    Returns:
        The RAGAS metric scores. Low faithfulness is the signal that the agent is
        answering from the model, not from the retrieved context.
    """
    if not samples:
        raise NotImplementedError(
            "AIR {{component_id}}: provide evaluation samples from the graph's RAG "
            "traces. An empty RAGAS run proves nothing."
        )
    dataset = Dataset.from_list(samples)
    return evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
    )
