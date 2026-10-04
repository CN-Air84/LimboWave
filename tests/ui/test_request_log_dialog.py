"""请求日志查看器的 Qt 验收（Task 3.3）。

锁住的交互语义：
- 运行列表每行含状态与传输次数；
- 选中一轮后三个页签分别渲染：应用意图 / 实际传输 / 参数差异；
- 参数差异页把「仅实际传输（运行时注入）」的键明确标出（§四.4 可观测化）；
- 中断的 run 显示「无意图快照」，不伪造；
- 脱敏头显示「已脱敏」，永不出现密钥本体。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pytestqt.qtbot import QtBot

from limbowave.application.services.request_log_service import RequestLogService
from limbowave.domain.conversation import Branch, Conversation
from limbowave.domain.run import RunRecord, RunStatus
from limbowave.domain.snapshots import RequestIntentSnapshot, TransportSnapshot
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui.request_log_dialog import RequestLogDialog

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
SECRET = "sk-live-MUST-NEVER-SHOW-IN-LOG-UI"


@pytest.fixture
def service() -> RequestLogService:
    store = InMemoryStore()
    with in_memory_uow_factory(store)() as uow:
        uow.conversations.add(Conversation(id="c1", title="t", created_at=T0))
        uow.branches.add(Branch(id="b1", conversation_id="c1", created_at=T0))
        uow.runs.add(
            RunRecord(
                id="r1",
                conversation_id="c1",
                branch_id="b1",
                status=RunStatus.COMPLETED,
                created_at=T0,
                user_message_id="m1",
            )
        )
        uow.snapshots.add_intent(
            RequestIntentSnapshot(
                id="i1",
                run_id="r1",
                conversation_id="c1",
                branch_id="b1",
                logical_model_id="deepseek-chat",
                endpoint_id="relay-a",
                routing_reason="默认绑定",
                created_at=T0,
                app_params={"model": "deepseek-chat", "max_tokens": 4096},
            )
        )
        uow.snapshots.add_transport(
            TransportSnapshot(
                id="t1",
                run_id="r1",
                created_at=T0,
                sequence=1,
                url="https://relay-a.example.com/v1/chat/completions",
                headers={
                    "Authorization": {"present": True, "scheme": "Bearer", "value": "[REDACTED]"},
                    "Content-Type": "application/json",
                },
                # Pi 注入了 stream / stream_options——意图里没有，差异页必须标出
                body={"model": "deepseek-chat", "max_tokens": 4096, "stream": True},
                response_status=200,
            )
        )
        uow.runs.add(
            RunRecord(
                id="r2",
                conversation_id="c1",
                branch_id="b1",
                status=RunStatus.INTERRUPTED,
                created_at=T0,
                user_message_id="m2",
                error="Runtime 异常退出",
            )
        )
        uow.commit()
    result = RequestLogService(in_memory_uow_factory(store))
    yield result
    result.close()


def _tree_text(tree) -> str:
    parts: list[str] = []

    def walk(item) -> None:
        parts.append(item.text(0))
        parts.append(item.text(1))
        for i in range(item.childCount()):
            walk(item.child(i))

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    return "\n".join(parts)


def test_run_list_shows_status_and_transport_count(qtbot: QtBot, service) -> None:
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    assert dialog._runs.count() == 2
    first = dialog._runs.item(0).text()
    assert "完成" in first
    assert "1 次传输" in first
    assert "已中断" in dialog._runs.item(1).text()


def test_completed_run_renders_three_tabs(qtbot: QtBot, service) -> None:
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    dialog._runs.setCurrentRow(0)
    qtbot.waitUntil(lambda: 0 in dialog._rendered_tabs)

    intent_text = _tree_text(dialog._intent_tree)
    assert "deepseek-chat" in intent_text
    assert "relay-a" in intent_text
    assert "默认绑定" in intent_text

    dialog._tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: 1 in dialog._rendered_tabs)
    transport_text = _tree_text(dialog._transport_tree)
    assert "relay-a.example.com" in transport_text
    assert "已脱敏" in transport_text
    assert SECRET not in transport_text
    assert "HTTP 200" in transport_text

    dialog._tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: 2 in dialog._rendered_tabs)
    diff_text = _tree_text(dialog._diff_tree)
    assert "stream" in diff_text
    assert "仅实际传输" in diff_text  # Pi 注入的键被明确标出
    assert "model" in diff_text


def test_interrupted_run_shows_missing_intent(qtbot: QtBot, service) -> None:
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    dialog._runs.setCurrentRow(1)
    qtbot.waitUntil(lambda: 0 in dialog._rendered_tabs)

    assert "没有意图快照" in dialog._summary.text()
    assert "Runtime 异常退出" in dialog._summary.text()
    assert "无意图快照" in _tree_text(dialog._intent_tree)


def test_secret_never_appears_anywhere(qtbot: QtBot, service) -> None:
    """密钥红线在 UI 层同样成立：三个页签 + 摘要里都没有密钥。"""
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    dialog._runs.setCurrentRow(0)
    qtbot.waitUntil(lambda: 0 in dialog._rendered_tabs)
    for index in (1, 2):
        dialog._tabs.setCurrentIndex(index)
        qtbot.waitUntil(lambda index=index: index in dialog._rendered_tabs)
    blob = (
        dialog._summary.text()
        + _tree_text(dialog._intent_tree)
        + _tree_text(dialog._transport_tree)
        + _tree_text(dialog._diff_tree)
    )
    assert SECRET not in blob
    assert "[REDACTED]" not in blob or "已脱敏" in blob  # 结构化占位可显示，密钥不可


def test_slow_list_load_does_not_block_ui(qtbot, service, monkeypatch):
    import threading

    from PySide6.QtCore import QTimer

    started, release = threading.Event(), threading.Event()
    original = service.list_summaries

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "list_summaries", slow)
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    ticks = []
    heartbeat = QTimer(dialog)
    heartbeat.timeout.connect(lambda: ticks.append(1))
    heartbeat.start(5)
    try:
        qtbot.waitUntil(started.is_set)
        qtbot.waitUntil(lambda: len(ticks) >= 3)
        assert dialog._runs.count() == 0
        assert "加载" in dialog._summary.text()
    finally:
        release.set()
    qtbot.waitUntil(lambda: dialog._runs.count() == 2)


def test_list_error_is_recoverable(qtbot, service, monkeypatch):
    original = service.list_summaries

    def failed(*args, **kwargs):
        raise ValueError("sensitive backend details must not leak")

    monkeypatch.setattr(service, "list_summaries", failed)
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    assert "加载失败" in dialog._summary.text()
    assert "sensitive" not in dialog._summary.text()
    monkeypatch.setattr(service, "list_summaries", original)
    dialog._reload.click()
    qtbot.waitUntil(lambda: dialog._runs.count() == 2)


def test_old_conversation_and_detail_results_cannot_replace_new_selection(
    qtbot, service, monkeypatch,
):
    from concurrent.futures import Future

    pages, details = [], []

    def page(*args, **kwargs):
        result = Future()
        result.set_running_or_notify_cancel()
        pages.append(result)
        return result

    def detail(*args, **kwargs):
        result = Future()
        result.set_running_or_notify_cancel()
        details.append(result)
        return result

    monkeypatch.setattr(service, "load_page_async", page)
    monkeypatch.setattr(service, "load_run_async", detail)
    dialog = RequestLogDialog(service, "old")
    qtbot.addWidget(dialog)
    dialog.set_conversation("c1")
    pages[1].set_result(service.list_summaries("c1"))
    qtbot.waitUntil(lambda: dialog._runs.count() == 2)
    pages[0].set_result(service.list_summaries("missing"))
    dialog._poll_results()
    assert dialog._runs.count() == 2
    dialog._runs.setCurrentRow(0)
    dialog._runs.setCurrentRow(1)
    details[1].set_result(service.get_for_run("r2"))
    qtbot.waitUntil(lambda: 0 in dialog._rendered_tabs)
    assert "r2" in dialog._summary.text()
    details[0].set_result(service.get_for_run("r1"))
    dialog._poll_results()
    assert "r2" in dialog._summary.text()
    assert "deepseek-chat" not in _tree_text(dialog._intent_tree)


def test_detail_tabs_render_lazily_in_bounded_batches(qtbot, service, monkeypatch):
    from dataclasses import replace

    entry = service.get_for_run("r1")
    entry = replace(entry, transports=[replace(entry.transports[0], stream_tape={
        "events": [{"e": "text", "t": i, "n": 1} for i in range(5000)],
    })])
    monkeypatch.setattr(service, "get_for_run", lambda _run_id: entry)
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._runs.count() == 2)
    dialog._runs.setCurrentRow(0)
    qtbot.waitUntil(lambda: 0 in dialog._rendered_tabs)
    assert dialog._tape_tree.topLevelItemCount() == 0
    dialog._tabs.setCurrentIndex(4)
    dialog._render_batch()
    transport = dialog._tape_tree.topLevelItem(1)
    assert transport is not None
    assert transport.childCount() <= 100
    assert 4 not in dialog._rendered_tabs
    dialog._tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: 2 in dialog._rendered_tabs)
    assert "stream" in _tree_text(dialog._diff_tree)
    dialog._tabs.setCurrentIndex(4)
    qtbot.waitUntil(lambda: 4 in dialog._rendered_tabs)
    assert dialog._tape_tree.topLevelItem(1).childCount() == 5000


def test_malformed_detail_can_be_left_and_retried(qtbot, service, monkeypatch):
    from dataclasses import replace

    entry = service.get_for_run("r1")
    broken = replace(entry, transports=[replace(entry.transports[0], stream_tape={
        "events": [{"e": "text", "t": "invalid"}],
    })])
    monkeypatch.setattr(service, "get_for_run", lambda _run_id: broken)
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._runs.count() == 2)
    dialog._runs.setCurrentRow(0)
    qtbot.waitUntil(lambda: 0 in dialog._rendered_tabs)
    dialog._tabs.setCurrentIndex(4)
    qtbot.waitUntil(lambda: dialog._render_iterator is None)
    assert "内容无法显示" in _tree_text(dialog._tape_tree)
    dialog._tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: 1 in dialog._rendered_tabs)
    assert "HTTP 200" in _tree_text(dialog._transport_tree)


def test_preview_does_not_serialize_an_entire_large_container():
    from limbowave.ui.request_log_dialog import _short

    # Serializing the unused tail would fail, not merely be slow.
    text = _short(["x" * 200, object()])
    assert len(text) == 161
    assert text.endswith("…")


def test_pagination_only_loads_summaries_and_returns_to_previous_page(qtbot, service, monkeypatch):
    def forbidden(*args):
        raise AssertionError("no details until a run is selected")

    monkeypatch.setattr(service, "get_for_run", forbidden)
    with service._uow_factory() as uow:
        for index in range(101):
            uow.runs.add(RunRecord(
                id=f"extra{index:03}", conversation_id="c1", branch_id="b1",
                status=RunStatus.COMPLETED, created_at=T0, user_message_id=f"m{index}",
            ))
        uow.commit()
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._page_future is None)
    first = dialog._runs.item(0).text()
    assert dialog._runs.count() == 100
    assert not dialog._previous.isEnabled()
    assert dialog._next.isEnabled()
    dialog._next.click()
    qtbot.waitUntil(lambda: dialog._page_future is None)
    assert dialog._runs.count() == 3
    assert dialog._previous.isEnabled()
    assert not dialog._next.isEnabled()
    dialog._previous.click()
    qtbot.waitUntil(lambda: dialog._page_future is None)
    assert dialog._runs.count() == 100
    assert dialog._runs.item(0).text() == first
    assert dialog._entry is None


def test_destroying_viewer_with_pending_read_does_not_receive_late_results(
    qtbot, service, monkeypatch,
):
    from concurrent.futures import Future

    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    pending = Future()
    pending.set_running_or_notify_cancel()
    monkeypatch.setattr(service, "load_page_async", lambda *args, **kwargs: pending)
    dialog = RequestLogDialog(service, "c1")
    qtbot.addWidget(dialog)
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(dialog)
    pending.set_result(service.list_summaries("c1"))
    QCoreApplication.processEvents()
