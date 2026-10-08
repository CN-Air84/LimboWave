# OOBE Page Transitions Implementation Plan

**Goal:** Animate all OOBE screen changes without delaying navigation or connection checks.

**Architecture:** Reuse `AnimatedPageStack.capture_refresh()` / `animate_refresh()` around a single page containing the complete OOBE column. Capture before changing text, forms and actions, then animate the outgoing snapshot and updated live content. Use the existing 160 ms fade-out and 230 ms fade/slide-in, reversing direction on backward navigation. Keep the rail and background stationary, preserve current page sizing, and skip animation during initialization or hidden updates.

**Tech Stack:** Python, PySide6, pytest-qt.

---

### Task 1: Regression tests
- Modify `tests/ui/test_oobe_demo.py`.
- Cover whole-screen transitions, direction, shared empty-stack screens, same-screen refreshes, rapid navigation and hide/reopen cleanup.
- Run `.venv/Scripts/python.exe -m pytest tests/ui/test_oobe_demo.py -q` and confirm the new tests fail before implementation.

### Task 2: OOBE integration
- Modify `src/limbowave/ui/oobe_demo.py` only; do not change the shared settings animation engine.
- Wrap the complete column in `AnimatedPageStack` using a left-aligned content page with the existing width constraints.
- Capture before screen updates; flush relevant layout changes before starting the refresh animation.
- Keep synchronous screen state, button handlers, check tokens, timers and input focus semantics unchanged.

### Task 3: Verification
- Run OOBE demo/restart, onboarding and shared settings animation regression tests.
- Run Ruff and scoped mypy checks; inspect rendered transition frames and final layouts with offscreen Qt.
- Preserve all unrelated uncommitted work; do not commit as part of this request.

## Verification results
- Added 11 regression cases; the original 17 OOBE tests remain passing.
- 90 tests passed across OOBE navigation/restart, onboarding and settings-page regressions.
- Ruff lint/format checks and scoped strict mypy passed.
- Inspected dark/light outgoing, incoming and final frames. Animation hosts retain transparent backgrounds, preserving the stage glow.
- Verified mouse hit-testing through outgoing snapshots, input focus during resizing, rapid navigation, and hide/reopen cleanup.
