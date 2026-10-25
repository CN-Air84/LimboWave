"""Scoped, neutral sidebar appearance and accessible navigation regressions."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from limbowave.ui import theme
from limbowave.ui.main_window import MainWindow
from limbowave.ui.sidebar import ROLE_ACTIVE, Sidebar


@pytest.fixture(autouse=True)
def restore_theme(qapp):
    old_css = qapp.styleSheet()
    yield
    theme.set_palette("dark")
    qapp.setStyleSheet(old_css)


def _rule(css: str, selector: str) -> str:
    return css.rsplit(selector + " {", 1)[1].split("}", 1)[0]


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_sidebar_uses_scoped_neutral_states(qtbot, qapp, palette):
    theme.set_palette(palette)
    css = theme.app_stylesheet()
    qapp.setStyleSheet(css)
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)

    assert not sidebar._new_btn.property("accent")
    assert sidebar._new_btn.objectName() == "sidebarNewConversation"
    assert sidebar._list.objectName() == "conversationList"
    button = _rule(css, "QPushButton#sidebarNewConversation")
    selected = _rule(css, "QListWidget#conversationList::item:selected")
    for rule in (button, selected):
        assert theme.qss_alpha(theme.TEXT_PRIMARY, 0.08) in rule
        assert theme.ACCENT not in rule
    assert "border: none" in _rule(css, "QListWidget#conversationList::item")
    assert theme.qss_alpha(theme.TEXT_PRIMARY, 0.04) in _rule(
        css, "QListWidget#conversationList::item:hover"
    )
    assert theme.qss_alpha(theme.TEXT_PRIMARY, 0.35) in _rule(
        css, "QPushButton#sidebarSettings:focus"
    )
    assert f"background: {theme.ACCENT}" in _rule(css, 'QPushButton[accent="true"]')


def test_settings_is_full_width_left_aligned_row(qtbot, qapp):
    qapp.setStyleSheet(theme.app_stylesheet())
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.resize(292, 500)
    sidebar.show()
    qtbot.waitExposed(sidebar)
    button = sidebar._settings_btn

    assert button.objectName() == "sidebarSettings"
    assert not button.icon().isNull()
    assert button.iconSize() == QSize(18, 18)
    assert button.height() >= 40
    assert button.x() == sidebar._list.x()
    assert button.width() == sidebar._list.width()
    assert "text-align: left" in _rule(theme.app_stylesheet(), "QPushButton#sidebarSettings")
    assert button.accessibleName() == "设置"
    with qtbot.waitSignal(sidebar.settings_requested):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(button.width() - 12, 20))
    sidebar.activateWindow()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(button.hasFocus)
    with qtbot.waitSignal(sidebar.settings_requested):
        qtbot.keyClick(button, Qt.Key.Key_Space)


def test_sidebar_icons_refresh_with_palette(qtbot):
    theme.set_palette("dark")
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    dark = sidebar._settings_btn.icon().pixmap(18, 18).toImage()
    theme.set_palette("light")
    sidebar.restyle()
    light = sidebar._settings_btn.icon().pixmap(18, 18).toImage()
    assert not dark.isNull()
    assert not light.isNull()
    assert dark != light


def test_active_delegate_uses_fill_instead_of_native_focus_frame(qtbot, monkeypatch):
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "会话", 1)])
    sidebar.set_active("c1")
    item = sidebar._list.item(0)
    assert item.data(ROLE_ACTIVE)
    option = QStyleOptionViewItem()
    option.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_HasFocus
    captured = []
    monkeypatch.setattr(
        QStyledItemDelegate, "paint",
        lambda self, painter, opt, idx: captured.append(opt.state),
    )

    image = QPixmap(280, 54)
    painter = QPainter(image)
    try:
        sidebar._list.itemDelegate().paint(painter, option, sidebar._list.indexFromItem(item))
    finally:
        painter.end()
    assert not captured[0] & QStyle.StateFlag.State_HasFocus
    assert captured[0] & QStyle.StateFlag.State_Selected
    assert option.state & QStyle.StateFlag.State_HasFocus  # input was not mutated


def test_workspace_divider_is_quiet_and_scoped(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert window._splitter.objectName() == "workspaceSplitter"
    assert window._splitter.handleWidth() == 1
    css = theme.app_stylesheet()
    rule = _rule(css, "QSplitter#workspaceSplitter::handle")
    assert theme.qss_alpha(theme.TEXT_PRIMARY, 0.04) in rule
    assert theme.qss_alpha(theme.TEXT_PRIMARY, 0.12) in _rule(
        css, "QSplitter#workspaceSplitter::handle:hover"
    )
    assert f"background: {theme.BORDER}" in _rule(css, "QSplitter::handle")


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_selection_fill_does_not_tint_entire_viewport(qtbot, qapp, palette):
    theme.set_palette(palette)
    qapp.setStyleSheet(theme.app_stylesheet())
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.resize(292, 500)
    sidebar.show_conversations([("c1", "A", 1), ("c2", "B", 2)])
    sidebar.set_active("c1")
    sidebar.show()
    sidebar.activateWindow()
    sidebar._list.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(sidebar._list.hasFocus)
    qapp.processEvents()
    image = sidebar.grab().toImage()
    viewport = sidebar._list.viewport()
    base = image.pixelColor(5, 200)
    blank = image.pixelColor(viewport.mapTo(sidebar, QPoint(30, 200)))
    assert blank == base
    row = sidebar._list.visualItemRect(sidebar._list.item(0))
    fill = image.pixelColor(viewport.mapTo(sidebar, QPoint(220, row.center().y())))
    assert fill != base
    assert fill.name() != theme.ACCENT.lower()
    # The left edge is the same neutral fill, not a focus frame or blue rail.
    left = image.pixelColor(viewport.mapTo(sidebar, QPoint(row.left() + 4, row.center().y())))
    assert left == fill
    with qtbot.waitSignal(sidebar.conversation_selected) as selected:
        qtbot.keyClick(sidebar._list, Qt.Key.Key_Down)
    assert selected.args == ["c2"]
    assert sidebar._list.currentRow() == 1
