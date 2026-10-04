"""应用层事件词汇：控制器与 GUI 之间的稳定契约。

放在独立模块，使 ``RunCoordinator`` 与 ``SessionController`` 共用同一套词汇，
避免任一方成为事实上的定义源。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """面向展示的聊天级事件。"""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


ChatEventHandler = Callable[[ChatEvent], None]

# 事件种类（供订阅方与测试引用，避免散落的字符串字面量）
USER = "user"
ASSISTANT_START = "assistant_start"
ASSISTANT_DELTA = "assistant_delta"
THINKING_DELTA = "thinking_delta"
ASSISTANT_END = "assistant_end"
TOOL = "tool"
SETTLED = "settled"
# 新会话首轮成功回复完成；供隔离的后台命名流程使用，不改变 settled 的空载荷契约。
FIRST_RESPONSE_COMPLETED = "first_response_completed"
ERROR = "error"
# 运行生命周期（Phase 1B 新增：让 GUI 知道这轮落在哪种终态）
RUN_FAILED = "run_failed"
RUN_INTERRUPTED = "run_interrupted"
# 分支（Task 3.2：编辑/重生成产生新分支时通知 GUI 刷新分支标识）
BRANCHED = "branched"
