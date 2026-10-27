"""Small keyboard-accessible voice controls; no audio modules imported at startup."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFocusEvent, QHideEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from limbowave.domain.voice import VoiceSettings
from limbowave.ui import theme


class HoldToTalkButton(QPushButton):
    hold_started = Signal()
    hold_finished = Signal()
    hold_cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("按住说话", parent)
        self._held = False
        self.setAccessibleName("按住说话，松开转写")
        self.setToolTip("按住鼠标或空格录音，松开转写；Esc 取消。最长 60 秒。")
        self.setAutoDefault(False)

    def _start(self) -> None:
        if self._held or not self.isEnabled():
            return
        self._held = True
        self.setDown(True)
        self.hold_started.emit()

    def reset_hold(self) -> None:
        self._held = False
        self.setDown(False)

    def _finish(self, *, cancel: bool = False) -> None:
        if not self._held:
            return
        self.reset_hold()
        (self.hold_cancelled if cancel else self.hold_finished).emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._start()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # A release outside the button must also finish; never leave the mic running.
            self._finish()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self._start()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self._finish(cancel=True)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self._finish()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self._finish(cancel=True)
        super().focusOutEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._finish(cancel=True)
        super().hideEvent(event)


class VoiceControls(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.record = HoldToTalkButton(self)
        self.read = QPushButton("朗读", self)
        self.read.setAccessibleName("朗读最近一条完整回复")
        self.read.setToolTip("朗读最近一条完整回复（AI 生成声音）；不会自动播放")
        self.stop = QPushButton("停止", self)
        self.stop.setAccessibleName("停止语音")
        self.stop.setToolTip("立即停止录音、转写或朗读（Esc）")
        for button in (self.record, self.read, self.stop):
            button.setFixedHeight(30)
            button.setProperty("flat", True)
            button.setAutoDefault(False)
            layout.addWidget(button)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName("语音状态")
        self.status.setStyleSheet(f"background: transparent; font-size: {theme.FS_SMALL}px;")
        self.status.setMinimumWidth(0)
        layout.addWidget(self.status, 1)
        self.status.hide()
        self.stop.hide()
        self.hide()

    def configure(self, settings: VoiceSettings) -> None:
        self.record.setVisible(settings.transcription.enabled)
        self.read.setVisible(settings.speech.enabled)
        self.setVisible(settings.transcription.enabled or settings.speech.enabled)

    def show_state(self, state: str, text: str = "") -> None:
        self.record.setText("松开转写" if state == "recording" else "按住说话")
        self.record.setEnabled(state != "transcribing")
        self.read.setEnabled(state != "recording")
        self.stop.setVisible(state != "idle")
        self.status.setText(text)
        self.status.setVisible(bool(text))
