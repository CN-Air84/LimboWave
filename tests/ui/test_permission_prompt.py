"""权限确认框（ui/permission_prompt.py）。

锁住的行为：
- 标题说能力、正文说工具与**解析后的**目标，不再把协议前缀和原始 JSON 丢给用户；
- 能建会话授权时才有「本会话允许」，并写明授出去的边界；
- 关闭（✕/Esc/点外部）一律按拒绝；
- 大段参数截断，不把面板撑满屏。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QPushButton, QWidget
from pytestqt.qtbot import QtBot

from limbowave.domain.permissions import (
    Capability,
    GrantScope,
    ResourceScope,
    ToolRequest,
)
from limbowave.ui.permission_prompt import (
    DENY,
    ONCE,
    PARAMS_PREVIEW_CHARS,
    SESSION,
    ask_permission,
    describe_request,
)

ROOT = str(Path("C:/ws/LimboWave"))


def _list_request() -> ToolRequest:
    return ToolRequest(
        tool_name="list_directory",
        capability=Capability.FILE_READ,
        scope=ResourceScope(paths=(ROOT,)),
        raw_params={},
    )


def _host(qtbot: QtBot) -> QWidget:
    host = QWidget()
    host.resize(1000, 800)
    qtbot.addWidget(host)
    host.show()
    return host


def _buttons(panel: QWidget) -> dict[str, QPushButton]:
    return {b.text(): b for b in panel.findChildren(QPushButton) if b.text() != "✕"}


def test_describe_speaks_capability_and_resolved_target() -> None:
    scope = GrantScope(Capability.FILE_READ, allowed_paths=(ROOT,))
    title, detail = describe_request(_list_request(), "该能力尚无会话授权", scope)
    assert title == "权限请求：文件读取"
    assert "limbowave.gate" not in title + detail
    assert "list_directory" in detail
    assert f"路径：{ROOT}" in detail
    assert "判定：该能力尚无会话授权" in detail
    # 用户点「本会话允许」之前就能看到授出去的边界
    assert f"文件读取 · 目录 {ROOT}" in detail


def test_describe_without_scope_has_no_session_note() -> None:
    _title, detail = describe_request(_list_request(), "高影响操作需二次确认：x", None)
    assert "本会话允许" not in detail


def test_describe_terminal_shows_command_once() -> None:
    request = ToolRequest(
        tool_name="user_bash",
        capability=Capability.TERMINAL,
        scope=ResourceScope(command="Get-Date"),
        raw_params={"command": "Get-Date"},
    )
    title, detail = describe_request(request, "该能力尚无会话授权", GrantScope(Capability.TERMINAL))
    assert title == "权限请求：终端执行"
    assert detail.count("Get-Date") == 1  # 命令单列展示，参数预览里不重复
    assert "终端执行 · 不限命令" in detail


def test_describe_clips_long_params() -> None:
    request = ToolRequest(
        tool_name="create_file",
        capability=Capability.FILE_WRITE,
        scope=ResourceScope(paths=(ROOT + "/a.txt",)),
        raw_params={"path": "a.txt", "content": "x" * 10_000},
    )
    _title, detail = describe_request(request, "该能力尚无会话授权", None)
    params_line = next(line for line in detail.splitlines() if line.startswith("参数："))
    assert len(params_line) < PARAMS_PREVIEW_CHARS + 50
    assert "已截断" in params_line


def test_ask_permission_offers_session_choice_when_grantable(qtbot: QtBot) -> None:
    host = _host(qtbot)  # 局部变量持有：qtbot.addWidget 只存弱引用
    answers: list[str] = []
    scope = GrantScope(Capability.FILE_READ, allowed_paths=(ROOT,))
    panel = ask_permission(host, _list_request(), "该能力尚无会话授权", scope, answers.append)
    buttons = _buttons(panel)
    assert list(buttons) == ["拒绝", "本会话允许", "允许本次"]
    assert buttons["允许本次"].property("accent") is True
    buttons["本会话允许"].click()
    assert answers == [SESSION]


def test_ask_permission_without_scope_has_two_choices(qtbot: QtBot) -> None:
    host = _host(qtbot)
    answers: list[str] = []
    panel = ask_permission(host, _list_request(), "高影响", None, answers.append)
    buttons = _buttons(panel)
    assert list(buttons) == ["拒绝", "允许本次"]
    buttons["允许本次"].click()
    assert answers == [ONCE]


def test_ask_permission_dismiss_means_deny(qtbot: QtBot) -> None:
    host = _host(qtbot)
    answers: list[str] = []
    panel = ask_permission(host, _list_request(), "x", None, answers.append)
    closes = [b for b in panel.findChildren(QPushButton) if b.text() == "✕"]
    closes[0].click()
    assert answers == [DENY]
