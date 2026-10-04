# Middle-click Auto-scroll Implementation Plan

**Goal:** Support middle-click auto-scrolling throughout existing scrollable pages without changing wheel/touchpad behavior.

**Architecture:** Extend the already-installed application-level `SmoothScrollFilter`, reusing its scroll-area discovery and animation cancellation. A timer polls the global cursor relative to a fixed origin and updates eligible scrollbars; a small, mouse-transparent viewport marker shows the origin. No new dependency or per-page wiring.

**Tech Stack:** Python, PySide6, pytest-qt.

## Design

- Prefer a shared event filter over chat-only handlers or replacing every scroll widget: it also covers settings, lists and nested message content.
- Middle-click toggles auto-scroll. A small dead zone keeps the page still near the origin; speed grows with distance and is capped. Support both eligible axes, including outer-page vertical scrolling over embedded text browsers.
- A subsequent mouse press or Escape exits; wheel input exits and continues normally. Consume cancelling mouse press/release pairs to prevent accidental button actions or text pastes.
- Stop on target hide/close, window deactivation, destruction or disabling; restore the cursor, dispose of the marker and stop the idle timer.
- Preserve wheel animation, native touchpad/control-wheel handling, scrollbar policies and the existing opt-out property. Cancel pending wheel animation before auto-scroll starts.

## Tasks

1. Add failing UI tests in `tests/ui/test_smooth_scroll.py` for activation/release, speed/dead zone, both directions and axes, nested content, stop gestures, lifecycle, opt-outs and wheel coexistence.
2. Implement the controller and origin marker in `src/limbowave/ui/smooth_scroll.py`; reuse the current installation entry point.
3. Run focused tests and adjacent/full UI regression tests as practical, Ruff and module mypy. Render a marker sample for visual verification.

Preserve all pre-existing changes. No unrelated refactors, commits or packaging changes.

## Verification (2026-10-04)

- `QT_QPA_PLATFORM=offscreen python -m pytest tests/ui/test_smooth_scroll.py -q`: **33 passed**, including real application event dispatch from child widgets, timer polling after release, keyboard shortcut reservation, low-speed fractional movement at both boundaries, nested axes, safe teardown and theme contrast.
- Repeated the same 33 tests with `QT_SCALE_FACTOR=2`: **33 passed**, exit code 0.
- Ruff and strict module mypy: **passed**.
- Rendered and inspected vertical/horizontal/two-axis origin markers in light and dark themes at `.var/probes/middle-scroll-preview.png`. Uses application theme tokens instead of a stylesheet-modified transparent Qt palette.
- Adjacent run (scroll, chat, settings, cursor reveal): **161 assertions/tests passed**, but the process exited with Windows access violation `0xC0000005` during shutdown. A separate baseline with the new middle-click event handler disabled and the existing chat/settings/reveal suites also passed its tests before exiting with the same access violation; this is not counted as a clean suite pass.
- Earlier full UI run: **719 passed, 4 failed, 1 skipped**. Failures were checkbox pixel rendering, data-reset settings construction, marquee font overflow, and startup recovery focus. Isolated rerun with the new handler disabled passed the last three, while checkbox rendering still failed; offscreen Qt also reports missing fonts. No unrelated source changes were made to hide those failures.
- Fixed two regressions discovered during feature testing: pending wheel animation no longer touches a destroyed scrollbar, and stopped auto-scroll state is released on the next event-loop turn so a viewport geometry event cannot destruct its own containing page mid-dispatch.
