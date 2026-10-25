from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot

from limbowave.ui.main_window import MainWindow


def _rect(window, widget):
    return QRect(widget.mapTo(window._content_host, QPoint()), widget.size())


def _open(qtbot, window):
    window.chat._advanced_btn.click()
    qtbot.waitUntil(lambda: window.chat.advanced_progress == 1.0)


@pytest.mark.parametrize("docked", [False, True])
@pytest.mark.parametrize("width", [760, 850, 1200, 1600])
def test_advanced_overlays_right_half_without_moving_composer(qtbot: QtBot, docked, width):
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(width, 800)
    window.show()
    if docked:
        window.chat.add_user_message("hello")
        qtbot.waitUntil(lambda: window.chat.composer_lift == 0)
    qtbot.wait(100)
    before = _rect(window, window.chat._composer)
    transcript = _rect(window, window.chat._scroll)
    _open(qtbot, window)
    panel = _rect(window, window.toolbar)
    assert _rect(window, window.chat._composer) == before
    assert _rect(window, window.chat._scroll) == transcript
    assert window.chat._composer_row.count() == 1
    assert before.contains(panel)
    assert panel.right() == before.right()
    assert panel.bottom() == before.bottom()
    assert abs(panel.width() - before.width() / 2) <= 1
    assert panel.height() <= 125
    assert not panel.intersects(transcript)
    window.toolbar._collapse_btn.click()
    qtbot.waitUntil(lambda: window.chat.advanced_progress == 0.0)
    assert not window.toolbar.isVisible()
    assert _rect(window, window.chat._composer) == before


