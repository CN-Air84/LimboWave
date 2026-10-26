# Reminder sound and Windows balloon notifications

**Goal:** Add a brief gentle sound and Windows notification balloons to committed reminders, with independent persistent switches and safe click-to-open behavior.

**Architecture:** Keep scheduling/persistence unchanged. A Qt presentation controller coalesces delivery events and invokes lazy sound and Windows-native notification adapters. Windows balloons use `NIIF_NOSOUND` so the custom soft tone never competes with a default OS alert. OS notifications contain no private reminder text. Each balloon has its own bounded icon ID/click target to avoid opening the wrong conversation after a newer reminder arrives.

## Decisions

- Use a bundled, locally synthesized 0.32-second PCM WAV and `QSoundEffect`, not an unpredictable default beep. Missing audio/backend degrades silently without affecting delivery.
- Use `Shell_NotifyIconW` rather than Qt's generic `showMessage`, because the latter does not expose suppression of the Windows default sound. Respect Windows quiet-time policy; never bypass system notification settings.
- Coalesce arrivals over 250 ms; throttle audible alerts to one every 5 seconds. Grouped balloons open the latest reminder through the existing history-preview path, which does not interrupt active runs.
- Default both features on for explicit reminders; expose independent controls in Settings → 提醒通知. This does not affect the opt-in low-pressure presence feature.
- On mute/reset/exit stop pending sounds and remove tray icons. No OS service, wake-on-sleep, autostart registration, browser sound, or production vault changes.

## Implementation / verification

1. Add preference domain defaults and strict parsing; test legacy files, corrupt booleans and unrelated-preference preservation.
2. Add deterministic WAV generator/asset, lazy sound player, and Windows API adapter with silent flags and correct cleanup/click routing. Unit-test using fake APIs, without displaying real notifications or playing sound during tests.
3. Add coalescing notifier and settings page; wire the existing reminder event, notification-click history route, and lifecycle. Test mute, burst handling, unavailable devices and cleanup.
4. Run reminder, preferences, UI/startup, packaging regressions and static checks; inspect settings rendering; document user-facing platform limitations.

## Verification results

- Reminder and notification-focused suite: **90 passed**, including raw Windows callback routing/ABI/flags, sound loading/mute/expiry, preference persistence, actual Qt/qasync startup/shutdown, and notification clicks during a live model run.
- Broader settings/preferences/packaging/reminder regression: **193 passed**; 3 unrelated existing failures in tab hover rendering and the companion window's hardcoded font-size audit. The hover tests do not create the new notification page (`preferences=None`). Unrelated UI work was left untouched.
- Ruff passed for all files changed in this increment. Full-source mypy passed for **227 source files**; the four new modules also pass a focused mypy check.
- Inspected a rendered Settings → 提醒通知 screenshot for layout.
- Performed one real Windows smoke test (no private text): `Shell_NotifyIconW` accepted the notification, `QSoundEffect` reached `Ready`, the pending playback request was consumed, and all test icons were removed. This verifies API acceptance/audio loading, not whether OS policy displayed the banner or the user's speakers were audible.
- Sound asset is deterministic and included by the existing PyInstaller asset directory rule. No install package was rebuilt and no OS autostart/background service was registered.
