# Three-note reminder cue implementation plan

**Goal:** Complete the interrupted request to replace the thin single ding with three crisp notes.

**Architecture:** Keep one bundled WAV and the existing lazy, low-volume player. Synthesize C6–E6–G6 at 0.00/0.20/0.40 seconds, with short separated first notes and a longer final decay (0.72 seconds total). Each note has a fixed pitch, rounded 2 ms attack and decaying upper harmonics; no glide, extra timer, dependency or notification behavior change.

**Tech Stack:** Python standard-library math/struct/wave, pytest; existing Qt player unchanged.

## Steps

1. Update `tests/unit/test_reminder_notification_settings.py`: require 0.72-second mono 48 kHz/16-bit PCM, three separated attacks, stable C6/E6/G6 pitches, low peaks, smooth note boundaries, and deterministic packaged bytes. Run against the old asset and confirm failure.
2. Update `scripts/generate_reminder_sound.py` and regenerate `src/limbowave/ui/assets/reminder-soft.wav`. Preserve low gain and the bundled resource path. Run the sound/settings tests until green.
3. Update `docs/development/reminders.md`; run reminder unit, delivery integration, Qt notification/startup, and packaging regression tests plus focused Ruff. Provide the actual audio file for subjective listening; automated checks do not verify perceived sound quality.

## Scope boundaries

- Preserve unrelated uncommitted work in this shared checkout.
- Do not modify scheduler, reminder delivery, OS settings, production data or installer.
- No new agents, chats, automation, application restart or Git commit.

## Verification results

- Red test run on old single-note WAV: 5 expected failures, 3 passes. Regenerated three-note WAV: all 8 sound/settings tests pass, including deterministic packaged bytes and all three measured pitches/attacks.
- Focused sound/notification/routing/Windows adapter/packager regression: **48 passed**. Ruff and strict mypy for the generator passed.
- Broader reminder/delivery/startup/preferences/packager run: **164 passed, 1 failed**. The failure is the previously recorded unrelated hardcoded font size in `src/limbowave/ui/companion_window.py:476`; left unchanged.
- Broader rerun without theme tests: **123 passed, 1 failed**, exposing an intermittent existing permission test (`test_create_reminder_requires_explicit_approval[once-custom]`). Its last-audit assertion assumes chronological insertion order; memory storage orders equal timestamps by random audit ID. A read-only in-memory probe with tied timestamps reproduces the assertion mismatch while approvals and confirmed audit records remain correct. Permission code/tests were not changed in this sound-only increment.
- No physical speaker listening was performed. The bundled WAV is delivered for subjective preview; restart the source application if its existing QSoundEffect has already cached the old WAV. No installer was rebuilt.

## Continuation verification — 2026-10-27

- Resumed from the linked interrupted chat and confirmed the three-note generator, WAV, tests and development documentation were already saved in the shared checkout. Retained that implementation without changing scheduler, notification or unrelated work.
- Re-ran all reminder unit tests, delivery integration, Qt sound/notifications/routing/Windows adapter/startup/UI/permissions tests, and `tests/unit/test_packager_core.py` with `QT_QPA_PLATFORM=offscreen`: **124 passed in 13.27s**. The previously noted intermittent permission assertion did not reproduce in this run; this is not a claim that its ordering issue was fixed.
- Focused Ruff check passed for the generator, sound/settings tests and Qt sound adapter. Strict mypy passed for the generator.
- The checked-in-location WAV is 69,164 bytes; tests verify 0.72 seconds, three separated fixed-pitch notes, low PCM peaks, smooth boundaries, byte-for-byte regeneration and packaging inclusion.
- No physical listening, application restart, production-data edits or installer rebuild was performed. Subjective acceptance remains with the user through the delivered WAV preview or Settings → 提醒通知 → 试听提示音.
