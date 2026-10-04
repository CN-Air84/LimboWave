"""Only newly accepted user messages get a paint-only entrance animation."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import ChatView


@pytest.fixture
def view(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> ChatView:
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _effect: True))
    chat = ChatView()
    qtbot.addWidget(chat)
    chat.resize(850, 650)
    chat.show()
    qtbot.waitExposed(chat)
    return chat


def test_new_user_row_animates_without_delaying_reply(view: ChatView, qtbot: QtBot) -> None:
    view.add_user_message("你好，灵波", "u1")
    row = view._rows[-1]
    animation = row._send_animation
    assert animation is not None
    assert animation.state() == QAbstractAnimation.State.Running
    effect = row.graphicsEffect()
    assert effect is not None
    assert effect.property("progress") == 0.0

    view.set_busy(True)
    view.append_assistant_delta("已经开始回复")
    assert view._stream_row is not None
    assert view._stream_row.graphicsEffect() is None
    assert row._send_animation is animation

    qtbot.waitUntil(lambda: row._send_animation is None)
    assert row.graphicsEffect() is None
    assert row._raw_text == "你好，灵波"


@pytest.mark.parametrize("keyboard", [False, True])
def test_button_and_shortcut_use_same_accepted_send_animation(
    view: ChatView, qtbot: QtBot, keyboard: bool
) -> None:
    view.message_submitted.connect(lambda text: view.add_user_message(text, "u1"))
    view._input.setPlainText("发送测试")
    if keyboard:
        qtbot.keyClick(view._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    else:
        qtbot.mouseClick(view._send_btn, Qt.MouseButton.LeftButton)
    assert view._input.toPlainText() == ""
    assert len(view._rows) == 1
    assert view._rows[0]._send_animation is not None


def test_history_never_plays_send_animation(view: ChatView) -> None:
    view.load_history([("user", "旧问题", "", "u1"), ("assistant", "旧回复", "", "a1")])
    assert all(row._send_animation is None for row in view._rows)
    assert all(row.graphicsEffect() is None for row in view._rows)


@pytest.mark.parametrize("hidden", [False, True])
def test_hidden_or_motion_disabled_messages_appear_immediately(
    view: ChatView, monkeypatch: pytest.MonkeyPatch, hidden: bool
) -> None:
    if hidden:
        view.hide()
    else:
        monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _effect: False))
    view.add_user_message("无需动画", "u1")
    assert view._rows[-1]._send_animation is None
    assert view._rows[-1].graphicsEffect() is None


def test_progress_does_not_change_message_geometry_or_scroll_range(
    view: ChatView, qtbot: QtBot
) -> None:
    view.load_history([("user", "\n".join(["history"] * 45), "", "u0")])
    qtbot.waitUntil(lambda: view._dock_anim is None)
    view.add_user_message("自动换行的长消息 " * 80, "u1")
    row = view._rows[-1]
    animation = row._send_animation
    assert animation is not None
    animation.pause()
    QApplication.processEvents()
    geometry = row.geometry()
    bar = view._scroll.verticalScrollBar()
    maximum = bar.maximum()
    for progress in (0.2, 0.5, 0.9):
        animation.setCurrentTime(round(animation.duration() * progress))
        QApplication.processEvents()
        assert row.geometry() == geometry
        assert bar.maximum() == maximum
        assert bar.value() == maximum
    row.cancel_send_animation()
    QApplication.processEvents()
    assert row.geometry() == geometry
    assert row.graphicsEffect() is None


def test_consecutive_messages_animate_independently(view: ChatView) -> None:
    view.add_user_message("第一条", "u1")
    first = view._rows[-1]
    view.add_user_message("第二条", "u2")
    second = view._rows[-1]
    assert first._send_animation is not None
    assert second._send_animation is not None
    assert first._send_animation is not second._send_animation
    first.cancel_send_animation()
    assert first.graphicsEffect() is None
    assert second._send_animation is not None


@pytest.mark.parametrize("operation", ["clear", "history", "hide"])
def test_interrupted_animation_cleans_up(view: ChatView, operation: str) -> None:
    view.add_user_message("即将切走", "u1")
    row = view._rows[-1]
    assert row._send_animation is not None
    if operation == "clear":
        view.clear_transcript()
    elif operation == "history":
        view.load_history([("user", "另一段历史", "", "u2")])
    else:
        view.hide()
    assert row._send_animation is None
    assert row.graphicsEffect() is None


def test_effect_fades_and_lifts_pixels_without_clipping(qtbot: QtBot) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QFrame, QWidget

    from limbowave.ui.message_motion import MessageSendEffect

    host = QWidget()
    qtbot.addWidget(host)
    host.resize(100, 80)
    host.setStyleSheet("background: white;")
    tile = QFrame(host)
    tile.setStyleSheet("background: red;")
    tile.setGeometry(20, 10, 40, 20)
    effect = MessageSendEffect(tile)
    tile.setGraphicsEffect(effect)
    host.show()
    qtbot.waitExposed(host)

    def pixel(x: int, y: int) -> str:
        image = host.grab().toImage()
        scale = image.devicePixelRatio()
        return image.pixelColor(round(x * scale), round(y * scale)).name()

    assert pixel(25, 20) == "#ffffff"
    effect.setProperty("progress", 0.5)
    assert pixel(25, 12) == "#ffffff"
    assert pixel(25, 20) in {"#ff7f7f", "#ff8080"}
    # Lower edge paints beyond the original widget without clipping.
    assert pixel(25, 35) in {"#ff7f7f", "#ff8080"}
    effect.setProperty("progress", 1.0)
    assert pixel(25, 12) == "#ff0000"
    assert pixel(25, 35) == "#ffffff"
    assert tile.geometry() == QRect(20, 10, 40, 20)


def test_attachment_and_text_share_one_animation(view: ChatView) -> None:
    from limbowave.ui.sent_attachments import SentAttachment, SentAttachmentStrip

    view.set_attachment_resolver(lambda _id: [SentAttachment("notes.txt", "Text document")])
    view.add_user_message("请查看附件", "u1")
    row = view._rows[-1]
    strip = row.findChild(SentAttachmentStrip)
    assert strip is not None
    assert row.isAncestorOf(strip)
    assert row._send_animation is not None
    assert row.graphicsEffect() is not None
    row.cancel_send_animation()
    assert row.graphicsEffect() is None
    assert strip.isVisible()


def test_history_crossfade_settles_send_before_animating_parent(
    view: ChatView, qtbot: QtBot
) -> None:
    view.add_user_message("正在发送", "u1")
    row = view._rows[-1]
    assert row._send_animation is not None
    view.transition_history([("user", "另一段历史", "", "u2")])
    assert row._send_animation is None
    assert row.graphicsEffect() is None
    qtbot.waitUntil(lambda: view._history_transition is None)
    assert [item._message_id for item in view._rows] == ["u2"]
    assert view._rows[0].graphicsEffect() is None
