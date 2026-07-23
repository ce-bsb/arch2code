# arch2code: generated from AIR {{run_id}} :: {{component_id}}
# source: {{source_artifact}}  evidence: {{evidence}}
# DO NOT EDIT BY HAND — regenerate via the arch-scaffold mode
#
# POST hook — the 'plugin post' / redaction / output-filter box in the drawing.
# Wired into the Crew as task_callback, it runs AFTER each task produces output,
# so it is where redaction and reshaping belong. A tighter single-task version is
# a Task(guardrail=...) callable returning (ok, value).
#
# [INF] Mapping decision: CrewAI's post hook is task_callback. crewai is NOT
# installed here, so the callback signature is documentation-level.
"""
Post-invoke redaction for the crew generated from AIR {{run_id}}.

Redact LOUDLY: log what was removed. A post-hook that hides a violation hides the
incident too.
"""

from typing import Any


def redaction_post(task_output: Any) -> Any:
    """Reshape / redact a task's output after it is produced.

    Args:
        task_output: the TaskOutput CrewAI passes to task_callback.

    Returns:
        The redacted output. On a policy violation the drawing scopes to output,
        redact the offending content and record that it happened — do not pass it
        through unchanged and do not drop the whole result silently.
    """
    raise NotImplementedError(
        "AIR {{component_id}}: implement the '{{redaction_source_label}}' post-guardrail "
        "(output redaction / reshaping) the drawing shows. Delegate PII detection "
        "to governance/openscale_monitor.py — do not return the output unchanged."
    )
