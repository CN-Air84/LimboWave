# Cursor Reveal Highlight Implementation Plan

**Goal:** Add Win10-style pointer-following radial light to interactive surfaces without replacing the cursor or interfering with input.

**Architecture:** A root-scoped Qt event filter enables hover events on existing and dynamically created controls. Lazily allocated, mouse-transparent paint layers render a bounded radial glow and illuminated border, clipped to the control or hovered item; idle layers do not tick. Keep the existing click ripples and theme materials independent.

**Tech Stack:** Python, PySide6 QWidget/QPainter/QRadialGradient/QVariantAnimation, pytest-qt.

## Design

- Prefer local Reveal lighting over a global cursor halo (distracts from text) or a hard cursor ring (unlike the requested tile effect).
- Buttons, selection/input controls and enabled item-view rows participate. Blank space, disabled controls, checkboxes/radio buttons and explicit theme/reveal opt-outs do not.
- About 192 logical pixels in diameter; soft center-to-transparent falloff, subtle border light, short enter/leave fades. Adapt color to light/dark themes; preserve text contrast and focus indicators.
- Respect Qt's disabled general UI animations with an immediate transition. No stylesheet rewriting, background blur recomputation, external dependencies, cursor replacement or full-window animation loop.
- Hide stale light on scroll, resize, hide and window deactivation. Input, scrolling and existing click effects must still work.

## Tasks

1. Add failing tests in `tests/ui/test_cursor_reveal.py`: tracking, clipping, dynamic installation, nested inputs, opt-outs, nonblocking input, fades, lifecycle and live palette changes.
2. Implement `src/limbowave/ui/cursor_reveal.py`; install alongside click ripples on startup and theme refresh in `src/limbowave/app.py`.
3. Run focused and adjacent UI regressions, Ruff and module mypy; render and inspect dark/light samples and verify at high DPI. Document results here.

Preserve all pre-existing changes; no unrelated refactors or commits.

## Results

- Implemented bounded radial surface/border lighting, live theme color lookup, interruptible enter/leave fades, reduced-motion behavior and transparent input passthrough.
- Added root-scoped dynamic installation and cleanup for scroll/model mutations, disable, geometry changes, hide, deactivation and destruction. A weak root reference prevents a Python/Qt lifetime cycle; garbage-collection regression coverage verifies removal of the global filter.
- 65 related tests passed: cursor reveal, click ripple, theme effects, main window and startup responsiveness.
- All 17 new tests passed again at 150% scaling, including pixel clipping, native pointer movement (allowing fractional-DPI rounding), animation reversal, dynamic/nested controls and lifecycle checks.
- Ruff passed for the new module/tests and app.py; strict mypy passed for the new module.
- Rendered and inspected dark/light control and row samples in ignored `dist/cursor-reveal/`. Offscreen Qt needed `QT_QPA_FONTDIR=C:\Windows\Fonts` for readable previews.
- Extended offscreen checks exposed two unrelated existing assertions: `test_unchecked_and_checked_both_draw_a_visible_box` (checkbox edge pixel) and `test_login_page_types_then_reveals_password` (Segoe UI vs Segoe UI Symbol). Both reproduce without the new reveal tests; no unrelated fixes applied. The targeted 65-test run and 150% run exited cleanly.
