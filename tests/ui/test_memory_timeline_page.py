"""Footprint gallery behavior and safe local-only display."""

from threading import Event

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from limbowave.domain.footprint import MemoryFootprint
from limbowave.domain.memory import MemoryItem
from limbowave.ui import theme
from limbowave.ui.memory_timeline import PAGE_SIZE, MemoryTimelinePage, _date_key
from limbowave.ui.sidebar import Sidebar
from tests.unit.test_memory_service import NOW
from tests.unit.test_memory_service import memory_stack as memory_stack
from tests.unit.test_memory_timeline import put_items


def show_page(qtbot, service):
    page = MemoryTimelinePage(service)
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    qtbot.waitUntil(lambda: not page._loading)
    return page


def test_empty_loading_and_privacy_boundary(qtbot, memory_stack):
    service, _ = memory_stack
    page = show_page(qtbot, service)
    assert not page.cards
    assert "已经为你留好了位置" in page.status.text()
    assert "0 段记忆" in page.summary.text()
    assert not page.progress.isVisible()
    assert page.privacy_button.isVisible() and not page.privacy.isVisible()
    qtbot.mouseClick(page.privacy_button, Qt.MouseButton.LeftButton)
    assert page.privacy.isVisible()
    assert "加密保存在本机" in page.privacy.text()
    assert "可能随请求发送" in page.privacy.text()
    assert "已归档" in page.privacy.text()
    with qtbot.waitSignal(page.manage_requested):
        button = next(b for b in page.findChildren(QPushButton) if b.text() == "管理记忆")
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


def test_search_scope_source_month_and_order(qtbot, memory_stack):
    service, factory = memory_stack
    put_items(factory, "global", [MemoryItem("a", "MOON 月亮", "user", "2026-09-01T00:00:00Z")])
    put_items(
        factory,
        "branch:b",
        [MemoryItem("b", "星星", "model:r:c", "2026-10-24T00:00:00Z")],
        "c",
        "b",
    )
    page = show_page(qtbot, service)
    assert [c.row.memory.id for c in page.cards] == ["b", "a"]
    page.order.setCurrentIndex(page.order.findData("oldest"))
    assert [c.row.memory.id for c in page.cards] == ["a", "b"]
    page.scope_filter.setCurrentIndex(page.scope_filter.findData("session"))
    assert [c.row.memory.id for c in page.cards] == ["b"]
    page.clear_filters()
    page.source_filter.setCurrentIndex(page.source_filter.findData("user"))
    assert [c.row.memory.id for c in page.cards] == ["a"]
    page.clear_filters()
    page.month_filter.setCurrentIndex(page.month_filter.findData("2026-09"))
    assert [c.row.memory.id for c in page.cards] == ["a"]
    page.clear_filters()
    page.search.setText("moon")
    qtbot.waitUntil(lambda: len(page.cards) == 1)
    assert page.cards[0].row.memory.id == "a"
    page.search.setText("chat")
    qtbot.waitUntil(lambda: len(page.cards) == 1 and page.cards[0].row.memory.id == "b")
    page.search.setText("cannot-match-anything")
    qtbot.waitUntil(lambda: not page.cards)
    assert page.reset_button.isVisible()
    qtbot.mouseClick(page.reset_button, Qt.MouseButton.LeftButton)
    assert len(page.cards) == 2


def test_complete_plaintext_expansion_and_no_html(qtbot, memory_stack):
    service, _ = memory_stack
    content = '<img src="https://invalid.example/image">\n' + "完整的原文" * 70
    service.save(content)
    page = show_page(qtbot, service)
    card = page.cards[0]
    assert card.content.textFormat() == Qt.TextFormat.PlainText
    assert card.content.text() != content
    expand = card.findChild(QPushButton, "footprintExpand")
    qtbot.mouseClick(expand, Qt.MouseButton.LeftButton)
    assert card.content.text() == content
    qtbot.mouseClick(expand, Qt.MouseButton.LeftButton)
    assert card.content.text() != content
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in page.findChildren(QLabel))


