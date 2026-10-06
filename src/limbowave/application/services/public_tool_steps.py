"""Public display-only tool metadata; never forward arguments/results/host errors."""

from collections.abc import Iterable

from limbowave.domain.tool_step import ToolStep


def public_tool_steps(steps: Iterable[ToolStep]) -> list[dict[str, str]]:
    return [
        {
            "tool_id": step.tool_call_id[:160],
            "name": step.name[:100],
            "status": {"ok": "completed", "error": "failed"}.get(step.status.value, "running"),
        }
        for step in steps
    ]
