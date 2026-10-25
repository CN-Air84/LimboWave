# Dark Surface Elevation Implementation Plan

**Goal:** Replace the default dark wireframe appearance with tonal surfaces, quiet borders and subtle no-wallpaper ambient light.

**Architecture:** Keep the existing PySide6 layout, wallpaper worker and soft shadows. Centralize neutral container borders in theme helpers, preserve opaque painter tokens and accessible focus states, and paint two static radial gradients in viewport coordinates only when no wallpaper is configured. Custom theme colors and light mode remain supported.

**Tech Stack:** Python, PySide6 QSS/QPainter, pytest-qt.

## Tasks
1. Add regression tests for default palette surfaces, border alpha, theme switching and ambient rendering; confirm failure before implementation.
2. Update default domain/UI colors to #0B0C0E / #111317 / #16181D; use 6% neutral container borders and an 8% hover boundary without weakening focus/drag states.
3. Add a lightweight shared ambient painter for the chat and transcript; no assets, timers, blur jobs or wallpaper changes.
4. Run appearance, chat, popover and sidebar regressions plus lint; render and inspect dark/light/wallpaper previews. Preserve all existing uncommitted changes and do not commit.

## Verification notes
- Added 10 regressions covering surface tokens, composited pixels, subtle native popup outlines, drag focus, palette switching, deterministic gradients and light/wallpaper exclusions.
- Visual QA covers dark empty/populated, narrow, light and wallpaper states; fixed the sidebar's missing styled-background painting discovered in rendered previews.
- Updated the existing material-panel pixel assertion to require a detectable but low-contrast dark edge while preserving the light-theme and external-shadow assertions.

### Final checks
- Focused dark-surface, popup-material, theme-effects and appearance suites: **121 passed**.
- Chat, main-window, toolbar, sidebar, backdrop and cursor-reveal suites: **236 passed, 1 skipped, 1 failed**. The skip is the offscreen system-font check. The failing dynamic-tree/cursor-reveal subprocess also fails with the committed pre-change theme restored in an isolated comparison; it is not resolved in this visual patch.
- The broader full-UI attempt was stopped after additional failures and is not reported as passing.
- Ruff passes on touched Python files; mypy passes on the new ambient painter and theme module. Dark/light, empty/populated, narrow and wallpaper screenshots were visually inspected.
