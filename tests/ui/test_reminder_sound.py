import sys
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from limbowave.ui.reminder_sound import SoftReminderSound


class Effect(QObject):
    statusChanged = Signal()
    Status = SimpleNamespace(Ready=1, Loading=0, Error=2)
    def __init__(self, parent):
        super().__init__(parent)
        self.state = 0
        self.plays = self.stops = 0
    def setLoopCount(self, value):
        assert value == 1
    def setVolume(self, value):
        assert 0 < value <= 0.4
    def setSource(self, url):
        assert url.isLocalFile() and url.fileName() == "reminder-soft.wav"
    def status(self):
        return self.state
    def play(self):
        self.plays += 1
    def stop(self):
        self.stops += 1


def test_lazy_sound_ready_mute_and_expiry(qapp, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("limbowave.ui.reminder_sound.monotonic", lambda: now[0])
    monkeypatch.setitem(sys.modules, "PySide6.QtMultimedia", SimpleNamespace(QSoundEffect=Effect))
    sound = SoftReminderSound(qapp)
    assert sound._effect is None
    sound.play()
    effect = sound._effect
    assert effect.plays == 0
    effect.state = effect.Status.Ready
    effect.statusChanged.emit()
    effect.statusChanged.emit()
    assert effect.plays == 1
    effect.state = effect.Status.Loading
    sound.play()
    sound.stop()
    effect.state = effect.Status.Ready
    effect.statusChanged.emit()
    assert effect.plays == 1
    effect.state = effect.Status.Loading
    sound.play()
    now[0] = 10.0
    effect.state = effect.Status.Ready
    effect.statusChanged.emit()
    assert effect.plays == 1
    effect.deleteLater()


def test_audio_error_drops_pending_play(qapp, monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6.QtMultimedia", SimpleNamespace(QSoundEffect=Effect))
    sound = SoftReminderSound(qapp)
    sound.play()
    effect = sound._effect
    effect.state = effect.Status.Error
    effect.statusChanged.emit()
    assert sound._pending_until == 0
    effect.state = effect.Status.Ready
    effect.statusChanged.emit()
    assert effect.plays == 0
    effect.deleteLater()
