# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# One knowledge SOURCE per knowledge_base component. A knowledge base is not an
# agent and not a tool — it is a passive retrieval source attached to an Agent or
# the Crew via knowledge_sources=[...].
#
# Contract: crewai.knowledge.source.* — StringKnowledgeSource /
# TextFileKnowledgeSource / PDFKnowledgeSource. [DOC] crewai is NOT installed here.
"""
{{knowledge_description}}
"""

from crewai.knowledge.source.text_file_knowledge_source import (
    TextFileKnowledgeSource,
)


def build_{{component_id}}():
    """Build the knowledge source for AIR {{component_id}}.

    The documents come from the drawing's document list — file paths relative to
    the project root. NEVER fabricate the corpus: an agent that retrieves invented
    facts is worse than one with no knowledge base, so if the documents are
    unknown this raises instead of shipping made-up content.
    """
    # File paths from the drawing. crewai resolves them under a knowledge/ dir.
    file_paths = [
        # "{{document_path}}",
    ]
    if not file_paths:
        raise NotImplementedError(
            "AIR {{component_id}}: the drawing shows a knowledge base but not "
            "which documents it holds. Provide the document paths — do not "
            "fabricate a corpus."
        )
    return TextFileKnowledgeSource(file_paths=file_paths)
