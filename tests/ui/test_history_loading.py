from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from qasync import QEventLoop

from limbowave import app
from limbowave.application.services.history_service import HistoryService
from limbowave.bootstrap import AppPaths
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.infrastructure import shell
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui import markdown_render
from limbowave.ui.chat_view import ChatView, HistoryEntry
from limbowave.ui.main_window import MainWindow


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            QApplication.processEvents()
            await asyncio.sleep(0.005)


def test_loading_animation_preserves_draft_and_availability(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.add_user_message("原会话")
    view._input.setPlainText("未发送的草稿")
    view.set_history_loading(True, "正在加载分支…")
    overlay = view._history_loading_overlay
    qtbot.waitUntil(overlay.isVisible)
    assert overlay.accessibleName() == "正在加载分支…"
    assert not view._input.isEnabled()
    assert not view._send_btn.isEnabled()
    assert not view._busy
    angle = overlay._angle
    qtbot.waitUntil(lambda: overlay._angle != angle)
    view._on_send()
    assert view._input.toPlainText() == "未发送的草稿"
    assert len(view._rows) == 1
    view.resize(1000, 760)
    qtbot.waitUntil(lambda: overlay.geometry() == view._scroll.viewport().rect())
    view.set_available(False)
    view.set_history_loading(False)
    qtbot.waitUntil(overlay.isHidden)
    assert not overlay._timer.isActive()
    assert overlay.isHidden()
    assert not view._input.isEnabled()
    view.set_available(True)
    assert view._send_btn.isEnabled()


def test_loading_waits_for_composer_to_finish_docking(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitUntil(lambda: view.composer_lift > 0)
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    assert view.docked
    assert not view.advanced_expanded
    assert not view._input.isEnabled()
    animation = view._dock_anim
    assert animation is not None
    animation.pause()
    for progress in (0.0, 0.5, 0.95):
        animation.setCurrentTime(round(animation.duration() * progress))
        QApplication.processEvents()
        assert overlay.isHidden()
        assert not overlay._timer.isActive()
    animation.setCurrentTime(animation.duration())
    qtbot.waitUntil(overlay.isVisible)
    assert view._dock_anim is None
    assert view.composer_lift == 0
    assert overlay.geometry() == view._scroll.viewport().rect()
    assert overlay._opacity < 1.0
    qtbot.waitUntil(lambda: overlay._opacity == 1.0)


@pytest.mark.parametrize("has_messages", [False, True])
def test_loading_finished_before_docking_never_shows_overlay(
    qtbot: QtBot, has_messages: bool
) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    if has_messages:
        view.load_history([HistoryEntry("user", "已加载", "", "u1")])
    view.set_history_loading(False)
    qtbot.waitUntil(lambda: view._dock_anim is None)
    assert overlay.isHidden()
    assert not overlay._timer.isActive()
    assert view.docked == has_messages
    assert view._input.isEnabled()


def test_docked_loading_fades_in_and_out_without_restarting(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history([HistoryEntry("user", "原会话", "", "u1")])
    view.resize(900, 700)
    view.show()
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    assert overlay.isVisible()
    assert overlay._opacity == 0.0
    animation = overlay._fade_anim
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    opacity = overlay._opacity
    assert 0.0 < opacity < 1.0
    view.set_history_loading(True, "正在加载分支…")
    assert overlay._opacity == opacity
    assert animation.currentTime() == animation.duration() // 2
    assert overlay.accessibleName() == "正在加载分支…"
    animation.setCurrentTime(animation.duration())
    view.set_history_loading(False)
    assert view._input.isEnabled()
    assert overlay.isVisible()
    assert overlay.accessibleName() == "正在加载分支…"
    assert overlay._opacity == 1.0
    assert overlay._timer.isActive()
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    assert 0.0 < overlay._opacity < 1.0
    animation.setCurrentTime(animation.duration())
    assert overlay.isHidden()
    assert not overlay._timer.isActive()


def test_loading_can_reverse_an_unfinished_fade(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history([HistoryEntry("user", "原会话", "", "u1")])
    view.show()
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    animation = overlay._fade_anim
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    opacity = overlay._opacity
    view.set_history_loading(False)
    assert overlay._opacity == opacity
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    opacity = overlay._opacity
    view.set_history_loading(True)
    assert overlay._opacity == opacity
    qtbot.waitUntil(lambda: overlay._opacity == 1.0)
    assert overlay.isVisible()
    assert overlay._timer.isActive()
    view.set_history_loading(False)
    qtbot.waitUntil(overlay.isHidden)


def test_empty_history_stays_docked_until_loading_finishes(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    qtbot.waitUntil(lambda: overlay._opacity == 1.0)
    view.load_history([])
    assert view.docked
    assert view._dock_anim is None
    assert overlay.isVisible()
    view.set_history_loading(False)
    qtbot.waitUntil(lambda: overlay.isHidden() and view._dock_anim is None)
    assert not view.docked
    assert view.composer_lift > 0
    assert not overlay._timer.isActive()


def test_disabling_overlay_cancels_pending_show(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.set_history_loading(True)
    view.set_history_loading(True, show_overlay=False)
    qtbot.waitUntil(lambda: view._dock_anim is None)
    assert view.history_loading
    assert not view._input.isEnabled()
    assert view._history_loading_overlay.isHidden()
    assert not view._history_loading_overlay._timer.isActive()
    assert not view.docked


async def test_incremental_render_yields_and_uses_prepared_markdown(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    entries = [HistoryEntry("assistant", f"reply {i}", "", f"m{i}") for i in range(20)]
    rendered = {entry.content: f"<p>{entry.content}</p>" for entry in entries}
    monkeypatch.setattr(
        markdown_render, "render", lambda _text: pytest.fail("GUI rendered Markdown")
    )
    task = asyncio.create_task(view.load_history_incrementally(entries, rendered=rendered))
    await asyncio.sleep(0)
    assert 0 < len(view._rows) < len(entries)
    assert await task
    assert len(view._rows) == 20
    # 清空/外部重绘接管后，迟到的分批渲染不得重新填回旧历史。
    task = asyncio.create_task(view.load_history_incrementally(entries, rendered=rendered))
    await asyncio.sleep(0)
    view.clear_transcript()
    assert not await task
    assert not view._rows


def _wire(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    now = datetime.now(UTC)
    with factory() as uow:
        uow.conversations.add(Conversation("c1", "历史会话", now))
        uow.branches.add(Branch("b1", "c1", now))
        uow.messages.add(Message("u1", "c1", "b1", MessageRole.USER, "问题", now))
        uow.messages.add(Message(
            "a1", "c1", "b1", MessageRole.ASSISTANT, "**回答**", now + timedelta(seconds=1)
        ))
        uow.commit()
    monkeypatch.setattr(app, "in_memory_uow_factory", lambda: factory)
    monkeypatch.setattr(app, "_build_kernel", lambda *_a, **_kw: None)
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    window = MainWindow()
    qtbot.addWidget(window)
    controller, _ipc, _warm, _startup, shutdown = app._wire(
        window, AppPaths(tmp_path, tmp_path / "logs"), None
    )
    return window, controller, shutdown


@pytest.mark.parametrize("branch", [False, True])
async def test_app_loading_reads_and_markdown_are_off_thread_and_duplicates_ignored(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, branch: bool
) -> None:
    window, controller, shutdown = _wire(qtbot, monkeypatch, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    worker_ids: list[int] = []
    renders: list[int] = []
    original = HistoryService.branch_messages if branch else HistoryService.open_conversation
    render = markdown_render.render
    snapshot_reads: list[int] = []
    with controller.coordinator()._uow_factory() as uow:
        snapshot_type = type(uow.snapshots)
    snapshots = snapshot_type.attachment_ids_for_messages

    def read_snapshots(self, *args):
        snapshot_reads.append(threading.get_ident())
        return snapshots(self, *args)

    monkeypatch.setattr(snapshot_type, "attachment_ids_for_messages", read_snapshots)

    def slow_read(self, target):
        worker_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(self, target)

    def render_recorded(text):
        renders.append(threading.get_ident())
        return render(text)

    monkeypatch.setattr(
        HistoryService, "branch_messages" if branch else "open_conversation", slow_read
    )
    monkeypatch.setattr(markdown_render, "render", render_recorded)
    window.chat.set_available(True)
    window.chat._input.setPlainText("草稿")

    def select():
        if branch:
            window.sidebar.branch_switch_requested.emit("c1", "b1")
        else:
            window.sidebar.conversation_selected.emit("c1")

    try:
        select()
        await _until(entered.is_set)
        assert window.chat.history_loading
        assert window.chat._input.toPlainText() == "草稿"
        assert not window.chat._send_btn.isEnabled()
        assert not window.sidebar._list.isEnabled()
        assert window.sidebar._settings_btn.isEnabled()
        select()
        window.sidebar.new_conversation_requested.emit()
        await asyncio.sleep(0.01)
        assert len(worker_ids) == 1
        assert controller.conversation_id is None
        release.set()
        await _until(lambda: not window.chat.history_loading)
        assert worker_ids == [renders[0]]
        assert snapshot_reads == worker_ids  # render-time resolver must hit the prepared cache
        assert set(renders) == set(worker_ids)
        assert worker_ids[0] != threading.get_ident()
        assert controller.conversation_id == "c1"
        assert controller.branch_id == "b1"
        assert [row.content_text() for row in window.chat._rows] == ["问题", "回答"]
        # Successful navigation selects the target draft, not the previous composer.
        assert window.chat._input.toPlainText() == ""
        assert not window.chat._send_btn.isEnabled()
        assert window.sidebar._list.isEnabled()
    finally:
        release.set()
        await shutdown()


@pytest.mark.parametrize("failure", ["read", "missing", "restore"])
async def test_app_loading_failure_restores_ui_and_old_history(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    window, controller, shutdown = _wire(qtbot, monkeypatch, tmp_path)
    window.chat.add_user_message("原会话")

    def read_error(*_args):
        raise RuntimeError("读取失败")

    async def restore_error(*_args, **_kwargs):
        return False

    if failure == "read":
        monkeypatch.setattr(HistoryService, "open_conversation", read_error)
    elif failure == "restore":
        monkeypatch.setattr(controller, "switch_conversation", restore_error)
    try:
        window.sidebar.conversation_selected.emit("missing" if failure == "missing" else "c1")
        await asyncio.sleep(0)
        await _until(lambda: not window.chat.history_loading)
        await asyncio.sleep(0)  # consume fire-and-forget exception callback
        assert window.sidebar._list.isEnabled()
        assert controller.conversation_id is None
        assert window.chat._rows[0].content_text() == "原会话"
        assert len(window.chat._rows) == 2  # visible error, not only hidden status text
    finally:
        await shutdown()


async def test_branch_expansion_is_background_and_shutdown_drains_reader(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    window, _controller, shutdown = _wire(qtbot, monkeypatch, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    worker_ids = []
    original = HistoryService.list_branches

    def slow_read(self, target):
        worker_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        result = original(self, target)
        finished.set()
        return result

    monkeypatch.setattr(HistoryService, "list_branches", slow_read)
    closing = None
    try:
        window.sidebar.toggle_branches("c1")
        await _until(entered.is_set)
        assert window.sidebar.branches_loading("c1")
        assert not window.sidebar._branch_loading.isHidden()
        assert worker_ids[0] != threading.get_ident()
        closing = asyncio.create_task(shutdown())
        await asyncio.sleep(0.02)
        assert not closing.done()
        release.set()
        await closing
        assert finished.is_set()
        assert not window.sidebar.branches_loading("c1")
    finally:
        release.set()
        if closing is None:
            await shutdown()
        else:
            await closing


def test_qasync_keeps_loading_animation_alive_during_slow_read(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    release = threading.Event()
    timer = QTimer()
    ticks = []
    shutdown = None
    try:
        window, _controller, shutdown = _wire(qtbot, monkeypatch, tmp_path)
        window.show()
        original = HistoryService.open_conversation

        def slow_read(self, target):
            assert release.wait(5)
            return original(self, target)

        monkeypatch.setattr(HistoryService, "open_conversation", slow_read)
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(window.chat.history_loading))
        timer.start()

        async def scenario():
            window.sidebar.conversation_selected.emit("c1")
            await asyncio.sleep(0.15)
            assert window.chat.history_loading
            overlay = window.chat._history_loading_overlay
            assert overlay.isHidden()
            async with asyncio.timeout(5):
                while not overlay.isVisible() or overlay._angle == 0:
                    await asyncio.sleep(0.01)
            assert window.chat.composer_lift == 0
            assert window.chat._dock_anim is None
            assert sum(ticks) >= 3
            release.set()
            async with asyncio.timeout(5):
                while window.chat.history_loading:
                    await asyncio.sleep(0.01)
                while not overlay.isHidden():
                    await asyncio.sleep(0.01)
            assert overlay.isHidden()
            assert not overlay._timer.isActive()

        loop.run_until_complete(scenario())
    finally:
        release.set()
        timer.stop()
        if shutdown is not None:
            loop.run_until_complete(shutdown())
        loop.close()
        asyncio.set_event_loop(None)


def test_send_preparation_locks_without_loading_animation(qtbot):
    from limbowave.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_available(True)
    view.set_history_loading(True, show_overlay=False)
    assert view.history_loading
    assert not view._history_loading_overlay.isVisible()
    assert not view._history_loading_overlay._timer.isActive()
    assert not view.docked
    assert view._dock_anim is None
    assert not view._input.isEnabled()
    view.set_history_loading(False)
    assert view._input.isEnabled()
    view.set_history_loading(True)
    qtbot.waitUntil(view._history_loading_overlay.isVisible)
    view.set_history_loading(False)
