# Assistant Background Restore Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore the missing AI reply background without undoing the centered reading column or other work in progress.

**Architecture:** Reuse the existing assistantMessagePanel, theme.card_surface(), and bubble radius/padding tokens. Keep the panel borderless and its children transparent so text, thinking, tools, and actions share one background per run. Preserve the current workspace and unrelated uncommitted changes.

**Tech Stack:** Python, PySide6, pytest-qt.

---

### Task 1: Update regression coverage
- Modify `tests/ui/test_chat_view.py`: require the rounded background and 16px horizontal / 10px vertical padding during streaming; keep finalized body/actions aligned inside the panel at wide and narrow widths.
- Modify `tests/unit/test_theme_and_preferences.py`: require card color, text color, and 18px radius in dark/light palettes; cover wallpaper material enabled/disabled.
- Modify `tests/ui/test_dark_surfaces.py`: verify actual rendered card pixels and live palette/material refresh rather than only QSS strings.
- Run focused tests offscreen and verify they fail before the production change.

### Task 2: Restore the panel
- Modify `src/limbowave/ui/theme.py`: use `card_surface()` and `RADIUS_BUBBLE` for assistant replies, retaining `border: none`.
- Modify `src/limbowave/ui/chat_view.py`: restore `_BUBBLE_PAD_X` / `_BUBBLE_PAD_Y` on the existing assistant layout and update documentation strings.
- Modify `docs/development/appearance.md`: document the restored theme-aware reply panel.
- Do not alter run grouping, streaming, tool state, Markdown rendering, or composer/column geometry.

### Task 3: Verify and deliver
- Run focused chat/theme tests, then adjacent history, tool, Markdown, backdrop, and action tests.
- Run Ruff on the touched Python files.
- Render local offscreen dark/light/narrow previews and inspect them for padding, clipping, and visible background.
- Record results; do not commit or package unrelated work.

## Verification results
- Confirmed all 14 targeted expectations fail against the transparent/unpadded panel and pass after the fix.
- Core chat, theme/preferences, rendered surfaces, and Markdown regression run: **174 passed**.
- Adjacent suites in isolated processes: history 20, run state 2, nontext fork 3, tool steps 24, tool timeline 5, backdrop appearance 20, backdrop resize 7 passed. Tool visibility animation passed all 14 on retry, for **269 passing tests** across the final focused/isolated runs.
- The initial combined run terminated in a native Qt access violation in `test_details_animate_both_directions`. The first isolated animation run had one endpoint-height failure; restoring the previous transparent/unpadded panel in memory also reproduced an interpolation failure in that parametrized test family. No animation code or assertions were changed; its final retry passed.
- Ruff passed for all five touched Python files. No full repository test run, release build, or commit was performed.
- Inspected offscreen previews in dark, light, wallpaper, and narrow layouts under `.tmp/assistant-background-preview/`. The wallpaper preview waited for the real background frame to become active (not a fallback). Full-width panel: 800px; body: 768px; padding: 16/10/16/10px. Corners, contrast, and action alignment are correct.
