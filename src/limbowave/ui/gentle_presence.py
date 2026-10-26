"""Quiet in-app presence UI; never a toast, modal, chat message or reply prompt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from limbowave.application.services.gentle_presence import GentlePresenceService
from limbowave.application.services.preferences_service import PreferencesService
from limbowave.domain.presence import PresenceCue, PresenceSettings
from limbowave.ui import theme

if TYPE_CHECKING:
    from limbowave.ui.main_window import MainWindow

CUE_DURATION_MS = 45_000


class PresenceBar(QWidget):
    """Static, understated indicator with explicit dismiss/mute and no attention animation."""

    mute_requested = Signal()
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("gentlePresenceBar")
        self.setAccessibleName("低压在场，无需回复")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(10)
        self._dot = QLabel("●")
        self._dot.setObjectName("presenceDot")
        self._dot.setAccessibleName("在场标记")
        layout.addWidget(self._dot)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._label, 1)
        self._mute = QPushButton("静音")
        self._mute.setAccessibleName("静音低压在场")
        self._mute.setToolTip("立即静音；可在设置 → 低压在场中恢复，不影响定时提醒")
        self._mute.clicked.connect(self.mute_requested.emit)
        layout.addWidget(self._mute)
        self._dismiss = QPushButton("×")
        self._dismiss.setAccessibleName("收起这次在场问候")
        self._dismiss.setToolTip("收起，不需要回复")
        self._dismiss.clicked.connect(self.dismiss)
        layout.addWidget(self._dismiss)
        self._expiry = QTimer(self)
        self._expiry.setSingleShot(True)
        self._expiry.setInterval(CUE_DURATION_MS)
        self._expiry.timeout.connect(self.clear)
        self.restyle()
        self.hide()

    def restyle(self) -> None:
        self.setStyleSheet(f"""
            #gentlePresenceBar {{ background: transparent; }}
            #gentlePresenceBar QLabel {{ color: {theme.TEXT_SECONDARY};
                background: transparent; font-size: {theme.FS_SMALL}px; }}
            #gentlePresenceBar #presenceDot {{ color: {theme.ACCENT}; }}
            #gentlePresenceBar QPushButton {{ color: {theme.TEXT_SECONDARY};
                background: transparent; padding: 4px 8px; }}
            #gentlePresenceBar QPushButton:hover {{ background: {theme.BG_SURFACE_HOVER}; }}
        """)

    def show_cue(self, cue: PresenceCue, *, mode: str = "greeting") -> None:
        self.restyle()
        self._label.setText(cue.text if mode == "greeting" else "安静在场 · 无需回复")
        self.show()
        self._expiry.start()

    def clear(self) -> None:
        self._expiry.stop()
        self.hide()

    def dismiss(self) -> None:
        self.clear()
        self.dismissed.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        self._expiry.stop()
        super().hideEvent(event)


class PresenceSettingsPage(QWidget):
    """Explicit local opt-in. Persist only this preference, preserving appearance changes."""

    changed = Signal(object)  # PresenceSettings, including a fail-closed runtime override

    def __init__(self, preferences: PreferencesService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preferences = preferences
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        title = QLabel("低压在场")
        title.setStyleSheet(f"font-size: {theme.FS_TITLE}px; font-weight: 600;")
        root.addWidget(title)
        hint = QLabel(
            "只在你明确允许时，安静地在这里。无需回复，也不会产生未读消息。\n"
            "不发系统通知、不播放声音、不抢焦点、不调用模型。只在当前聊天页显示。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        self._enabled = QCheckBox("允许低压在场（默认关闭）")
        self._muted = QCheckBox("静音低压在场，直到我手动恢复")
        root.addWidget(self._enabled)
        root.addWidget(self._muted)
        self._triggers = QWidget()
        form = QFormLayout(self._triggers)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(12)
        self._idle_enabled = QCheckBox("长时间没有交互时")
        self._idle_minutes = QSpinBox()
        self._idle_minutes.setRange(15, 240)
        self._idle_minutes.setSuffix(" 分钟")
        self._idle_minutes.setAccessibleName("在场问候的空闲时长")
        form.addRow(self._idle_enabled, self._idle_minutes)
        self._night_enabled = QCheckBox("在预设的夜间时刻")
        self._night_time = QTimeEdit()
        self._night_time.setDisplayFormat("HH:mm")
        self._night_time.setAccessibleName("在场问候的夜间时刻（本地时间）")
        form.addRow(self._night_enabled, self._night_time)
        self._mode = QComboBox()
        self._mode.addItem("一条安静问候", "greeting")
        self._mode.addItem("仅显示静态在场标记", "indicator")
        form.addRow("展示方式", self._mode)
        root.addWidget(self._triggers)
        note = QLabel(
            "每天最多 2 次，两次至少间隔 4 小时；每次 45 秒后自动收起。\n"
            "输入、点击、纯净模式、模型运行或加载时保持安静；睡眠和错过的夜间时刻不补发。\n"
            "静音只影响低压在场，不影响你明确设置的定时提醒。夜间时刻使用设备本地时间。"
        )
        note.setWordWrap(True)
        root.addWidget(note)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.FS_SMALL}px;")
        self._error.hide()
        root.addWidget(self._error)
        root.addStretch(1)
        self.reload()
        for check in (self._enabled, self._muted, self._idle_enabled, self._night_enabled):
            check.toggled.connect(self._save)
        self._idle_minutes.valueChanged.connect(self._save)
        self._night_time.timeChanged.connect(self._save)
        self._mode.currentIndexChanged.connect(self._save)

    def reload(self, settings: PresenceSettings | None = None) -> None:
        value = settings if settings is not None else self._preferences.load().presence
        controls = (
            self._enabled,
            self._muted,
            self._idle_enabled,
            self._idle_minutes,
            self._night_enabled,
            self._night_time,
            self._mode,
        )
        blockers = [QSignalBlocker(control) for control in controls]
        self._enabled.setChecked(value.enabled)
        self._muted.setChecked(value.muted)
        self._idle_enabled.setChecked(value.idle_enabled)
        self._idle_minutes.setValue(value.idle_minutes)
        self._night_enabled.setChecked(value.night_enabled)
        self._night_time.setTime(QTime.fromString(value.night_time, "HH:mm"))
        self._mode.setCurrentIndex(self._mode.findData(value.mode))
        del blockers
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        self._muted.setEnabled(self._enabled.isChecked() or self._muted.isChecked())
        self._triggers.setEnabled(self._enabled.isChecked() and not self._muted.isChecked())
        self._idle_minutes.setEnabled(self._idle_enabled.isChecked())
        self._night_time.setEnabled(self._night_enabled.isChecked())

    def show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(bool(text))

    def _save(self) -> None:
        settings = PresenceSettings(
            enabled=self._enabled.isChecked(),
            muted=self._muted.isChecked(),
            idle_enabled=self._idle_enabled.isChecked(),
            idle_minutes=self._idle_minutes.value(),
            night_enabled=self._night_enabled.isChecked(),
            night_time=self._night_time.time().toString("HH:mm"),
            mode=str(self._mode.currentData()),
        )
        self._sync_enabled()
        try:
            self._preferences.save(replace(self._preferences.load(), presence=settings))
        except OSError:
            self.changed.emit(replace(settings, enabled=False))
            self.show_error("设置未保存；本次运行已暂停低压在场。请检查磁盘权限或空间后重试。")
            return
        self.show_error("")
        self.changed.emit(settings)


class GentlePresenceController(QObject):
    """Window-scoped interaction tracking and cue lifecycle, with cheap opt-in-only polling."""

    settings_changed = Signal(object)
    error_changed = Signal(str)

    def __init__(
        self,
        window: MainWindow,
        preferences: PreferencesService,
        *,
        service: GentlePresenceService | None = None,
        allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._preferences = preferences
        self._allowed = allowed
        self.service = service or GentlePresenceService(
            preferences.path.with_name("presence_state.json"), preferences.load().presence
        )
        self.error = ""
        self._closed = False
        self._closing = False
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self.poll)
        window.chat.presence_bar.mute_requested.connect(self.mute)
        window.chat.presence_bar.dismissed.connect(self.note_activity)
        window.chat.presence_context_changed.connect(self._context_changed)
        window.destroyed.connect(self.shutdown)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            app.aboutToQuit.connect(self.shutdown)
        self.configure(self.service.settings)

    def configure(self, settings: PresenceSettings) -> None:
        if self._closed:
            return
        self.service.configure(settings)
        self._window.chat.presence_bar.clear()
        self._timer.stop()
        current = self.service.settings
        if (
            not self._closed
            and current.enabled
            and not current.muted
            and not self.service.storage_failed
        ):
            self._timer.start()
        if self.error and not self.service.storage_failed:
            self.error = ""
            self.error_changed.emit("")
        self.settings_changed.emit(self.service.settings)

    def note_activity(self) -> None:
        if self._closed or not isValid(self._window):
            return
        self.service.note_activity()
        self._window.chat.presence_bar.clear()

    def _context_changed(self) -> None:
        if self._closed or not isValid(self._window):
            return
        if not self._window.chat.presence_allowed:
            self.note_activity()

    def poll(self) -> None:
        if self._closed or not isValid(self._window):
            return
        app = QApplication.instance()
        window = self._window
        floating = any(panel.isVisible() for panel in window.findChildren(QWidget, "floatingPanel"))
        blocked = (
            not self._allowed()
            or floating
            or not window.isVisible()
            or not window.isActiveWindow()
            or window.isMinimized()
            or window.showing_full_page
            or window.transition_active
            or not window.chat.isVisible()
            or not window.chat.presence_allowed
            or (
                isinstance(app, QApplication)
                and (app.activeModalWidget() is not None or app.activePopupWidget() is not None)
            )
        )
        if blocked:
            window.chat.presence_bar.clear()
        cue = self.service.poll(blocked=blocked)
        if self.service.storage_failed:
            self._timer.stop()
            self.error = "低压在场已暂停：无法保存限频记录。请检查磁盘权限或空间。"
            self.error_changed.emit(self.error)
        if cue is not None:
            window.chat.presence_bar.show_cue(cue, mode=self.service.settings.mode)

    def mute(self) -> None:
        muted = replace(self.service.settings, muted=True)
        self.configure(muted)  # Mute immediately, even if persistence subsequently fails.
        try:
            self._preferences.save(replace(self._preferences.load(), presence=muted))
        except OSError:
            self.error = "本次运行已静音，但设置未保存。重启前请检查磁盘权限或空间。"
            self.error_changed.emit(self.error)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._closed or not isValid(self._window) or not isinstance(watched, QWidget):
            return False
        if watched is not self._window and not self._window.isAncestorOf(watched):
            return False
        kind = event.type()
        if watched is self._window:
            if kind == QEvent.Type.Close:
                self._closing = True
                QTimer.singleShot(0, self._cancel_rejected_close)
            elif kind == QEvent.Type.Hide and self._closing:
                self.shutdown()  # Hide arrives only after close guards accept the close.
            elif kind in (
                QEvent.Type.Hide,
                QEvent.Type.WindowDeactivate,
                QEvent.Type.WindowActivate,
                QEvent.Type.WindowStateChange,
            ):
                self.note_activity()
        if kind == QEvent.Type.Hide and watched is self._window.chat:
            self.note_activity()
        if kind == QEvent.Type.Show and watched.objectName() == "floatingPanel":
            self.note_activity()
        if kind in (
            QEvent.Type.KeyPress,
            QEvent.Type.InputMethod,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
            QEvent.Type.TouchBegin,
            QEvent.Type.Drop,
        ):
            self.service.note_activity()
            bar = self._window.chat.presence_bar
            # Do not hide a button between press and release, which would eat its click.
            if watched is not bar and not bar.isAncestorOf(watched):
                bar.clear()
        return False

    def _cancel_rejected_close(self) -> None:
        if not self._closed and isValid(self._window) and self._window.isVisible():
            self._closing = False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if isValid(self._timer):
            self._timer.stop()
        if isValid(self._window):
            self._window.chat.presence_bar.clear()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
