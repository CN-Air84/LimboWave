"""AI rename suggestions are cancellable drafts, not automatic history mutations."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QWidget

from limbowave.application.services.conversation_title_service import ConversationTitleService
from limbowave.application.services.history_service import HistoryService
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui.floating import ask_prompt
from tests.ui.test_model_selector_routing import _callback
from tests.unit.test_conversation_title_service import _OwnerKernel, _TitleKernel


def _host(qtbot):
    host = QWidget()
    host.resize(800, 600)
    qtbot.addWidget(host)
    host.show()
    return host


def _button(panel, label):
    return next(b for b in panel.findChildren(QPushButton) if b.text() == label)


async def test_ai_suggestion_fills_draft_only_and_blocks_duplicate_requests(qtbot):
    answer = asyncio.get_running_loop().create_future()
    calls = []
    saved = []

    async def suggest():
        calls.append(True)
        return await answer

    host = _host(qtbot)
    panel = ask_prompt(
        host, "重命名会话", "新标题：", saved.append,
        default_text="原名称", on_suggest=suggest,
    )
    ai = _button(panel, "AI 重命名")
    edit = panel.findChild(QLineEdit)
    ai.click()
    ai.click()
    await asyncio.sleep(0)
    assert calls == [True]
    assert not ai.isEnabled()
    assert edit.isReadOnly()
    assert not _button(panel, "确定").isEnabled()
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert saved == []
    answer.set_result("AI 候选名称")
    await asyncio.sleep(0)
    assert edit.text() == "AI 候选名称"
    assert not edit.isReadOnly()
    assert ai.isEnabled()
    assert saved == []
    edit.setText("修改后采用")
    _button(panel, "确定").click()
    assert saved == ["修改后采用"]


@pytest.mark.parametrize("result", [None, RuntimeError("private provider details")])
async def test_ai_failure_preserves_input_and_allows_retry(qtbot, result):
    calls = []

    async def suggest():
        calls.append(True)
        if len(calls) == 1:
            if isinstance(result, Exception):
                raise result
            return result
        return "重试成功"

    host = _host(qtbot)
    panel = ask_prompt(
        host, "重命名会话", "新标题：", lambda _s: None,
        default_text="保留名称", on_suggest=suggest,
    )
    ai = _button(panel, "AI 重命名")
    ai.click()
    await asyncio.sleep(0)
    assert panel.findChild(QLineEdit).text() == "保留名称"
    assert ai.isEnabled()
    assert any("失败" in label.text() for label in panel.findChildren(QLabel))
    assert all("private" not in label.text() for label in panel.findChildren(QLabel))
    ai.click()
    await asyncio.sleep(0)
    assert panel.findChild(QLineEdit).text() == "重试成功"
    panel.close_panel()


async def test_closing_prompt_cancels_suggestion_without_saving(qtbot):
    cancelled = []
    saved = []

    async def suggest():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append(True)
            # Even a late result from a cancellation-resistant provider is ignored.
            return "过期名称"

    host = _host(qtbot)
    panel = ask_prompt(
        host, "重命名会话", "新标题：", saved.append,
        default_text="原名称", on_suggest=suggest,
    )
    _button(panel, "AI 重命名").click()
    await asyncio.sleep(0)
    panel.close_panel()
    await asyncio.sleep(0)
    assert cancelled == [True]
    assert saved == []
    assert panel.findChild(QLineEdit).text() == "原名称"


def test_other_prompts_have_no_ai_action(qtbot):
    host = _host(qtbot)
    panel = ask_prompt(host, "密码", "密码：", lambda _s: None, password=True)
    assert not any("AI" in b.text() for b in panel.findChildren(QPushButton))
    panel.close_panel()


@pytest.fixture
def rename_runtime(qtbot):
    factory = in_memory_uow_factory(InMemoryStore())
    now = datetime.now(UTC)
    with factory() as uow:
        for conversation_id in ("target", "other", "empty"):
            uow.conversations.add(Conversation(conversation_id, "原名称", now))
        for branch_id, conversation_id, parent, fork in (
            ("root", "target", None, None),
            ("chosen", "target", "root", "a1"),
            ("latest", "target", "root", "a1"),
            ("other-branch", "other", None, None),
        ):
            uow.branches.add(Branch(
                branch_id, conversation_id, now, parent_branch_id=parent,
                forked_from_message_id=fork, include_fork_message=True,
            ))
        for index, (mid, branch, role, text) in enumerate([
            ("u1", "root", "user", "最初讨论部署"),
            ("a1", "root", "assistant", "首轮部署答复"),
            ("u2", "root", "user", "已经离开的分支问题"),
            ("a2", "root", "assistant", "已经离开的分支答复"),
            ("u3", "chosen", "user", "现在讨论数据库备份"),
            ("a3", "chosen", "assistant", "最终选择异地备份"),
            ("u4", "latest", "user", "另一分支的后续问题"),
            ("a4", "latest", "assistant", "另一分支的后续答复"),
            ("u5", "other-branch", "user", "其他会话的私有消息"),
        ]):
            uow.messages.add(Message(
                mid, "other" if branch == "other-branch" else "target", branch,
                MessageRole(role), text, now + timedelta(seconds=index),
            ))
        uow.commit()
    history = HistoryService(factory)
    isolated = _TitleKernel("异地备份方案")
    owner = _OwnerKernel(isolated)
    controller = SimpleNamespace(
        conversation_id="target", branch_id="chosen",
        coordinator=lambda: SimpleNamespace(kernel=owner),
    )
    panels = []
    tasks = []

    def prompt(*args, **kwargs):
        panel = ask_prompt(*args, **kwargs)
        panels.append(panel)
        return panel

    async def read(work, *args):
        return work(*args)

    def spawn(coro):
        tasks.append(asyncio.create_task(coro))

    namespace = {
        "asyncio": asyncio,
        "shutting_down": False,
        "_pending_tasks": set(),
        "history": history,
        "history_reader": SimpleNamespace(read=read),
        "controller": controller,
        "_display_scope": lambda: (controller.conversation_id, controller.branch_id),
        "_active_title_route": lambda: ("selected-site", "selected-model"),
        "conversation_titles": ConversationTitleService(),
        "window": _host(qtbot),
        "ask_prompt": prompt,
        "_spawn": spawn,
        "run_blocking": read,
        "_refresh_conversations": Mock(),
    }
    return SimpleNamespace(
        namespace=namespace, callback=_callback("_rename_conversation", namespace),
        controller=controller, isolated=isolated, owner=owner, history=history,
        panels=panels, tasks=tasks,
    )


async def _finish_ai(panel):
    for _ in range(100):
        await asyncio.sleep(0)
        if any(b.text() == "AI 重命名" and b.isEnabled()
               for b in panel.findChildren(QPushButton)):
            return
    raise AssertionError("AI action did not finish")


@pytest.mark.parametrize("scope", ["active", "preview", "background", "active-with-preview"])
async def test_rename_uses_target_full_branch_not_live_or_first_turn(rename_runtime, scope):
    rt = rename_runtime
    if scope in ("preview", "background"):
        rt.controller.conversation_id, rt.controller.branch_id = "other", "other-branch"
    if scope == "preview":
        rt.namespace["_display_scope"] = lambda: ("target", "chosen")
    elif scope == "active-with-preview":
        rt.namespace["_display_scope"] = lambda: ("other", "other-branch")
    await rt.callback("target")
    panel = rt.panels[-1]
    assert panel.findChild(QLineEdit).text() == "原名称"
    _button(panel, "AI 重命名").click()
    await _finish_ai(panel)
    expected = ["最初讨论部署", "首轮部署答复"] + (
        ["另一分支的后续问题", "另一分支的后续答复"] if scope == "background"
        else ["现在讨论数据库备份", "最终选择异地备份"]
    )
    positions = [rt.isolated.sent.index(text) for text in expected]
    assert positions == sorted(positions)
    assert "已经离开的分支" not in rt.isolated.sent
    assert "其他会话的私有消息" not in rt.isolated.sent
    assert rt.owner.sent == ""
    assert rt.isolated.model == ("selected-site", "selected-model")
    assert rt.isolated.stopped
    assert rt.history.title("target") == "原名称", "AI generation must not save implicitly"
    assert panel.findChild(QLineEdit).text() == "异地备份方案"
    _button(panel, "确定").click()
    await asyncio.gather(*rt.tasks)
    assert rt.history.title("target") == "异地备份方案"
    assert rt.history.title("other") == "原名称"
    rt.namespace["_refresh_conversations"].assert_called_once()


@pytest.mark.parametrize("missing", ["route", "kernel", "messages", "conversation"])
async def test_rename_missing_inputs_preserve_title_without_model_request(rename_runtime, missing):
    rt = rename_runtime
    if missing == "route":
        rt.namespace["_active_title_route"] = lambda: None
    elif missing == "kernel":
        rt.controller.coordinator = lambda: SimpleNamespace(kernel=None)
    target = {"messages": "empty", "conversation": "deleted"}.get(missing, "target")
    await rt.callback(target)
    if missing == "conversation":
        assert rt.panels == []
        return
    panel = rt.panels[-1]
    _button(panel, "AI 重命名").click()
    await _finish_ai(panel)
    assert not rt.isolated.started
    assert panel.findChild(QLineEdit).text() == "原名称"
    assert any("请先" in label.text() for label in panel.findChildren(QLabel))
    assert rt.history.title(target) == "原名称"
    panel.close_panel()


async def test_destroying_prompt_host_cancels_pending_suggestion(qtbot):
    import shiboken6

    cancelled = []

    async def suggest():
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    host = _host(qtbot)
    panel = ask_prompt(host, "重命名会话", "新标题：", lambda _s: None, on_suggest=suggest)
    _button(panel, "AI 重命名").click()
    await asyncio.sleep(0)
    shiboken6.delete(host)
    await asyncio.sleep(0)
    assert cancelled == [True]


async def test_app_shutdown_can_cancel_and_drain_ai_rename(rename_runtime):
    rt = rename_runtime
    started = []
    stopped = []

    async def waiting_suggestion(*args, **kwargs):
        started.append(True)
        try:
            await asyncio.Future()
        finally:
            stopped.append(True)

    rt.namespace["conversation_titles"] = SimpleNamespace(suggest_for_messages=waiting_suggestion)
    await rt.callback("target")
    panel = rt.panels[-1]
    _button(panel, "AI 重命名").click()
    await asyncio.sleep(0)
    assert started == [True]
    pending = list(rt.namespace["_pending_tasks"])
    assert len(pending) == 1
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    assert stopped == [True]
    assert not rt.namespace["_pending_tasks"]
    assert rt.history.title("target") == "原名称"
    panel.close_panel()
