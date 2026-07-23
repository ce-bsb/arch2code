# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# The Crew — the single entrypoint. It assembles the agents and tasks under a
# process and runs them. This is where the two mappings that decide correctness
# live:
#
#   SUPERVISOR  -> Process.hierarchical + manager_agent=<supervisor>. The manager
#                  is NOT also a worker in agents=[]. A hierarchical crew with no
#                  manager raises at kickoff.
#   PLUGIN PRE  -> step_callback=guardrails_pre (runs on each agent step).
#   PLUGIN POST -> task_callback=redaction_post (runs after each task output).
#
# Contract: crewai.Crew(agents, tasks, process, manager_agent|manager_llm,
# step_callback, task_callback). [DOC] crewai is NOT installed here.
"""
Crew assembled from AIR {{run_id}}.
"""

from crewai import LLM, Crew, Process

from src.agents.{{agent_module}} import build_{{agent_id}}
from src.tasks import build_tasks

# Pre/post hooks — generated only when the drawing shows guardrail/redaction boxes:
# from src.guardrails_pre import guardrails_pre
# from src.redaction_post import redaction_post
# Observability — generated only when the drawing shows a langfuse/tracing box:
# from src.observability.langfuse_tracing import setup_tracing


def build_crew() -> Crew:
    """Build the crew from the drawing.

    Every agent, task, tool and knowledge source here traces to a box in AIR
    {{run_id}}. Nothing is invented.
    """
    # 1. Build agents, keyed by AIR id so tasks bind to the exact box.
    agents = {
        "{{agent_id}}": build_{{agent_id}}(),
        # "{{second_agent_id}}": build_{{second_agent_id}}(),
    }

    # 2. Build tasks against those agents (order encodes the connections).
    tasks = build_tasks(agents)

    # 3. Choose the process. SEQUENTIAL: agents run in task order. HIERARCHICAL:
    #    a manager delegates — and a hierarchical crew MUST have a manager.
    process = Process.{{process}}          # sequential | hierarchical

    crew_kwargs = dict(
        # In a hierarchical crew the manager is NOT listed here — only the workers.
        agents=[a for aid, a in agents.items() if aid != "{{manager_agent_id}}"]
        if process is Process.hierarchical
        else list(agents.values()),
        tasks=tasks,
        process=process,
        verbose=True,
        # Pre/post hooks — uncomment when the drawing shows them:
        # step_callback=guardrails_pre,     # 'plugin pre' / guardrails box
        # task_callback=redaction_post,     # 'plugin post' / redaction box
    )

    if process is Process.hierarchical:
        # Prefer the supervisor Agent the drawing names as the manager; fall back
        # to a manager_llm only when no manager agent was drawn.
        crew_kwargs["manager_agent"] = agents["{{manager_agent_id}}"]
        # crew_kwargs["manager_llm"] = LLM(model="{{manager_llm}}")

    return Crew(**crew_kwargs)


def run(inputs: dict | None = None) -> str:
    """Kick off the crew. `crewai run` calls this (see pyproject.toml scripts)."""
    # setup_tracing()   # uncomment when observability is generated
    result = build_crew().kickoff(inputs=inputs or {})
    return str(result)


if __name__ == "__main__":
    print(run())
