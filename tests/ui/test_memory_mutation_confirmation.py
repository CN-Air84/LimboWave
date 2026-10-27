"""Restricted presets show real consent; full access auto-approves without a popup."""

import asyncio
import json
from contextlib import suppress

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QPushButton, QWidget

from limbowave.app import _make_permission_handler
from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import ToolGateway
from limbowave.domain.memory import MemoryPolicy, MemoryRunContext
from limbowave.domain.permissions import PermissionPreset
from limbowave.ui.floating import FloatingPanel
from tests.unit.test_memory_service import memory_stack as memory_stack


@pytest.mark.parametrize("tool,arguments,scope", [
    ("update_memory", {"updated_content": "updated content"}, "session"),
    ("delete_session_memory", {"archive": True}, "session"),
    ("delete_session_memory", {"archive": False}, "session"),
    ("update_memory", {"updated_content": "updated global content"}, "global"),
])
@pytest.mark.parametrize("accept", [True, False])
async def test_restricted_access_still_shows_real_memory_confirmation(
    qtbot, memory_stack, tmp_path, tool, arguments, scope, accept,
):
    service, factory = memory_stack
    service.set_policy("c", MemoryPolicy.ALLOW)
    conversation_id, branch_id = (None, None) if scope == "global" else ("c", "b")
    original = service.save("original content", conversation_id, branch_id)
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.READ_ONLY)
    context = MemoryRunContext("r", "c", "b", "msg")
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    host = QWidget()
    host.resize(900, 800)
    qtbot.addWidget(host)
    host.show()
    handler = _make_permission_handler(host, permissions, lambda: "c", gateway,
                                       memory_service=service, memory_context=lambda: context)
    params = {"memory_id": original.id, **arguments}
    task = asyncio.create_task(handler(f"limbowave.gate:{tool}", json.dumps({
        "tool": tool, "input": params, "run_id": "r", "call_id": "call",
    })))
    try:
        for _ in range(300):
            QApplication.processEvents()
            popup = host.findChild(FloatingPanel)
            if popup is not None and popup.isVisible():
                break
            assert not task.done(), "The permission handler returned without showing a popup"
            await asyncio.sleep(0.01)
        else:
            pytest.fail("No memory approval popup appeared")
        editor = popup.findChild(QPlainTextEdit)
        assert editor.isReadOnly()
        assert original.content in editor.toPlainText()
        if tool == "update_memory":
            assert arguments["updated_content"] in editor.toPlainText()
        if scope == "global":
            assert "全局记忆" in editor.toPlainText()
            assert "所有继承" in editor.toPlainText() and "自定义覆盖" in editor.toPlainText()
        assert not task.done() and service.list(conversation_id, branch_id) == [original]
        button = next(b for b in popup.findChildren(QPushButton)
                      if b.text() == ("允许本次" if accept else "拒绝"))
        button.click()
        assert await asyncio.wait_for(task, timeout=3) is accept
        result = gateway.invoke(tool, {**params, "run_id": "r", "call_id": "call"},
                                "c", user_confirmed=True)
        assert result.ok is accept
        if not accept:
            assert service.list(conversation_id, branch_id) == [original]
        qtbot.wait(250)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("tool,arguments,scope", [
    ("update_memory", {"updated_content": "updated content"}, "session"),
    ("delete_session_memory", {"archive": True}, "session"),
    ("delete_session_memory", {"archive": False}, "session"),
    ("update_memory", {"updated_content": "updated global content"}, "global"),
    ("add_session_memory", {"content": "new memory"}, "session"),
])
async def test_full_access_memory_never_prompts(
    qtbot, memory_stack, tmp_path, tool, arguments, scope,
):
    service, factory = memory_stack
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    context = MemoryRunContext("r", "c", "b", "msg")
    gateway = ToolGateway(permissions, tmp_path)
    gateway.memory_service = service
    gateway.memory_context = lambda: context
    host = QWidget()
    qtbot.addWidget(host)
    handler = _make_permission_handler(host, permissions, lambda: "c", gateway,
                                       memory_service=service, memory_context=lambda: context)
    params = dict(arguments)
    if tool != "add_session_memory":
        conversation_id, branch_id = (None, None) if scope == "global" else ("c", "b")
        original = service.save("original content", conversation_id, branch_id)
        params["memory_id"] = original.id
    allowed = await asyncio.wait_for(handler(f"limbowave.gate:{tool}", json.dumps({
        "tool": tool, "input": params, "run_id": "r", "call_id": "call",
    })), timeout=3)
    assert allowed
    assert host.findChild(FloatingPanel) is None
    result = gateway.invoke(tool, {**params, "run_id": "r", "call_id": "call"}, "c")
    assert result.ok
    assert all(not audit.user_confirmed for audit in permissions.list_audit("c"))
    assert permissions.list_audit("c")[-1].matched_rule == "preset.full_access"
