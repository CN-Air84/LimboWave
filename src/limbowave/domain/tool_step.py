"""工具步骤（Phase 9 / 设计计划 §三.2）。

一次工具调用在助手消息里呈现为一个「步骤」：

- **折叠态**：名称 + 状态 + 简短摘要（设计计划：默认折叠，只显示名称、状态和摘要）；
- **展开态**：参数、返回范围、结果、耗时、错误。

数据来源是内核事件（Pi 的 ``tool_execution_start`` / ``tool_execution_end``）：
start 带 ``toolCallId``/``toolName``/``args``，end 带 ``toolCallId``/``toolName``/
``result``/``isError``。按 ``toolCallId`` 配对，耗时由应用侧计时得出。

**这些是审计数据**：设计计划要求「隐藏只影响展示，不删除审计数据」——
所以工具步骤随消息落库（加密），隐藏开关只作用于界面。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Any

# 结果摘要的截断长度：折叠态与展开态都读它，过长会撑爆界面
RESULT_SUMMARY_CHARS = 200


class ToolStatus(enum.StrEnum):
    """工具步骤状态。"""

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolStep:
    """一次工具调用的审计记录。不可变；配对结束事件时用 :func:`finish` 派生。"""

    tool_call_id: str
    name: str
    status: ToolStatus = ToolStatus.RUNNING
    created_at_ms: int = 0  # 相对本轮开始的毫秒数（便于跨进程比较）
    duration_ms: int = 0
    args: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    error: str | None = None
    # Transient backend projection; deliberately excluded from audit JSON/equality.
    display_summary: str | None = field(default=None, compare=False, repr=False)
    display_detail: str | None = field(default=None, compare=False, repr=False)

    @property
    def summary(self) -> str:
        """折叠态的一行摘要：状态 + 参数要点或错误。"""
        if self.display_summary is not None:
            return self.display_summary
        if self.status is ToolStatus.ERROR:
            return self.error or "执行失败"
        if self.result_summary:
            return self.result_summary
        if self.args:
            return _args_summary(self.args)
        return ""

    def to_json(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "status": self.status.value,
            "created_at_ms": self.created_at_ms,
            "duration_ms": self.duration_ms,
            "args": self.args,
            "result_summary": self.result_summary,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ToolStep:
        return cls(
            tool_call_id=str(data.get("tool_call_id", "")),
            name=str(data.get("name", "")),
            status=ToolStatus(str(data.get("status", "ok"))),
            created_at_ms=int(data.get("created_at_ms", 0)),
            duration_ms=int(data.get("duration_ms", 0)),
            args=dict(data.get("args") or {}),
            result_summary=str(data.get("result_summary", "")),
            error=data.get("error"),
        )


def finish(
    step: ToolStep,
    *,
    result: Any = None,
    is_error: bool = False,
    duration_ms: int = 0,
) -> ToolStep:
    """用结束事件补全一个步骤。返回新对象（不可变）。"""
    return replace(
        step,
        status=ToolStatus.ERROR if is_error else ToolStatus.OK,
        duration_ms=duration_ms,
        display_summary=None,
        display_detail=None,
        result_summary="" if is_error else _summarize_result(result),
        error=_error_text(result) if is_error else None,
    )


def _summarize_result(result: Any) -> str:
    """结果摘要。**不内联大段内容**——只给一个可读的短描述。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return _truncate(result)
    if isinstance(result, dict):
        # 常见形状：{content: [...]} / {stdout: "..."} / {output: "..."}
        for key in ("output", "stdout", "text", "content", "summary"):
            if key in result:
                return _truncate(str(result[key]))
        return _truncate(", ".join(sorted(result)) or "（空结果）")
    if isinstance(result, list):
        return f"{len(result)} 项结果"
    return _truncate(str(result))


def _error_text(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("error", "errorMessage", "message"):
            if key in result:
                return _truncate(str(result[key]))
    if result is None:
        return "工具执行失败"
    return _truncate(str(result))


def _args_summary(args: dict[str, Any]) -> str:
    """参数摘要：只列键与前几个短值，避免把长参数摊在折叠行上。"""
    parts: list[str] = []
    for key in sorted(args)[:3]:
        value = args[key]
        text = _truncate(str(value), limit=40)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _truncate(text: str, limit: int = RESULT_SUMMARY_CHARS) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"
