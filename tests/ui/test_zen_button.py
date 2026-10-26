"""The paired capsule reveals both labels without relaying out the conversation."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QFocusEvent
from PySide6.QtWidgets import QApplication

from limbowave.ui.chat_view import ChatView, HistoryEntry


@pytest.fixture
def chat(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(680, 700)
    view.show()
    view.load_history([HistoryEntry("user", "消息 " * 10, "", f"u{i}") for i in range(30)])
    qtbot.waitUntil(lambda: view._scroll.verticalScrollBar().maximum() > 0)
    view._input.setFocus()
    qtbot.waitUntil(lambda: view._dock_anim is None and view._dock_progress == 1)
    qtbot.mouseMove(view._scroll.viewport(), QPoint(10, 80))
    qtbot.wait(30)
    return view


def enter(capsule):
    QApplication.sendEvent(capsule, QEnterEvent(QPointF(14, 14), QPointF(14, 14), QPointF(14, 14)))


def leave(capsule):
    QApplication.sendEvent(capsule, QEvent(QEvent.Type.Leave))


def midpoint(capsule):
    animation = capsule._animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    return animation


def divider(capsule):
    return capsule.x() + capsule.companion_button.width()


def test_hover_reveals_both_labels_without_moving_divider_or_content(chat, qtbot):
    capsule = chat._mode_capsule
    anchor = divider(capsule)
    viewport = chat._scroll.geometry()
    composer = chat._composer.geometry()
    bar = chat._scroll.verticalScrollBar()
    position = bar.value()
    assert capsule.width() == capsule.collapsed_width
    enter(capsule)
    animation = midpoint(capsule)
    assert capsule.collapsed_width < capsule.width() < capsule.expanded_width
    assert 0 < capsule._progress < 1
    assert divider(capsule) == anchor
    animation.resume()
    qtbot.waitUntil(lambda: capsule._progress == 1)
    assert capsule.companion_button.caption == "迷你陪伴"
    assert capsule.zen_button.caption == "倾诉模式"
    assert capsule.width() == capsule.expanded_width
    assert capsule.geometry().right() == chat._scroll.viewport().width() - 3
    assert chat._scroll.geometry() == viewport
    assert chat._composer.geometry() == composer
    assert bar.value() == position
    leave(capsule)
    animation = midpoint(capsule)
    assert capsule.collapsed_width < capsule.width() < capsule.expanded_width
    assert divider(capsule) == anchor
    animation.resume()
    qtbot.waitUntil(lambda: capsule._progress == 0)


def test_fast_hover_reversal_keeps_current_width_and_progress(chat, qtbot):
    capsule = chat._mode_capsule
    enter(capsule)
    animation = midpoint(capsule)
    geometry, progress = capsule.geometry(), capsule._progress
    leave(capsule)
    animation.pause()
    assert capsule.geometry() == geometry
    assert capsule._progress == progress
    assert animation.direction() == QAbstractAnimation.Direction.Backward
    enter(capsule)
    animation.pause()
    assert capsule.geometry() == geometry
    assert capsule._progress == progress
    assert animation.direction() == QAbstractAnimation.Direction.Forward
    animation.resume()
    qtbot.waitUntil(lambda: capsule._progress == 1)


def test_expanded_labels_have_independent_actions_and_mouse_focus_does_not_pin(chat, qtbot):
    capsule = chat._mode_capsule
    requests = []
    chat.companion_requested.connect(lambda: requests.append(True))
    enter(capsule)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    qtbot.mouseClick(chat._companion_btn, Qt.MouseButton.LeftButton, pos=QPoint(12, 14))
    assert requests == [True]
    assert not chat.zen_mode
    qtbot.mouseClick(chat._zen_btn, Qt.MouseButton.LeftButton, pos=QPoint(12, 14))
    assert chat.zen_mode and chat._zen_btn.isChecked()
    assert chat._zen_btn.accessibleName() == "退出倾诉模式"
    assert requests == [True]
    leave(capsule)
    qtbot.waitUntil(lambda: capsule.width() == capsule.collapsed_width)
    qtbot.mouseClick(chat._zen_btn, Qt.MouseButton.LeftButton)
    assert not chat.zen_mode


@pytest.mark.parametrize("side", ["companion_button", "zen_button"])
def test_keyboard_focus_reveals_both_labels_and_focus_out_collapses(chat, qtbot, side):
    capsule = chat._mode_capsule
    button = getattr(capsule, side)
    QApplication.sendEvent(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
    qtbot.waitUntil(lambda: capsule._progress == 1)
    leave(capsule)
    assert capsule._progress == 1
    QApplication.sendEvent(button, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason))
    qtbot.waitUntil(lambda: capsule.width() == capsule.collapsed_width)


def test_tab_between_halves_keeps_capsule_open_and_space_activates(chat, qtbot):
    capsule = chat._mode_capsule
    chat.activateWindow()
    chat._companion_btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    qtbot.keyClick(chat._companion_btn, Qt.Key.Key_Tab)
    assert chat._zen_btn.hasFocus()
    qtbot.wait(260)
    assert capsule._progress == 1
    qtbot.keyClick(chat._zen_btn, Qt.Key.Key_Space)
    assert chat.zen_mode
    qtbot.keyClick(chat._zen_btn, Qt.Key.Key_Backtab)
    assert chat._companion_btn.hasFocus()
    requests = []
    chat.companion_requested.connect(lambda: requests.append(True))
    qtbot.keyClick(chat._companion_btn, Qt.Key.Key_Space)
    assert requests == [True]
    assert chat.zen_mode


def test_hide_resets_animation_and_loading_mask_stays_above_both_halves(chat, qtbot):
    capsule = chat._mode_capsule
    enter(capsule)
    midpoint(capsule)
    chat.hide()
    assert capsule._animation.state() == QAbstractAnimation.State.Stopped
    assert capsule.width() == capsule.collapsed_width
    chat.show()
    enter(capsule)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    chat.set_history_loading(True)
    overlay = chat._history_loading_overlay
    qtbot.waitUntil(lambda: overlay._opacity == 1)
    for button in (chat._companion_btn, chat._zen_btn):
        assert chat.childAt(button.mapTo(chat, button.rect().center())) is overlay
    chat.resize(360, 700)
    qtbot.waitUntil(lambda: capsule.geometry().right() == chat._scroll.viewport().width() - 3)
    for button in (chat._companion_btn, chat._zen_btn):
        assert chat.childAt(button.mapTo(chat, button.rect().center())) is overlay
    chat.set_history_loading(False)


def test_disabled_motion_settles_immediately(chat, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: False))
    capsule = chat._mode_capsule
    enter(capsule)
    assert capsule.width() == capsule.expanded_width
    assert capsule._animation.state() == QAbstractAnimation.State.Stopped
    leave(capsule)
    assert capsule.width() == capsule.collapsed_width


@pytest.mark.parametrize("side", ["companion_button", "zen_button"])
def test_real_pointer_stays_over_same_action_during_reveal(chat, qtbot, side):
    capsule = chat._mode_capsule
    button = getattr(capsule, side)
    viewport = chat._scroll.viewport()
    qtbot.mouseMove(viewport, QPoint(10, 80))
    pointer = button.mapTo(chat, button.rect().center())
    qtbot.mouseMove(chat, pointer)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    assert chat.childAt(pointer) is button
    qtbot.mouseMove(button, QPoint(12, 14))
    qtbot.wait(260)
    assert capsule._progress == 1
    qtbot.mouseMove(viewport, QPoint(10, 80))
    qtbot.waitUntil(lambda: capsule._progress == 0)


def test_pointer_crosses_divider_without_collapsing(chat, qtbot):
    capsule = chat._mode_capsule
    qtbot.mouseMove(chat._companion_btn)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    qtbot.mouseMove(capsule, QPoint(capsule.companion_button.width(), 14))
    qtbot.wait(260)
    assert capsule._progress == 1
    qtbot.mouseMove(chat._zen_btn)
    qtbot.wait(260)
    assert capsule._progress == 1


def test_label_width_tracks_font_scale_and_expanded_capsule_stays_in_viewport(chat, qtbot):
    from limbowave.ui import theme

    capsule = chat._mode_capsule
    enter(capsule)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    old_width, right = capsule.width(), capsule.geometry().right()
    old_scale = theme.current_font_scale()
    try:
        theme.set_font_scale(1.6)
        QApplication.sendEvent(capsule, QEvent(QEvent.Type.StyleChange))
        assert capsule.width() == capsule.expanded_width
        assert capsule.width() > old_width
        assert capsule.geometry().right() == right
        chat.resize(360, 700)
        qtbot.waitUntil(lambda: chat._scroll.viewport().rect().contains(capsule.geometry()))
    finally:
        theme.set_font_scale(old_scale)
        QApplication.sendEvent(capsule, QEvent(QEvent.Type.StyleChange))


@pytest.mark.parametrize("zen", [False, True])
def test_first_message_clears_capsule_without_a_fixed_header(chat, qtbot, zen):
    chat.set_zen_mode(zen)
    qtbot.waitUntil(lambda: not chat._zen_motion.running)
    capsule = chat._mode_capsule
    enter(capsule)
    qtbot.waitUntil(lambda: capsule._progress == 1)
    chat._scroll.verticalScrollBar().setValue(0)
    first = chat._rows[0]
    viewport = chat._scroll.viewport()
    assert first.mapTo(viewport, QPoint()).y() > capsule.geometry().bottom()
    assert chat._scroll.y() == 0
