"""Generate an original, crisp three-note reminder cue; no external sound assets."""

import math
import struct
import wave
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "src/limbowave/ui/assets/reminder-soft.wav"
SAMPLE_RATE = 48000
# Onset, fixed pitch (C6 / E6 / G6), duration, gain. The last note rings longer.
NOTES = (
    (0.00, 1046.50, 0.18, 0.16),
    (0.20, 1318.51, 0.18, 0.15),
    (0.40, 1567.98, 0.32, 0.14),
)


def generate(path: Path = OUTPUT) -> None:
    duration = max(onset + length for onset, _, length, _ in NOTES)
    samples = [0.0] * round(SAMPLE_RATE * duration)
    for onset, frequency, length, gain in NOTES:
        offset = round(SAMPLE_RATE * onset)
        for i in range(round(SAMPLE_RATE * length)):
            t = i / SAMPLE_RATE
            # Rounded 2 ms attack and brief upper partials, without a pitch glide.
            attack = math.sin(min(1.0, t / 0.002) * math.pi / 2) ** 2
            release = min(1.0, (length - t) / 0.025) ** 2
            phase = 2 * math.pi * frequency * t
            tone = (
                0.74 * math.exp(-t / 0.075) * math.sin(phase)
                + 0.20 * math.exp(-t / 0.032) * math.sin(2 * phase)
                + 0.06 * math.exp(-t / 0.014) * math.sin(3 * phase)
            )
            samples[offset + i] += gain * attack * release * tone
    pcm = [round(value * 32767) for value in samples]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))


if __name__ == "__main__":
    generate()
