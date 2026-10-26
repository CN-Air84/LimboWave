import math
import struct
import wave
from dataclasses import replace

import pytest

from limbowave.application.services.preferences_service import Preferences, PreferencesService
from limbowave.domain.reminder_notifications import ReminderNotificationSettings as Settings
from limbowave.ui.reminder_sound import SOUND_PATH


def test_notification_defaults_and_strict_booleans():
    assert Preferences.from_json({}).reminder_notifications == Settings(True, True)
    for value in (None, [], "bad", {"sound_enabled": "false", "desktop_enabled": 0}):
        assert Settings.from_json(value) == Settings(True, True)
    disabled = {"sound_enabled": False, "desktop_enabled": False}
    assert Settings.from_json(disabled) == Settings(False, False)


def test_preference_round_trip_preserves_other_settings(tmp_path):
    service = PreferencesService(tmp_path / "prefs.json")
    initial = replace(Preferences(), font_scale=1.3, font_family="Test Font")
    service.save(replace(initial, reminder_notifications=Settings(False, True)))
    saved = service.load()
    assert saved.font_scale == 1.3 and saved.font_family == "Test Font"
    assert saved.reminder_notifications == Settings(False, True)
    assert saved.presence == initial.presence


def test_bundled_sound_is_short_low_peak_mono_pcm():
    with wave.open(str(SOUND_PATH), "rb") as audio:
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        count = audio.getnframes()
        assert audio.getframerate() == 48000
        assert count / audio.getframerate() == pytest.approx(0.72)
        values = struct.unpack(f"<{count}h", audio.readframes(count))
    assert 0 < max(abs(sample) for sample in values) < 0.2 * 32767
    assert abs(values[0]) < 3 and abs(values[-1]) < 3


def test_wave_reproducible_and_included_in_packager(tmp_path):
    from scripts.generate_reminder_sound import generate
    from scripts.packager import DATA_BUNDLES

    destination = tmp_path / "soft.wav"
    generate(destination)
    assert destination.read_bytes() == SOUND_PATH.read_bytes()
    assert any(SOUND_PATH.is_relative_to(source) for source, _ in DATA_BUNDLES)


@pytest.mark.parametrize(
    ("onset", "duration", "expected_pitch"),
    [(0.0, 0.18, 1046.50), (0.2, 0.18, 1318.51), (0.4, 0.32, 1567.98)],
)
def test_each_note_has_crisp_attack_and_stable_pitch(onset, duration, expected_pitch):
    with wave.open(str(SOUND_PATH), "rb") as audio:
        rate = audio.getframerate()
        count = audio.getnframes()
        samples = struct.unpack(f"<{count}h", audio.readframes(count))

    def frequency(start, end):
        crossings = []
        for index in range(round(start * rate), round(end * rate) - 1):
            left, right = samples[index : index + 2]
            if left <= 0 < right:
                crossings.append(index - left / (right - left))
        assert len(crossings) > 20
        return (len(crossings) - 1) * rate / (crossings[-1] - crossings[0])

    early = frequency(onset + 0.01, onset + 0.05)
    late = frequency(onset + 0.08, onset + 0.12)
    assert early == pytest.approx(expected_pitch, abs=2)
    assert late == pytest.approx(early, abs=2)
    start = round(onset * rate)
    end = round((onset + duration) * rate)
    note = samples[start:end]
    block = round(0.005 * rate)
    first_rms = math.sqrt(sum(sample**2 for sample in note[:block]) / block)
    next_rms = math.sqrt(sum(sample**2 for sample in note[block : 2 * block]) / block)
    # Fast, rounded attack; all three notes are audible and start/end without clicks.
    assert first_rms > next_rms * 0.6
    assert 0.08 * 32767 < max(abs(sample) for sample in note) < 0.16 * 32767
    assert abs(note[0]) < 3 and abs(note[-1]) < 3


def test_cue_has_exactly_three_separated_notes():
    with wave.open(str(SOUND_PATH), "rb") as audio:
        rate = audio.getframerate()
        count = audio.getnframes()
        samples = struct.unpack(f"<{count}h", audio.readframes(count))

    # Each 5 ms block is active inside a note; the 20 ms gaps split exactly 3 notes.
    block = round(0.005 * rate)
    activity = [any(samples[i : i + block]) for i in range(0, count, block)]
    onsets = [i for i, active in enumerate(activity) if active and (i == 0 or not activity[i - 1])]
    assert onsets == [0, 40, 80]
    for start, end in [(0.18, 0.20), (0.38, 0.40)]:
        assert not any(samples[round(start * rate) : round(end * rate)])
