# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# RAGAS evaluation for any knowledge-base / RAG path in the agent. Complements
# the ADK evaluations (which score the agent end to end) with retrieval-quality
# metrics the ADK does not compute. Only emit this when the drawing has a
# knowledge base or a retrieval step; otherwise it is noise.
#
# Contract: ragas (open framework). [DOC] Requires `pip install ragas datasets`.
"""
{{ragas_description}}
"""

# ragas is an optional dependency; fail loudly with the fix if it is missing.
try:
    from ragas import evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )
    from datasets import Dataset
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "ragas is not installed. `pip install ragas datasets` to run RAG "
        "evaluation for {{component_id}}."
    ) from exc


def evaluate_rag(samples: list[dict]) -> dict:
    """Score the RAG path on faithfulness, relevancy and context quality.

    Args:
        samples: rows of {question, answer, contexts (list[str]), ground_truth}.
            Build them from the agent's traces over the evaluation stories.

    Returns:
        The RAGAS metric scores. A low faithfulness score is the signal that the
        agent is answering from the model, not from the retrieved context.
    """
    if not samples:
        raise NotImplementedError(
            "{{component_id}}: provide evaluation samples from the agent's RAG "
            "traces. An empty RAGAS run proves nothing."
        )
    dataset = Dataset.from_list(samples)
    return evaluate(
        dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
    )
