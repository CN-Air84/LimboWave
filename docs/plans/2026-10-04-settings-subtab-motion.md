# Settings Subtab Motion Implementation Plan

**Goal:** Settings subtab pages slide horizontally in tab order, while primary navigation and list refreshes remain vertical.

**Architecture:** Add a keyword-only Qt orientation to AnimatedPageStack, defaulting to vertical for compatibility. Configure horizontal tab stacks explicitly; keep capture_refresh/animate_refresh vertical and retain the existing fade, timing, and interruption handling.

**Tech Stack:** Python, PySide6, pytest-qt.

## Steps
1. Extend `tests/ui/test_animated_stack.py` to cover both orientations, forward/backward movement, vertical refreshes, and interrupted transitions.
2. Add settings integration coverage for logical-model, endpoint, and all seven appearance subtabs; check memory subtabs too.
3. Update `src/limbowave/ui/animated_stack.py` and horizontal stack construction in `appearance_editor.py`, `logical_models_tab.py`, `settings_panel.py`, and `session_memory_panel.py`.
4. Run focused Qt tests and lint. Do not modify unrelated existing workspace changes.

## Verification
Run `.venv/Scripts/python.exe -m pytest tests/ui/test_animated_stack.py tests/ui/test_settings_panel.py tests/ui/test_backdrop_appearance.py tests/ui/test_memory_panel.py tests/ui/test_settings_dialog.py` with `QT_QPA_PLATFORM=offscreen`.
