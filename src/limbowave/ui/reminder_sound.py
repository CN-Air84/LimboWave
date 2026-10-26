"""Lazy, low-volume cue. Never falls back to the Windows default error/beep sound."""
from __future__ import annotations

import logging
from pathlib import Path
from time import monotonic
from typing import Any

from PySide6.QtCore import QObject, QUrl

SOUND_PATH = Path(__file__).with_name("assets") / "reminder-soft.wav"
_LOG = logging.getLogger(__name__)


class SoftReminderSound:
    def __init__(self, parent: QObject) -> None:
        self._parent = parent
        self._effect: Any = None
        self._pending_until = 0.0

    def play(self) -> None:
        try:
            if self._effect is None:
                # Keep multimedia loading off the application startup path.
                from PySide6.QtMultimedia import QSoundEffect

                effect = QSoundEffect(self._parent)
                effect.setLoopCount(1)
                effect.setVolume(0.4)
                self._effect = effect
                effect.statusChanged.connect(self._status_changed)
                effect.setSource(QUrl.fromLocalFile(str(SOUND_PATH)))
            self._pending_until = monotonic() + 2.0
            self._status_changed()
        except (ImportError, OSError, RuntimeError) as exc:
            self._pending_until = 0.0
            _LOG.debug("reminder.sound_unavailable", extra={"error_type": type(exc).__name__})

    def _status_changed(self) -> None:
        effect = self._effect
        if effect is None:
            return
        if effect.status() == effect.Status.Ready and self._pending_until:
            pending, self._pending_until = self._pending_until, 0.0
            if monotonic() <= pending:
                effect.play()
        elif effect.status() == effect.Status.Error:
            self._pending_until = 0.0
            _LOG.debug("reminder.sound_unavailable")

    def stop(self) -> None:
        self._pending_until = 0.0
        if self._effect is not None:
            self._effect.stop()
