"""Optional attention for committed reminders; never a second scheduler or model run."""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from typing import Protocol

from PySide6.QtCore import QObject, QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

from limbowave.application.services.preferences_service import PreferencesService
from limbowave.domain.reminder_notifications import ReminderNotificationSettings
from limbowave.ui.reminder_sound import SoftReminderSound
from limbowave.ui.windows_reminder_balloons import WindowsReminderBalloons

_LOG = logging.getLogger(__name__)
Target = tuple[str, str, str]


class SoundPlayer(Protocol):
    def play(self) -> None: ...
    def stop(self) -> None: ...


class BalloonBackend(Protocol):
    def show(self, title: str, body: str, on_click: Callable[[], None]) -> bool: ...
    def close(self) -> None: ...


class ReminderNotifications(QObject):
    open_requested = Signal(object)  # immutable (conversation_id, branch_id, message_id)
    settings_changed = Signal(object)
    error_changed = Signal(str)

    def __init__(
        self, window: QWidget, preferences: PreferencesService, *,
        allowed: Callable[[], bool] = lambda: True,
        sound_factory: Callable[[], SoundPlayer] | None = None,
        balloon_factory: Callable[[], BalloonBackend] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__(window)
        self._preferences = preferences
        self.settings = preferences.load().reminder_notifications
        self.error = ""
        self._allowed, self._clock = allowed, clock
        self._sound_factory = sound_factory or (lambda: SoftReminderSound(window))
        self._balloon_factory = balloon_factory or (lambda: WindowsReminderBalloons(window))
        self._sound: SoundPlayer | None = None
        self._balloons: BalloonBackend | None = None
        self._last_sound = float("-inf")
        self._last_attention = float("-inf")
        self._closed = False
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending: dict[str, Target] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._flush)

    def notify(self, conversation_id: str, branch_id: str, message_id: str) -> None:
        if self._closed or not self._allowed() or message_id in self._seen:
            return
        self._seen[message_id] = None
        if len(self._seen) > 512:
            self._seen.popitem(last=False)
        if not self.settings.sound_enabled and not self.settings.desktop_enabled:
            return
        self._pending[message_id] = (conversation_id, branch_id, message_id)
        if not self._timer.isActive():
            self._timer.start(250)

    def _play_sound(self) -> None:
        try:
            if self._sound is None:
                self._sound = self._sound_factory()
            self._sound.play()
        except Exception as exc:
            _LOG.debug("reminder.sound_unavailable", extra={"error_type": type(exc).__name__})

    def preview_sound(self) -> None:
        if not self._closed:
            self._play_sound()  # Explicit user-requested preview works even when muted.

    def _flush(self) -> None:
        if not self._pending:
            return
        if self._closed or not self._allowed():
            self._pending.clear()
            return
        now = self._clock()
        remaining = 5.0 - (now - self._last_attention)
        if remaining > 0:
            self._timer.start(max(1, round(remaining * 1000)))
            return
        pending, self._pending = self._pending, {}
        self._last_attention = now
        if self.settings.sound_enabled and now - self._last_sound >= 5.0:
            self._last_sound = now
            self._play_sound()
        if self.settings.desktop_enabled:
            target = next(reversed(pending.values()))
            count = len(pending)
            body = ("你设置的提醒已到时间。点击回到当初的约定。" if count == 1 else
                    f"{count} 条定时提醒已送达。点击回到最近一条的约定，其余提醒保留在原会话。")
            try:
                if self._balloons is None:
                    self._balloons = self._balloon_factory()
                self._balloons.show(
                    "灵波 · 定时提醒", body, lambda: self._open(target),
                )
            except Exception as exc:
                _LOG.debug("reminder.balloon_unavailable", extra={"error_type": type(exc).__name__})

    def _open(self, target: Target) -> None:
        if not self._closed and self._allowed():
            self.open_requested.emit(target)

    def configure(self, settings: ReminderNotificationSettings) -> None:
        if self._closed or not isinstance(settings, ReminderNotificationSettings):
            return
        self.settings = settings
        if not settings.sound_enabled and self._sound is not None:
            self._sound.stop()
        if not settings.desktop_enabled and self._balloons is not None:
            self._balloons.close()
        if not settings.sound_enabled and not settings.desktop_enabled:
            self._timer.stop()
            self._pending.clear()
        self.error = ""
        try:
            self._preferences.save(replace(
                self._preferences.load(), reminder_notifications=settings,
            ))
        except OSError:
            self.error = "本次设置已生效，但未能保存。重启前请检查磁盘权限或空间。"
        self.settings_changed.emit(settings)
        self.error_changed.emit(self.error)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._pending.clear()
        if self._sound is not None:
            self._sound.stop()
        if self._balloons is not None:
            self._balloons.close()


class ReminderNotificationSettingsPage(QWidget):
    changed = Signal(object)
    preview_requested = Signal()

    def __init__(self, preferences: PreferencesService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        title = QLabel("定时提醒的声音与系统通知")
        title.setObjectName("settingsSectionTitle")
        layout.addWidget(title)
        intro = QLabel("只影响你明确设置的定时提醒，不影响聊天回复和「低压在场」。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.sound = QCheckBox("播放轻柔提示音")
        self.sound.setAccessibleName("定时提醒轻柔提示音")
        self.desktop = QCheckBox("显示 Windows 通知气泡")
        self.desktop.setAccessibleName("定时提醒 Windows 通知气泡")
        layout.addWidget(self.sound)
        preview = QPushButton("试听提示音")
        preview.setAccessibleName("试听定时提醒提示音")
        preview.clicked.connect(self.preview_requested.emit)
        layout.addWidget(preview)
        layout.addWidget(self.desktop)
        note = QLabel(
            "提示音短暂、低音量；多条提醒同时补发时合并提示，不连续响铃。\n"
            "Windows 气泡不显示提醒正文，以免在锁屏或共享屏幕上泄露内容。"
            "点击后回到设定时的上下文；不会打断正在生成的回复。\n"
            "Windows 气泡遵循系统通知设置；提示音由上方开关独立控制。"
            "无音频设备或通知不可用时，提醒仍会保留在会话中。\n"
            "这不是后台服务：应用退出或电脑休眠期间无法准点通知，恢复后补发。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)
        layout.addStretch()
        self.reload(preferences.load().reminder_notifications)
        self.sound.toggled.connect(self._changed)
        self.desktop.toggled.connect(self._changed)

    def reload(self, settings: ReminderNotificationSettings) -> None:
        blockers = [QSignalBlocker(self.sound), QSignalBlocker(self.desktop)]
        self.sound.setChecked(settings.sound_enabled)
        self.desktop.setChecked(settings.desktop_enabled)
        del blockers

    def show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(bool(text))

    def _changed(self) -> None:
        self.changed.emit(ReminderNotificationSettings(
            sound_enabled=self.sound.isChecked(), desktop_enabled=self.desktop.isChecked(),
        ))
