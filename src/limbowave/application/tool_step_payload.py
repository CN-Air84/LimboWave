"""Qt-free tool normalization/display projection for the conversation process."""

from dataclasses import replace
from typing import Any

from limbowave.domain.tool_step import ToolStatus, ToolStep, finish

_DISPLAY_ARGS_LIMIT = 16_000


def prepare_tool_step(step: ToolStep) -> ToolStep:
    if step.display_summary is not None and step.display_detail is not None:
        return step
    summary = step.summary
    if len(summary) > 200:
        summary = summary[:200] + "…"
    args = str(step.args) if step.args else "（无）"
    if len(args) > _DISPLAY_ARGS_LIMIT:
        args = args[:_DISPLAY_ARGS_LIMIT] + "…（内容过长，完整参数保留在审计记录中）"
    outcome = (f"错误：{step.error or '未知'}" if step.status is ToolStatus.ERROR
               else f"结果：{step.result_summary or '（无摘要）'}")
    return replace(
        step, display_summary=summary,
        display_detail=f"参数：{args}\n{outcome}\n耗时：{step.duration_ms}ms",
    )


def prepare_tool_event(
    kind: str, payload: dict[str, Any], previous: ToolStep | None,
    created_at_ms: int, duration_ms: int,
) -> ToolStep:
    step = previous or ToolStep(
        tool_call_id=str(payload.get("toolCallId") or ""),
        name=str(payload.get("toolName") or "tool"),
        created_at_ms=created_at_ms,
        args=dict(payload.get("args") or {}),
    )
    if kind == "tool.end":
        step = finish(
            step, result=payload.get("result"), is_error=bool(payload.get("isError", False)),
            duration_ms=duration_ms,
        )
    return prepare_tool_step(step)
