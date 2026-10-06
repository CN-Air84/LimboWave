"""Thinking reveal animates layout without reflowing or rebuilding text per frame."""

import pytest
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from limbowave.ui.chat_view import ChatView
from limbowave.ui.thinking_block import ThinkingBlock


@pytest.fixture
def block(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    host = QWidget()
    layout = QVBoxLayout(host)
    thinking = ThinkingBlock()
    layout.addWidget(thinking)
    layout.addWidget(QLabel("Following answer"))
    layout.addStretch()
    thinking.set_text("Reasoning line\n" * 8)
    host.resize(600, 600)
    qtbot.addWidget(host)
    host.show()
    qtbot.waitExposed(host)
    yield thinking


def midpoint(block):
    animation = block._animation
    assert animation.state() == QAbstractAnimation.State.Running
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    QApplication.processEvents()
    return animation


def finish(block, qtbot):
    block._animation.resume()
    qtbot.waitUntil(lambda: block._animation.state() == QAbstractAnimation.State.Stopped)
    QApplication.processEvents()


def test_reveal_animates_height_and_following_answer_without_squashing_text(block, qtbot):
    following = block.parentWidget().layout().itemAt(1).widget()
    before = following.y()
    block._toggle.click()
    assert block._clip.height() == 0
    animation = midpoint(block)
    natural = block._content.height() + 6
    assert block._clip.height() == pytest.approx(natural / 2, abs=1)
    assert following.y() - before == pytest.approx(natural / 2, abs=1)
    assert block._content.verticalScrollBar().maximum() == 0
    assert block._toggle.isChecked()
    finish(block, qtbot)
    assert block._clip.height() == natural
    assert animation.state() == QAbstractAnimation.State.Stopped

    block._toggle.click()
    midpoint(block)
    assert block._content.isVisible()  # Keep painting until the collapse finishes.
    assert block._clip.height() == pytest.approx(natural / 2, abs=1)
    assert not block._toggle.isChecked()
    finish(block, qtbot)
    assert block._content.isHidden()
    assert block._clip.height() == 0
    assert following.y() == before  # No leftover gap or final layout jump.


def test_toggle_reverses_the_same_timeline_without_jumping(block, qtbot):
    block._toggle.click()
    animation = midpoint(block)
    before = (block._clip.height(), block.height(), animation.currentValue())
    block._toggle.click()
    animation.pause()
    QApplication.processEvents()
    assert animation.direction() == QAbstractAnimation.Direction.Backward
    assert (block._clip.height(), block.height(), animation.currentValue()) == before
    block._toggle.click()
    animation.pause()
    QApplication.processEvents()
    assert animation.direction() == QAbstractAnimation.Direction.Forward
    assert (block._clip.height(), block.height(), animation.currentValue()) == before
    finish(block, qtbot)
    assert not block._content.isHidden()


def test_streaming_during_reveal_preserves_progress_and_selection(block, qtbot):
    block._toggle.click()
    animation = midpoint(block)
    cursor = block._content.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
    block._content.setTextCursor(cursor)
    selected = block._content.textCursor().selectedText()
    height = block._clip.height()
    for _ in range(20):
        block.append("More reasoning\n")
    qtbot.waitUntil(lambda: block._content.toPlainText() == block.text())
    assert animation.currentValue() == pytest.approx(0.5)
    assert block._clip.height() > height
    assert block._clip.height() == pytest.approx((block._content.height() + 6) / 2, abs=1)
    assert block._content.textCursor().selectedText() == selected
    finish(block, qtbot)


def test_collapse_stops_rendering_new_deltas_immediately(block, qtbot):
    block._toggle.click()
    midpoint(block)
    block._toggle.click()
    block._animation.pause()
    rendered = block._content.toPlainText()
    block.append("Buffered while closing")
    assert not block._flush_timer.isActive()
    qtbot.wait(70)
    assert block._content.toPlainText() == rendered
    finish(block, qtbot)
    block._toggle.click()
    assert block._content.toPlainText() == block.text()


def test_resize_during_reveal_remeasures_wrapping_at_same_progress(block, qtbot):
    block.set_text("Long wrapped reasoning text. " * 100)
    block._toggle.click()
    midpoint(block)
    old_height = block._content.height()
    block.parentWidget().resize(320, 600)
    qtbot.waitUntil(lambda: block._content.height() > old_height)
    assert block._clip.height() == pytest.approx((block._content.height() + 6) / 2, abs=1)
    finish(block, qtbot)
    assert block._content.verticalScrollBar().maximum() == 0


@pytest.mark.parametrize("expanded", [True, False])
def test_hiding_parent_settles_motion_and_stops_timers(block, qtbot, expanded):
    block._toggle.click()
    midpoint(block)
    if not expanded:
        block._toggle.click()
    block.parentWidget().hide()
    assert block._animation.state() == QAbstractAnimation.State.Stopped
    assert not block._flush_timer.isActive()
    assert block._content.isHidden() is not expanded
    block.append("Arrived while hidden")
    assert not block._flush_timer.isActive()
    block.parentWidget().show()
    if expanded:
        assert block._content.toPlainText() == block.text()
    else:
        assert block._clip.height() == 0


def test_reduced_motion_settles_without_animation(block, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: False))
    block._toggle.click()
    assert block._animation.state() == QAbstractAnimation.State.Stopped
    assert block._clip.height() == block._content.height() + 6
    block._toggle.click()
    assert block._animation.state() == QAbstractAnimation.State.Stopped
    assert block._content.isHidden()
    assert block._clip.height() == 0


def test_space_key_toggles_thinking(block, qtbot):
    block._toggle.setFocus()
    qtbot.keyClick(block._toggle, Qt.Key.Key_Space)
    midpoint(block)
    finish(block, qtbot)
    qtbot.keyClick(block._toggle, Qt.Key.Key_Space)
    midpoint(block)
    finish(block, qtbot)
    assert block._content.isHidden()


def test_animation_frames_do_not_rebuild_text_or_reflow_document(block):
    block._toggle.click()
    animation = midpoint(block)
    changes = []
    document = block._content.document()
    document.contentsChanged.connect(lambda: changes.append("text"))
    document.documentLayout().documentSizeChanged.connect(lambda _: changes.append("layout"))
    for time in range(animation.duration() // 2, animation.duration(), 10):
        animation.setCurrentTime(time)
        QApplication.processEvents()
    assert changes == []


def test_chat_card_and_body_follow_both_animation_directions(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(820, 650)
    view.show()
    view.load_history([("assistant", "Following answer", "Reasoning\n" * 5, "m1")])
    row = view._rows[0]
    block = row._thinking
    qtbot.wait(250)
    collapsed = row._assistant_panel.height()
    block._toggle.click()
    midpoint(block)
    natural = block._content.height() + 6
    qtbot.waitUntil(
        lambda: abs(row._assistant_panel.height() - collapsed - natural / 2) <= 1
    )
    finish(block, qtbot)
    qtbot.waitUntil(lambda: row._assistant_panel.height() == collapsed + natural)
    block._toggle.click()
    midpoint(block)
    qtbot.waitUntil(
        lambda: abs(row._assistant_panel.height() - collapsed - natural / 2) <= 1
    )
    finish(block, qtbot)
    qtbot.waitUntil(lambda: row._assistant_panel.height() == collapsed)
