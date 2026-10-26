"""The footprint entry is wired to full-page navigation, not another settings tab."""

import asyncio

import pytest
from PySide6.QtWidgets import QPushButton

import limbowave.app as app
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.memory_service import MemoryService
from limbowave.bootstrap import AppPaths
from limbowave.domain.conversation import Branch, Conversation, Message, MessageRole
from limbowave.domain.memory import MemoryRunContext
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.infrastructure import shell
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.ui.main_window import MainWindow
from limbowave.ui.memory_timeline import MemoryTimelinePage
from limbowave.ui.settings_panel import SettingsPage
from tests.ui.test_footprint_workspace_layout import assert_workspace_fits
from tests.ui.test_new_session_memory import wait_for
from tests.unit.test_memory_service import NOW


@pytest.mark.asyncio
async def test_app_footprint_entry_preserves_draft_and_opens_memory_settings(
    qtbot,
    qapp,
    monkeypatch,
    tmp_path,
    vault_key,
):
    monkeypatch.setattr(app, "_build_kernel", lambda *_a, **_kw: None)
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    factory = sqlite_uow_factory(tmp_path / "limbowave.db", vault_key)
    service = MemoryService(
        factory, ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))
    )
    service.save("这是一段测试足迹")
    with factory() as uow:
        uow.conversations.add(Conversation("source-chat", "来源对话", NOW))
        uow.branches.add(Branch("source-branch", "source-chat", NOW))
        uow.messages.add(
            Message(
                "source-message",
                "source-chat",
                "source-branch",
                MessageRole.USER,
                "最初写下的文字",
                NOW,
            )
        )
        uow.runs.add(
            RunRecord(
                "source-run",
                "source-chat",
                "source-branch",
                RunStatus.COMPLETED,
                NOW,
                "source-message",
            )
        )
        uow.commit()
    context = MemoryRunContext("source-run", "source-chat", "source-branch", "source-message")
    service.approve(context, "call", "在这场对话中被记住")
    remembered = service.add_from_model(context, "call", "在这场对话中被记住", lambda _: True)
    original_stylesheet = qapp.styleSheet()
    window = MainWindow()
    qtbot.addWidget(window)

    class LocalIpc:
        session = rate_limit_session = None

        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    def finish_bootstrap(coro):
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("Unexpected asynchronous IPC bootstrap work")

    monkeypatch.setattr(app, "ToolIpcServer", LocalIpc)
    with monkeypatch.context() as bootstrap:
        bootstrap.setattr(asyncio.get_running_loop(), "run_until_complete", finish_bootstrap)
        _, _, _, _, shutdown = app._wire(window, AppPaths(tmp_path, tmp_path / "logs"), vault_key)
    window.resize(1000, 780)
    window.show()
    try:
        original_size = window.size()
        window.chat._input.setPlainText("尚未发送的文字")
        window.sidebar._footprints_btn.click()
        await wait_for(qapp, lambda: bool(window.findChildren(MemoryTimelinePage)))
        page = window.findChildren(MemoryTimelinePage)[-1]
        await wait_for(qapp, lambda: not page._loading)
        assert window.showing_full_page and page.isVisible()
        assert window.size() == original_size
        assert any(card.row.memory.content == "这是一段测试足迹" for card in page.cards)
        page.findChild(QPushButton, "footprintReturn").click()
        assert not window.showing_full_page and page._disposed
        assert window.chat._input.toPlainText() == "尚未发送的文字"
        await wait_for(qapp, lambda: not window.transition_active)
        assert window.size() == original_size
        assert_workspace_fits(window)
        window.sidebar._footprints_btn.click()
        await wait_for(qapp, lambda: window.showing_full_page)
        page = window.findChildren(MemoryTimelinePage)[-1]
        manage = next(
            button for button in page.findChildren(QPushButton) if button.text() == "管理记忆"
        )
        manage.click()
        await wait_for(qapp, lambda: bool(window.findChildren(SettingsPage)))
        settings = window.findChildren(SettingsPage)[-1]
        assert window.showing_full_page
        assert settings.memory_tab.isVisible()
        settings.close_requested.emit()
        assert not window.showing_full_page
        await wait_for(qapp, lambda: not window.transition_active)
        assert window.size() == original_size
        assert_workspace_fits(window)
        window.sidebar._footprints_btn.click()
        page = window.findChildren(MemoryTimelinePage)[-1]
        await wait_for(qapp, lambda: not page._loading)
        source_card = next(card for card in page.cards if card.row.memory.id == remembered.id)
        assert source_card.row.source_message_id == "source-message"
        source_card.findChild(QPushButton, "footprintOpenSource").click()
        await wait_for(
            qapp,
            lambda: (
                not window.chat.history_loading
                and any(
                    entry.message_id == "source-message"
                    for entry in (window.history_preview or window.chat)._full_history
                )
            ),
        )
        surface = window.history_preview or window.chat
        assert not window.showing_full_page
        assert not surface._search_ids and not surface._search_bar.isVisible()
    finally:
        await shutdown()
        factory.close()
        qapp.setStyleSheet(original_stylesheet)


@pytest.mark.asyncio
async def test_app_without_vault_does_not_claim_encrypted_footprints(
    qtbot,
    qapp,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(app, "_build_kernel", lambda *_a, **_kw: None)
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    original_stylesheet = qapp.styleSheet()
    window = MainWindow()
    qtbot.addWidget(window)
    _, _, _, _, shutdown = app._wire(window, AppPaths(tmp_path, tmp_path / "logs"), None)
    try:
        window.sidebar._footprints_btn.click()
        assert not window.findChildren(MemoryTimelinePage)
        assert not window.showing_full_page
    finally:
        await shutdown()
        qapp.setStyleSheet(original_stylesheet)
