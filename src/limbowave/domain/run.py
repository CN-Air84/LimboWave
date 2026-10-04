"""运行记录：一轮请求的生命周期。

一轮（run）= 一次用户输入引发的完整处理，可能包含多次 provider 请求（工具调用会多次往返）。

状态语义（Phase 1B 验收）：

- ``RUNNING``：进行中。
- ``COMPLETED``：正常完成。
- ``ABORTED``：用户主动停止；保留部分输出。
- ``FAILED``：Provider 或内核报错。
- ``INTERRUPTED``：Runtime 异常退出（Phase 1C 负责据此恢复，本阶段只负责如实落盘）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.ABORTED, RunStatus.FAILED, RunStatus.INTERRUPTED}
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一轮运行的记录。

    ``user_message_id`` 在创建时即确定，因此即便内核启动失败，这轮运行与用户输入
    仍然留在应用记录里——**不静默删除用户输入**。
    """

    id: str
    conversation_id: str
    branch_id: str
    status: RunStatus
    created_at: datetime
    user_message_id: str
    assistant_message_id: str | None = None
    finished_at: datetime | None = None
    stop_reason: str | None = None
    error: str | None = None
    # 手动重试的来源用户消息：运行与审计仍独立，聊天展示按此关联原位替换。
    retry_of_message_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


@dataclass(frozen=True, slots=True)
class RunLogSummary:
    """List metadata only; no encrypted error, request, response or event payload."""

    id: str
    status: RunStatus
    created_at: datetime
    transport_count: int
