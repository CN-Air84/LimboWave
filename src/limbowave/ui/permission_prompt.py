"""权限确认框（§11.1 / §11.2）：把一次需确认的工具请求讲给用户听，并收回答复。

答复三选一：

- **拒绝**；
- **允许本次**：只放行这一次调用；
- **本会话允许**：放行并建立一条会话授权——同类请求此后静默放行，
  可在「权限」面板查看、撤销或缩小。只有这条授权**确实能覆盖**本次请求时才提供
  （高影响操作即使有授权也要逐次确认，给「记住」按钮反而误导）。

✕ / Esc / 点面板外部都按拒绝（默认拒绝，合同 GATE-04）。

正文里的工具参数来自模型，一律按**纯文本**显示——富文本会让模型能在确认框里
藏字或排版出假按钮。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import QWidget

from limbowave.domain.permissions import Capability, GrantScope, ToolRequest
from limbowave.ui.floating import FloatingPanel, ask_choice
from limbowave.ui.permissions_dialog import CAPABILITY_TEXT

# 答复的键
DENY = "deny"
ONCE = "once"
SESSION = "session"

# 参数预览上限：写文件的整段内容不该把确认框撑满屏
PARAMS_PREVIEW_CHARS = 300
# 命令要让用户看全再决定；只防极端长度把面板撑出窗口
COMMAND_PREVIEW_CHARS = 2000

# 已作为「路径 / 命令」单列展示的参数，不在参数预览里重复
_SHOWN_AS_SCOPE = frozenset({"path", "command"})


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（已截断，共 {len(text)} 字）"


def _params_preview(params: dict[str, Any]) -> str | None:
    rest = {k: v for k, v in params.items() if k not in _SHOWN_AS_SCOPE}
    if not rest:
        return None
    return _clip(json.dumps(rest, ensure_ascii=False), PARAMS_PREVIEW_CHARS)


def _scope_text(scope: GrantScope) -> str:
    if scope.allowed_paths:
        return "目录 " + ", ".join(scope.allowed_paths)
    if scope.allowed_domains:
        return "域名 " + ", ".join(scope.allowed_domains)
    if scope.capability is Capability.TERMINAL:
        return "不限命令"
    return "（无边界限制）"


def describe_request(
    request: ToolRequest, reason: str, scope: GrantScope | None
) -> tuple[str, str]:
    """确认框的标题与正文。纯函数（可测）。"""
    capability = CAPABILITY_TEXT.get(request.capability, request.capability.value)
    lines = [f"工具：{request.tool_name}"]
    lines.extend(f"路径：{path}" for path in request.scope.paths)
    lines.extend(f"域名：{domain}" for domain in request.scope.domains if domain)
    if request.scope.command:
        lines.append(f"命令：{_clip(request.scope.command, COMMAND_PREVIEW_CHARS)}")
    preview = _params_preview(request.raw_params)
    if preview is not None:
        lines.append(f"参数：{preview}")
    lines.append(f"判定：{reason}")
    if scope is not None:
        lines.append("")
        lines.append(
            f"「本会话允许」将授予：{capability} · {_scope_text(scope)}。"
            "高影响操作仍会逐次确认；可在「权限」面板撤销。"
        )
    return f"权限请求：{capability}", "\n".join(lines)


def ask_permission(
    parent: QWidget,
    request: ToolRequest,
    reason: str,
    scope: GrantScope | None,
    on_choice: Callable[[str], None],
) -> FloatingPanel:
    """弹出权限确认框。回调 ``DENY`` / ``ONCE`` / ``SESSION``；关闭按 ``DENY``。

    ``scope`` 为 None 时不提供「本会话允许」。
    """
    title, detail = describe_request(request, reason, scope)
    choices = [(DENY, "拒绝")]
    if scope is not None:
        choices.append((SESSION, "本会话允许"))
    choices.append((ONCE, "允许本次"))
    return ask_choice(
        parent,
        title,
        detail,
        choices,
        lambda key: on_choice(key or DENY),
        accent=ONCE,
        width=440,
    )


__all__ = [
    "DENY",
    "ONCE",
    "SESSION",
    "ask_permission",
    "describe_request",
]
