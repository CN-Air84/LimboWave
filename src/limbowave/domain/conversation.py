"""会话领域模型：Conversation / Branch / Message。

Phase 1B 的核心区分（ADR-0001 权威边界）：

- **应用实体 ID 是主键**；Pi 的 entry ID 只作为可空的溯源字段（``Message.pi_entry_id``）。
- 用户可见消息（``Message``）与 Pi 运行时条目（``RuntimeEntryMirror``）是两件事，
  前者是权威，后者是镜像。
- 所有实体不可变；状态迁移用 ``dataclasses.replace`` 显式表达。

``MessageRevision`` 仅在编辑/分支落地时才需要，本轮不提前构造版本系统（Phase 1B 边界）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from limbowave.domain.permissions import PermissionPreset
from limbowave.domain.tool_step import ToolStep


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(enum.StrEnum):
    """消息终态。``PARTIAL`` 表示用户停止后保留的部分输出。"""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    title: str
    created_at: datetime
    default_logical_model_id: str | None = None
    permission_preset: PermissionPreset = PermissionPreset.READ_ONLY


@dataclass(frozen=True, slots=True)
class Branch:
    """会话内的分支。Phase 1B 每个会话只有一条主线，但结构上已支持分叉。"""

    id: str
    conversation_id: str
    created_at: datetime
    parent_branch_id: str | None = None
    forked_from_message_id: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """用户可见消息。**应用持有权威**，不依赖向 Pi 查询。"""

    id: str
    conversation_id: str
    branch_id: str
    role: MessageRole
    content: str
    created_at: datetime
    run_id: str | None = None
    status: MessageStatus = MessageStatus.COMPLETE
    thinking: str = ""
    # Pi 的运行时条目 ID：仅供追溯与恢复，**不是应用主键**
    pi_entry_id: str | None = None
    # 白名单标记（§7.3）：压缩时保留原文，压缩模型被明确指示不得压缩
    is_whitelisted: bool = False
    # 工具步骤审计（§三.2）：隐藏开关只影响展示，这些数据始终保留
    tool_steps: tuple[ToolStep, ...] = ()
