# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# One CrewAI Task per unit of work the crew performs. Task ordering + context[]
# encode the connections between agents in the drawing: a downstream task reads
# an upstream task's output through context.
#
# Contract: crewai.Task(description, expected_output, agent, context, guardrail).
# [DOC] crewai is NOT installed here.
"""
Tasks for the crew generated from AIR {{run_id}}.

expected_output is what each agent AIMS AT — it comes from the drawing's
responsibilities/outputs. A vague expected_output produces vague work, so never
leave it empty.
"""

from crewai import Task

# Agents that own these tasks, imported from src/agents/ by id:
# from src.agents.{{agent_module}} import build_{{agent_id}}


def build_tasks(agents: dict) -> list[Task]:
    """Build the ordered task list.

    Args:
        agents: {component_id: Agent} built in src/crew.py, so each task binds to
            the exact agent from the drawing.

    Returns:
        Tasks in pipeline order. In a sequential crew this order IS the flow; in a
        hierarchical crew the manager picks order from these tasks.
    """
    {{first_task_id}} = Task(
        description="{{task_description}}",
        expected_output="{{task_expected_output}}",
        agent=agents["{{owner_agent_id}}"],
    )

    # A downstream task consumes an upstream one via context — this is where a
    # connection between two agents becomes a data dependency:
    # {{second_task_id}} = Task(
    #     description="{{second_task_description}}",
    #     expected_output="{{second_task_expected_output}}",
    #     agent=agents["{{second_owner_agent_id}}"],
    #     context=[{{first_task_id}}],
    #     # Scope a single-step redaction/validation to this task if the drawing
    #     # puts a guardrail on exactly one step:
    #     # guardrail=redact_task_output,
    # )

    return [{{first_task_id}}]
