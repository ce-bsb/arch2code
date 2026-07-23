# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# One CrewAI Agent per agent component. The builder function is named after the
# AIR component id, so src/crew.py wires this agent back to the exact box in the
# drawing.
#
# Contract: crewai.Agent(role, goal, backstory, llm, tools, allow_delegation).
# [DOC]/[NV] crewai is NOT installed on the machine that authored this profile —
# the shape comes from documentation, not introspection.
#
# MAPPING REMINDERS the scaffold must honour:
#   * role/goal/backstory come from the component's name + responsibilities[].
#     The backstory is behaviour, not decoration: it is the strongest lever on
#     how this agent reasons.
#   * A supervisor/manager is NOT one more peer: it sets allow_delegation=True and
#     is passed to the Crew as manager_agent (Process.hierarchical). The Crew
#     decides delegation; this file only marks the agent as able to delegate.
#   * An 'LLM' box inside the agent is this agent's llm=. Default model:
#     groq/openai/gpt-oss-120b. When a multi-LLM gateway is drawn, take it from
#     src/llm/router.py get_llm() instead of the inline LLM below.
#   * A guardrails / PII / LLM-Judge box is NOT an agent — callbacks + eval.
"""
{{agent_description}}
"""

from crewai import LLM, Agent

# Tools this agent may call, imported from src/tools/ by id:
# from src.tools.{{tool_module}} import {{tool_symbol}}
# Knowledge sources attached to this agent, imported from src/knowledge/ by id:
# from src.knowledge.{{knowledge_module}} import build_{{knowledge_id}}


def build_{{component_id}}(tools=None, knowledge_sources=None) -> Agent:
    """Construct the '{{agent_role}}' agent from AIR {{component_id}}.

    Args:
        tools: BaseTool instances from src/tools/ this agent may call.
        knowledge_sources: knowledge sources from src/knowledge/ to retrieve from.

    Returns:
        A configured crewai.Agent. src/crew.py wires it into the Crew.
    """
    # Architectural default model; key read from the environment by litellm,
    # never a literal here. When src/llm/router.py exists (a multi-LLM gateway
    # was drawn), replace this line with:
    #   from src.llm.router import get_llm
    #   llm = get_llm("{{component_id}}")
    llm = LLM(model="{{llm}}")           # default: groq/openai/gpt-oss-120b

    return Agent(
        role="{{agent_role}}",
        goal="{{agent_goal}}",
        backstory="{{agent_backstory}}",
        llm=llm,
        tools=list(tools or []),
        knowledge_sources=list(knowledge_sources or []),
        # True ONLY for the crew's manager/supervisor. A peer agent with
        # delegation on will try to hand work sideways and stall.
        allow_delegation={{allow_delegation}},
        verbose=True,
        # Cap tool-calling loops so a confused agent fails fast.
        max_iter=15,
    )