def test_sending_does_not_open_popover(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message('hello')
    qtbot.waitUntil(lambda: window.chat.composer_lift == 0)
    assert window.toolbar.collapsed
    assert not window.toolbar.isVisible()


def test_region_persists_while_typing_and_nested_popup_owns_escape(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    _open(qtbot, window)
    qtbot.mouseClick(window.toolbar._endpoint_value, Qt.MouseButton.LeftButton)
    nested = window.toolbar._endpoint_panel
    assert nested is not None
    qtbot.mouseClick(nested.findChildren(QLabel)[0], Qt.MouseButton.LeftButton)
    assert not window.toolbar.collapsed
    nested.close_panel()
    qtbot.waitUntil(lambda: window.toolbar._endpoint_panel is None)
    qtbot.keyClick(window.toolbar._thinking, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not window.toolbar.isVisible())
    qtbot.waitUntil(lambda: QApplication.focusWidget() is window.chat._advanced_btn)
    _open(qtbot, window)
    qtbot.mouseClick(window.chat._input.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(12, 12))
    qtbot.keyClicks(window.chat._input, "draft")
    assert window.chat._input.hasFocus()
    assert window.chat._input.toPlainText() == "draft"
    assert window.toolbar.isVisible()
    assert not window.toolbar.collapsed
    qtbot.mouseClick(window.chat._scroll.viewport(), Qt.MouseButton.LeftButton)
    assert not window.toolbar.collapsed


def test_dismissing_popover_cancels_compression_confirmation(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    _open(qtbot, window)
    window.toolbar._compress_btn.click()
    assert window.toolbar._action_row.slider_active
    qtbot.keyClick(window.toolbar._compress_btn, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.toolbar.collapsed)
    assert not window.toolbar._action_row.slider_active
    assert not window.toolbar._slider_timer.isActive()


def test_region_reflows_on_resize_without_auto_collapsing(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message('hello')
    qtbot.waitUntil(lambda: window.chat.composer_lift == 0)
    _open(qtbot, window)
    for width, height in [(850, 620), (1200, 800), (760, 560)]:
        window.resize(width, height)
        qtbot.wait(50)
        assert not window.toolbar.collapsed
        assert window._content_host.rect().contains(_rect(window, window.toolbar))
        composer = _rect(window, window.chat._composer)
        panel = _rect(window, window.toolbar)
        assert composer.contains(panel)
        assert abs(panel.width() - composer.width() / 2) <= 1
        assert not panel.intersects(_rect(window, window.chat._scroll))


def test_native_thinking_popup_owns_its_clicks_and_escape(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    _open(qtbot, window)
    combo = window.toolbar._thinking
    combo.showPopup()
    qtbot.waitUntil(lambda: combo.view().isVisible())
    qtbot.wait(200)
    row = combo.view().visualRect(combo.model().index(4, 0)).center()
    qtbot.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=row)
    assert combo.currentText() == 'high'
    assert not window.toolbar.collapsed
    combo.showPopup()
    qtbot.wait(200)
    qtbot.keyClick(combo.view(), Qt.Key.Key_Escape)
    assert not combo.view().isVisible()
    assert not window.toolbar.collapsed
    qtbot.keyClick(combo, Qt.Key.Key_Escape)
    assert window.toolbar.collapsed


@pytest.mark.parametrize("scale", [1.0, 1.6])
@pytest.mark.parametrize("width", [218, 262, 320, 400])
def test_selector_halves_share_one_row_without_clipping(qtbot: QtBot, qapp, width, scale):
    from limbowave.ui import theme
    from limbowave.ui.session_toolbar import SessionToolbar

    old_scale, old_css = theme.current_font_scale(), qapp.styleSheet()
    theme.set_font_scale(scale)
    qapp.setStyleSheet(theme.app_stylesheet())
    try:
        toolbar = SessionToolbar()
        qtbot.addWidget(toolbar)
        toolbar.use_external_toggle()
        toolbar.setFixedWidth(width)
        full_name = 'An unusually long provider name ' * 10
        toolbar.set_model_info('', full_name)
        toolbar.show()
        qtbot.wait(50)
        endpoint = toolbar._endpoint_value
        thinking = toolbar._thinking
        assert endpoint.text() == full_name
        assert full_name in endpoint.toolTip()
        assert endpoint.accessibleName() == "实际站点"
        assert thinking.accessibleName() == "思考强度"
        assert endpoint.y() == thinking.y()
        assert endpoint.geometry().right() < thinking.x()
        assert abs(endpoint.width() - thinking.width()) <= 1
        labels = [label for label in toolbar.findChildren(QLabel)
                  if label.property("advancedLabel")]
        assert len(labels) == 2
        left = labels[0].geometry().united(endpoint.geometry())
        right = labels[1].geometry().united(thinking.geometry())
        assert abs(left.width() - right.width()) <= 1
        assert left.right() < right.left()
        assert toolbar._body.rect().contains(left)
        assert toolbar._body.rect().contains(right)
        assert endpoint.height() == thinking.height() >= 28
        assert toolbar.height() <= (125 if scale == 1.0 else 160)
        for button in toolbar._action_buttons:
            assert toolbar._action_row.rect().contains(button.geometry())
        assert toolbar.width() == width
        assert toolbar._collapse_btn.isVisible()
        assert thinking.property('themeEffectDisabled')
    finally:
        theme.set_font_scale(old_scale)
        qapp.setStyleSheet(old_css)


def test_history_preview_hides_region_and_restores_it_with_live_chat(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    _open(qtbot, window)
    preview = window.prepare_history_preview()
    preview.load_history([("user", "old message", "", "m1")])
    window.show_history_preview()
    assert not window.toolbar.isVisible()
    window.clear_history_preview()
    qtbot.wait(50)
    assert window.toolbar.isVisible()
    assert not window.toolbar.collapsed
    assert _rect(window, window.chat._composer).contains(_rect(window, window.toolbar))


def test_escape_in_another_window_does_not_close_region(qtbot: QtBot):
    from PySide6.QtWidgets import QDialog

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    _open(qtbot, window)
    dialog = QDialog(window)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.isVisible()
    assert not window.toolbar.collapsed


def test_region_stays_open_when_first_message_docks_composer(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    _open(qtbot, window)
    window.chat.add_user_message("hello")
    qtbot.waitUntil(lambda: window.chat.composer_lift == 0)
    assert not window.toolbar.collapsed
    panel = _rect(window, window.toolbar)
    composer = _rect(window, window.chat._composer)
    assert composer.contains(panel)
    assert panel.bottom() == composer.bottom()
    assert not panel.intersects(_rect(window, window.chat._scroll))


def test_overlay_intercepts_covered_controls_but_leaves_left_input_available(qtbot: QtBot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    _open(qtbot, window)
    composer = window.chat._composer
    # This used to be the send button. The topmost hit must now belong to settings.
    send_point = window.chat._send_btn.mapTo(composer, window.chat._send_btn.rect().center())
    covered = composer.childAt(send_point)
    assert covered is window.toolbar or window.toolbar.isAncestorOf(covered)
    left_point = window.chat._input.viewport().mapTo(composer, QPoint(12, 12))
    assert composer.childAt(left_point) is window.chat._input.viewport()
    window.toolbar._collapse_btn.click()
    qtbot.waitUntil(lambda: window.chat.advanced_progress == 0.0)
    assert composer.childAt(send_point) is window.chat._send_btn


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 1.6])
def test_compact_settings_remain_inside_composer_at_larger_fonts(qtbot: QtBot, qapp, scale):
    from limbowave.ui import theme

    old_scale, old_css = theme.current_font_scale(), qapp.styleSheet()
    try:
        theme.set_font_scale(scale)
        qapp.setStyleSheet(theme.app_stylesheet())
        window = MainWindow()
        qtbot.addWidget(window)
        window.resize(1200, 800)
        window.show()
        _open(qtbot, window)
        panel = window.toolbar
        assert panel.sizeHint().height() <= window.chat._composer.height()
        for widget in (panel._thinking, panel._endpoint_value, panel._export_btn,
                       panel._collapse_btn, *panel._action_buttons):
            rect = QRect(widget.mapTo(panel, QPoint()), widget.size())
            assert panel.rect().contains(rect)
        assert _rect(window, window.chat._composer).contains(_rect(window, panel))
    finally:
        theme.set_font_scale(old_scale)
        qapp.setStyleSheet(old_css)
