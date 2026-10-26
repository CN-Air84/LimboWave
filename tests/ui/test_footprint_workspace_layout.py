"""Leaving a gallery must not push the sidebar footer or composer offscreen."""

import pytest
from PySide6.QtCore import QPoint, QSize
from PySide6.QtWidgets import QPushButton

from limbowave.ui import theme
from limbowave.ui.main_window import MainWindow
from limbowave.ui.memory_timeline import MemoryTimelinePage
from tests.ui.test_footprint_background import GalleryData
from tests.ui.test_footprint_background import gallery_style as gallery_style
from tests.ui.test_footprint_readability import collapsed


def assert_workspace_fits(window):
    assert window._root_stack.geometry().size() == window._workspace.geometry().size()
    assert window._workspace.rect().contains(window._sidebar_reveal.geometry())
    sidebar = window.sidebar
    assert sidebar.height() == window._sidebar_reveal.height()
    actions = [sidebar._footprints_btn, sidebar._settings_btn]
    assert [button.text() for button in actions] == ["足迹", "设置"]
    for button in actions:
        assert button.isVisibleTo(window)
        assert button.visibleRegion().boundingRect() == button.rect()
        point = button.mapTo(window, QPoint())
        assert window.rect().contains(point)
        assert window.rect().contains(button.mapTo(window, button.rect().bottomRight()))
        assert window.childAt(button.mapTo(window, button.rect().center())) is button
    capsule = window.chat._mode_capsule
    assert capsule.isVisibleTo(window)
    assert window.chat._scroll.viewport().rect().contains(capsule.geometry())
    composer = window.chat._composer
    assert window.rect().contains(composer.mapTo(window, composer.rect().bottomRight()))


@pytest.mark.parametrize("size", [QSize(1100, 760), QSize(900, 600)])
@pytest.mark.parametrize("dock", [False, True])
def test_open_return_preserves_window_size_and_footer(qtbot, qapp, gallery_style, size, dock):
    theme.set_palette("dark")
    qapp.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(size)
    window.sidebar.show_conversations([(str(i), f"会话 {i}", 10) for i in range(20)])
    window.show()
    qtbot.waitExposed(window)
    qtbot.wait(30)
    before = window.size()
    assert_workspace_fits(window)
    for _ in range(2):
        # The live app constructs a child before Qt has given it page geometry.
        page = MemoryTimelinePage(GalleryData(), window)
        page.close_requested.connect(window.show_workspace)
        window.show_full_page(page)
        assert window.size() == before, "Opening footprints must not enlarge the window"
        qtbot.waitUntil(lambda current=page: not current._loading and not window.transition_active)
        assert window.size() == before
        if dock:
            page._scroll.verticalScrollBar().setValue(200)
            collapsed(qtbot, page)
        if dock:
            page._escape.activated.emit()
        else:
            page.findChild(QPushButton, "footprintReturn").click()
        page.dispose()
        qtbot.waitUntil(lambda: not window.transition_active)
        qtbot.wait(30)
        assert window.size() == before
        assert not window.showing_full_page
        assert_workspace_fits(window)


@pytest.mark.parametrize("scale", [1.0, 1.3])
def test_maximized_workspace_footer_survives_opening_at_placeholder_width(
    qtbot,
    qapp,
    gallery_style,
    monkeypatch,
    scale,
):
    for name in ("FS_TINY", "FS_SMALL", "FS_BASE", "FS_TITLE", "_current_scale"):
        monkeypatch.setattr(theme, name, getattr(theme, name))
    theme.set_font_scale(scale)
    theme.set_palette("dark")
    qapp.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    window.showMaximized()
    qtbot.waitExposed(window)
    qtbot.wait(30)
    size = window.size()
    state = window.windowState()
    page = MemoryTimelinePage(GalleryData(), window)
    window.show_full_page(page)
    assert window.size() == size and window.windowState() == state
    qtbot.waitUntil(lambda: not page._loading and not window.transition_active)
    page._scroll.verticalScrollBar().setValue(100)
    collapsed(qtbot, page)
    window.show_workspace()
    page.dispose()
    qtbot.waitUntil(lambda: not window.transition_active)
    qtbot.wait(30)
    assert window.size() == size and window.windowState() == state
    assert_workspace_fits(window)
