"""Coordinated Zen chrome motion never mutates session or draft state."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QAbstractAnimation, QPoint
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect

from limbowave.ui.chat_view import ChatView, HistoryEntry
from limbowave.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1280, 820)
    window.show()
    window.chat.load_history([HistoryEntry("user", "原会话", "", "u1")])
    qtbot.waitUntil(lambda: window.chat._dock_anim is None)
    window.chat._input.setPlainText("保留草稿")
    qtbot.wait(40)
    return window


def at(animation, fraction):
    animation.pause()
    animation.setCurrentTime(round(animation.duration() * fraction))
    QApplication.processEvents()


def test_sidebar_slides_without_squashing_and_composer_eases(window, qtbot):
    view = window.chat
    host = window._sidebar_reveal
    width = window.sidebar.width()
    before = view._composer.mapTo(window, QPoint()).x()
    draft = view.snapshot_draft()
    view.set_zen_mode(True)
    sidebar_anim = host.motion.animation
    composer_anim = view._zen_motion.animation
    assert sidebar_anim.state() == QAbstractAnimation.State.Running
    at(sidebar_anim, 0.5)
    at(composer_anim, 0.5)
    assert 0 < host.width() < width
    assert window.sidebar.width() == width
    assert window.sidebar.x() < 0
    assert 720 < view._composer.width() < 800
    middle = view._composer.mapTo(window, QPoint()).x()
    assert middle < before
    assert not view._logical_model.isHidden()
    effect = view._logical_model.graphicsEffect()
    assert isinstance(effect, QGraphicsOpacityEffect)
    assert effect.opacity() == pytest.approx(0.5)
    assert view._input.graphicsEffect() is None
    assert view._send_btn.graphicsEffect() is None
    assert view.snapshot_draft() == draft
    sidebar_anim.resume()
    composer_anim.resume()
    qtbot.waitUntil(lambda: host.isHidden() and view._logical_model.isHidden())
    assert window.sidebar.isHidden()
    assert view._composer.width() == 720
    assert view._composer.mapTo(window, QPoint()).x() < middle
    assert view._logical_model.graphicsEffect() is None

    view.set_zen_mode(False)
    at(sidebar_anim, 0.5)
    at(composer_anim, 0.5)
    assert 0 < host.width() < width
    assert not view._logical_model.isHidden()
    assert view._logical_model.graphicsEffect().opacity() == pytest.approx(0.5)
    sidebar_anim.resume()
    composer_anim.resume()
    qtbot.waitUntil(lambda: not host.motion.running and not view._zen_motion.running)
    assert window.sidebar.width() == width
    assert view._composer.width() == 800
    assert view._composer.mapTo(window, QPoint()).x() == before
    assert view.snapshot_draft() == draft


def test_quick_reversal_preserves_geometry_opacity_and_original_width(window, qtbot):
    view = window.chat
    host = window._sidebar_reveal
    window._splitter.setSizes([350, 900])
    width = window.sidebar.width()
    view.set_zen_mode(True)
    at(host.motion.animation, 0.5)
    at(view._zen_motion.animation, 0.5)
    original = (host.width(), view._composer.geometry(),
                view._logical_model.graphicsEffect().opacity())
    view.set_zen_mode(False)
    host.motion.animation.pause()
    view._zen_motion.animation.pause()
    assert (host.width(), view._composer.geometry(),
            view._logical_model.graphicsEffect().opacity()) == original
    view.set_zen_mode(True)
    host.motion.animation.pause()
    view._zen_motion.animation.pause()
    assert (host.width(), view._composer.geometry(),
            view._logical_model.graphicsEffect().opacity()) == original
    view.set_zen_mode(False)
    qtbot.waitUntil(lambda: not host.motion.running and not view._zen_motion.running)
    assert window.sidebar.width() == width
    assert view._logical_model.graphicsEffect() is None


def test_disabled_controls_and_loading_mask_survive_animation(window, qtbot):
    view = window.chat
    view.set_available(False)
    view.set_zen_mode(True)
    at(view._zen_motion.animation, 0.5)
    assert not view._permission_btn.isEnabled()
    view.set_history_loading(True)
    overlay = view._history_loading_overlay
    qtbot.waitUntil(lambda: overlay._opacity == 1)
    assert view.childAt(view._composer.mapTo(view, view._composer.rect().center())) is overlay
    view.set_zen_mode(False)
    qtbot.waitUntil(lambda: not view._zen_motion.running)
    assert not view._permission_btn.isEnabled()
    view.set_history_loading(False)


def test_hidden_window_and_disabled_motion_settle_without_leaked_effects(
    window, qtbot, monkeypatch,
):
    view = window.chat
    host = window._sidebar_reveal
    view.set_zen_mode(True)
    at(host.motion.animation, 0.5)
    at(view._zen_motion.animation, 0.5)
    window.hide()
    assert not host.motion.running
    assert not view._zen_motion.running
    assert window.sidebar.isHidden()
    assert view._logical_model.graphicsEffect() is None
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: False))
    window.show()
    view.set_zen_mode(False)
    assert window.sidebar.isVisible()
    assert view._logical_model.isVisible()
    assert not view._zen_motion.running


def test_standalone_composer_width_animates(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1000, 700)
    view.show()
    view.load_history([HistoryEntry("user", "你好", "", "u1")])
    qtbot.waitUntil(lambda: view._dock_anim is None)
    view.set_zen_mode(True)
    at(view._zen_motion.animation, 0.5)
    assert view._composer.width() == 760
    view._zen_motion.animation.resume()
    qtbot.waitUntil(lambda: not view._zen_motion.running)
    assert view._composer.width() == 720


def test_advanced_panel_fades_as_snapshot_without_nested_effects(window, qtbot):
    view = window.chat
    window.toolbar.set_collapsed(False)
    qtbot.waitUntil(lambda: view.advanced_progress == 1)
    view.set_zen_mode(True)
    at(view._zen_motion.animation, 0.5)
    snapshot = view._zen_panel_snapshot
    assert snapshot.isVisible()
    assert snapshot.opacity == pytest.approx(0.5)
    assert not snapshot.frame.isNull()
    assert not window.toolbar.isVisible()
    assert not window.toolbar.collapsed
    assert view._advanced_clip.graphicsEffect() is None
    view._zen_motion.animation.resume()
    qtbot.waitUntil(lambda: not view._zen_motion.running)
    assert snapshot.isHidden()
    view.set_zen_mode(False)
    at(view._zen_motion.animation, 0.5)
    assert snapshot.isVisible()
    assert snapshot.opacity == pytest.approx(0.5)
    view._zen_motion.animation.resume()
    qtbot.waitUntil(lambda: not view._zen_motion.running)
    assert window.toolbar.isVisible()
    assert snapshot.isHidden()


def test_preview_and_resizing_keep_transition_targets(window, qtbot):
    view = window.chat
    width = window.sidebar.width()
    view.set_zen_mode(True)
    at(window._sidebar_reveal.motion.animation, 0.5)
    at(view._zen_motion.animation, 0.5)
    preview = window.prepare_history_preview()
    window.show_history_preview()
    assert preview.zen_mode
    assert view._logical_model.graphicsEffect() is None
    preview.set_zen_mode(False)
    at(preview._zen_motion.animation, 0.5)
    window.resize(1080, 760)
    preview._zen_motion.animation.resume()
    qtbot.waitUntil(lambda: not preview._zen_motion.running
                   and not window._sidebar_reveal.motion.running)
    assert not view.zen_mode
    assert window.sidebar.width() == width
    assert preview._logical_model.graphicsEffect() is None
    window.clear_history_preview()
    assert view.isVisible()
    assert not view._logical_model.isHidden()


def test_reading_anchor_is_held_for_entire_transition(window, qtbot):
    view = window.chat
    view.load_history([HistoryEntry("user", "消息 " * 15, "", f"u{i}") for i in range(40)])
    bar = view._scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 1000)
    bar.setValue(bar.maximum() // 2)
    viewport = view._scroll.viewport()
    anchor = next(row for row in view._rows
                  if row.mapTo(viewport, QPoint()).y() + row.height() > 0)
    y = anchor.mapTo(viewport, QPoint()).y()
    view.set_zen_mode(True)
    at(window._sidebar_reveal.motion.animation, 0.5)
    at(view._zen_motion.animation, 0.5)
    qtbot.wait(60)
    assert view._history_scroll_anchor is not None
    assert abs(anchor.mapTo(viewport, QPoint()).y() - y) <= 2
    window._sidebar_reveal.motion.animation.resume()
    view._zen_motion.animation.resume()
    qtbot.waitUntil(lambda: view._history_scroll_anchor is None)
    assert abs(anchor.mapTo(viewport, QPoint()).y() - y) <= 2
    assert bar.value() < bar.maximum() - 100


def test_already_hidden_sidebar_stays_hidden_and_does_not_reappear(window, qtbot):
    window.sidebar.hide()
    assert window._sidebar_reveal.isHidden()
    window.chat.set_zen_mode(True)
    window.chat.set_zen_mode(False)
    qtbot.waitUntil(lambda: not window.chat._zen_motion.running)
    assert window.sidebar.isHidden()
    assert window._sidebar_reveal.isHidden()


def test_easing_has_small_steps_at_both_endpoints(window):
    view = window.chat
    host = window._sidebar_reveal
    width = window.sidebar.width()
    view.set_zen_mode(True)
    positions = []
    for progress in (0, 0.1, 0.4, 0.5, 0.9, 1):
        at(host.motion.animation, progress)
        at(view._zen_motion.animation, progress)
        positions.append(view._composer.mapTo(window, QPoint()).x())
    assert positions[0] >= positions[1] > positions[2] > positions[3] > positions[4] >= positions[5]
    assert positions[0] - positions[1] < positions[2] - positions[3]
    assert positions[4] - positions[5] < positions[2] - positions[3]
    assert window.sidebar.width() == width


def test_fading_controls_leave_keyboard_order_and_restore_original_policy(window, qtbot):
    from PySide6.QtCore import Qt

    view = window.chat
    model = view._logical_model
    original = model.focusPolicy()
    window.activateWindow()
    model.setFocus()
    view.set_zen_mode(True)
    at(view._zen_motion.animation, 0.5)
    assert model.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert not model.hasFocus()
    view.set_zen_mode(False)
    qtbot.waitUntil(lambda: not view._zen_motion.running)
    assert model.focusPolicy() == original
    assert not model.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_live_stream_and_manual_sidebar_drag_survive_round_trip(window, qtbot):
    view = window.chat
    view.set_busy(True)
    view.begin_assistant()
    view.append_assistant_delta("仍然")
    row = view._stream_row
    view.set_zen_mode(True)
    at(window._sidebar_reveal.motion.animation, 0.5)
    at(view._zen_motion.animation, 0.5)
    view.append_assistant_delta("在这里。")
    view.end_assistant("仍然在这里。", "a1")
    view.set_busy(False)
    assert row.content_text() == "仍然在这里。"
    view.set_zen_mode(False)
    qtbot.waitUntil(lambda: not view._zen_motion.running
                   and not window._sidebar_reveal.motion.running)
    window._splitter.setSizes([380, 899])
    QApplication.processEvents()
    assert window.sidebar.width() == 380
    assert window._sidebar_reveal.width() == 380
    assert window.sidebar.x() == 0
    assert window.sidebar.maximumWidth() == 420
    assert window.sidebar.minimumWidth() == 240
