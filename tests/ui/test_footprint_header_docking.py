"""A collapsed gallery header is half clipped above the page, not merely hidden text."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QPushButton

from limbowave.ui import theme
from tests.ui.test_footprint_readability import (
    collapsed,
    show_gallery,
)
from tests.ui.test_footprint_readability import (
    gallery_style as gallery_style,
)


@pytest.mark.parametrize("width", [720, 1080])
def test_compact_header_is_half_above_page_and_bottom_half_at_top(
    qtbot, qapp, gallery_style, width
):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page.resize(width, 760)
    qtbot.wait(30)
    expanded_height = page._scroll.viewport().height()
    page._scroll.verticalScrollBar().setValue(200)
    collapsed(qtbot, page)
    header = page._header_panel
    qtbot.waitUntil(lambda: header.mapTo(page, QPoint()).y() < 0)
    qtbot.wait(30)
    top = header.mapTo(page, QPoint()).y()
    assert abs(top + header.height() / 2) <= 1
    assert page._header_viewport.mapTo(page, QPoint()).y() == 0
    assert abs(page._header_viewport.height() - header.height() / 2) <= 1
    assert page._scroll.viewport().height() > expanded_height + 200
    assert header.visibleRegion().boundingRect().top() >= header.height() // 2 - 1
    back = header.findChild(QPushButton, "footprintReturn")
    assert back.visibleRegion().isEmpty()
    for combo in (page.scope_filter, page.source_filter, page.month_filter, page.order):
        assert not combo.visibleRegion().isEmpty()
    page._scroll.verticalScrollBar().setValue(0)
    qtbot.waitUntil(lambda: not page._title_collapsed)
    qtbot.waitUntil(lambda: page._header_motion.state() == page._header_motion.State.Stopped)
    qtbot.waitUntil(lambda: not page._title_settling)
    qtbot.waitUntil(lambda: page._header_viewport.mapTo(page, QPoint()).y() == 16)
    assert header.mapTo(page, QPoint()).y() == 16
    assert header.height() == page._header_viewport.height()
    assert not back.visibleRegion().isEmpty()
    assert page._scroll.viewport().height() == expanded_height


def test_resize_recalculates_half_docking_and_search_focus_restores_controls(
    qtbot, qapp, gallery_style
):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._scroll.verticalScrollBar().setValue(200)
    collapsed(qtbot, page)
    page.resize(720, 640)
    qtbot.wait(40)
    top = page._header_panel.mapTo(page, QPoint()).y()
    assert abs(top + page._header_panel.height() / 2) <= 1
    qtbot.keyClick(page, Qt.Key.Key_F, modifier=Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not page._title_collapsed)
    qtbot.waitUntil(lambda: not page._title_settling)
    assert page.search.hasFocus()
    assert page.search.visibleRegion().boundingRect() == page.search.rect()


def test_disposal_stops_both_header_animations(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._scroll.verticalScrollBar().setValue(200)
    assert page._header_motion.state() == page._header_motion.State.Running
    page.dispose()
    assert page._header_motion.state() == page._header_motion.State.Stopped


@pytest.mark.parametrize("scale", [1.0, 1.3])
def test_docking_uses_actual_font_and_resize_height(qtbot, qapp, gallery_style, monkeypatch, scale):
    for name in ("FS_TINY", "FS_SMALL", "FS_BASE", "FS_TITLE", "_current_scale"):
        monkeypatch.setattr(theme, name, getattr(theme, name))
    theme.set_font_scale(scale)
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._scroll.verticalScrollBar().setValue(200)
    collapsed(qtbot, page)
    page.resize(760, 640)
    qtbot.wait(40)
    header = page._header_panel
    assert abs(header.mapTo(page, QPoint()).y() + header.height() / 2) <= 1
    assert page._header_viewport.mapTo(page, QPoint()).y() == 0
    assert abs(page._header_viewport.height() - header.height() / 2) <= 1


def test_empty_filter_immediately_restores_entire_header(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._scroll.verticalScrollBar().setValue(200)
    collapsed(qtbot, page)
    page.source_filter.setCurrentIndex(page.source_filter.findData("model"))
    qtbot.waitUntil(lambda: not page.cards)
    qtbot.wait(30)
    assert page._header_panel.mapTo(page, QPoint()).y() == 16
    assert page._header_viewport.height() == page._header_panel.height()
    assert not page._title_collapsed


def test_escape_still_returns_when_navigation_is_offscreen(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._scroll.verticalScrollBar().setValue(200)
    collapsed(qtbot, page)
    with qtbot.waitSignal(page.close_requested):
        qtbot.keyClick(page, Qt.Key.Key_Escape)