def test_source_signal_contains_scope_without_private_search_text(qtbot, memory_stack):
    service, _ = memory_stack
    item = service.save("秘密正文", "c", "b")
    page = show_page(qtbot, service)
    card = page.cards[0]
    assert card.row.memory == item
    button = card.findChild(QPushButton, "footprintOpenSource")
    with qtbot.waitSignal(page.source_requested) as signal:
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert signal.args == [("c", "b", None, "")]


def test_incremental_rendering_and_refresh_after_delete(qtbot, memory_stack):
    service, factory = memory_stack
    rows = [MemoryItem(str(i), f"片段 {i}", "user", NOW.isoformat()) for i in range(PAGE_SIZE + 3)]
    put_items(factory, "global", rows)
    page = show_page(qtbot, service)
    assert len(page.cards) == PAGE_SIZE
    assert page.more_button.isVisible()
    qtbot.mouseClick(page.more_button, Qt.MouseButton.LeftButton)
    assert len(page.cards) == PAGE_SIZE + 3
    assert not page.more_button.isVisible()
    service.delete(rows[0].id)
    page.reload()
    qtbot.waitUntil(lambda: not page._loading)
    assert len(page._entries) == PAGE_SIZE + 2
    assert all(row.memory.id != rows[0].id for row in page._entries)


def test_known_local_days_and_unknown_timestamp_group(qtbot, memory_stack):
    service, factory = memory_stack
    rows = [
        MemoryItem("a", "第一段", "user", "2026-10-24T22:00:00+08:00"),
        MemoryItem("b", "第二段", "user", "2026-10-24T23:00:00+08:00"),
        MemoryItem("unknown", "旧日期", "user", "not-a-time"),
    ]
    put_items(factory, "global", rows)
    page = show_page(qtbot, service)
    headings = page.findChildren(QLabel, "footprintDayHeading")
    assert len(headings) == 2
    assert headings[-1].text() == "日期未记录"
    assert _date_key(MemoryFootprint(rows[0], "global")) == "2026-10-24"
    page.month_filter.setCurrentIndex(page.month_filter.findData("unknown"))
    assert [c.row.memory.id for c in page.cards] == ["unknown"]


def test_refresh_failure_is_safe_and_retryable(qtbot, memory_stack, monkeypatch):
    service, _ = memory_stack
    original = service.timeline

    def failure():
        raise ValueError("sensitive plaintext must not be shown")

    monkeypatch.setattr(service, "timeline", failure)
    page = show_page(qtbot, service)
    assert "读取失败" in page.status.text()
    assert "sensitive" not in page.status.text()
    assert page.refresh_button.isEnabled()
    monkeypatch.setattr(service, "timeline", original)
    qtbot.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page._loading)
    assert "已经为你留好了位置" in page.status.text()


def test_disposal_ignores_late_plaintext_results(qtbot, memory_stack, monkeypatch):
    service, _ = memory_stack
    release = Event()
    begun = Event()

    def delayed():
        begun.set()
        release.wait(3)
        return [
            MemoryFootprint(
                MemoryItem("late", "late private text", "user", NOW.isoformat()), "global"
            )
        ]

    monkeypatch.setattr(service, "timeline", delayed)
    page = MemoryTimelinePage(service)
    qtbot.addWidget(page)
    assert begun.wait(1)
    page.dispose()
    release.set()
    qtbot.waitUntil(lambda: not page._jobs._callbacks)
    assert not page._entries and not page.cards
    assert "late private text" not in [label.text() for label in page.findChildren(QLabel)]


def test_sidebar_footprint_entry_is_a_user_intent(qtbot):
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    with qtbot.waitSignal(sidebar.footprints_requested):
        qtbot.mouseClick(sidebar._footprints_btn, Qt.MouseButton.LeftButton)
    assert "足迹" in sidebar._footprints_btn.accessibleName()
    for palette in ("light", "dark"):
        theme.set_palette(palette)
        sidebar.restyle()
        assert not sidebar._footprints_btn.icon().isNull()
    assert "sidebarFootprints:hover" in theme.app_stylesheet()
