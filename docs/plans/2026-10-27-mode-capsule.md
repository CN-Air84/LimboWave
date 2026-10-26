# Companion / Zen Capsule Implementation Plan

**Goal:** Complete the interrupted request: top-right `(bubble | book)` reveals `(迷你陪伴 | 倾诉模式)` with independent actions.

**Architecture:** Refactor the existing overlay in `src/limbowave/ui/zen_button.py` into one animated capsule containing two native, keyboard-accessible buttons. ChatView emits companion intent; MainWindow routes live/history surfaces to the existing CompanionMode. Keep drafts, attachments, permission guards and shortcut handling unchanged. Work in the current checkout because the prerequisite features are uncommitted; preserve unrelated edits.

**Tech Stack:** Python / PySide6, QPainter, QVariantAnimation, pytest-qt.

## 1. Specify and test the shared capsule
- Extend `tests/ui/test_zen_button.py` for both hit targets, shared hover/focus animation, reversal, font scaling, motion-disabled behavior, loading-mask stacking and fixed divider (expanded capsule ends at the right edge).
- Add `tests/ui/test_mode_capsule.py` integration tests: live/history entry, returning from mini preserves Zen state, no sidebar duplicate, permission/full-page guards.
- Run new tests before implementation; expect missing capsule attributes.

## 2. Implement and wire
- Refactor `src/limbowave/ui/zen_button.py`: shared rounded surface and divider; bubble/book paths fade to four-character captions. Use theme colors and reversible 220 ms motion. Both halves expand together, without adding a header or relaying out messages.
- Update `src/limbowave/ui/chat_view.py`: host/position the capsule, retain the right `_zen_btn` for the existing toggle, expose `companion_requested` and the left `_companion_btn`.
- Update `src/limbowave/ui/main_window.py`: connect live and newly created history views to `CompanionMode.open`.
- Update `src/limbowave/ui/companion_mode.py`: remove sidebar-only button construction; keep existing actions and guard paths.

## 3. Verify and document
- Update old entry assertions and `tests/ui/test_companion_startup.py` to exercise the capsule under production QSS/qasync.
- Run capsule, Zen, companion, attachment, history loading, main-window and shutdown regressions with `QT_QPA_PLATFORM=offscreen`; scoped Ruff/mypy.
- Render light/dark collapsed/expanded/checked states for visual QA, including narrow width and enlarged fonts.
- Update `README.md` and `docs/development/companion-mode.md` with the new entry. Do not package or restart the user's running app.

## Implementation notes

- Kept the divider stationary instead of fixing the outer right edge throughout expansion: otherwise the growing right half would steal the original bubble hit target. Real-pointer tests cover both halves and crossing the divider.
- Updated the footprint workspace layout regression because the sidebar no longer contains the companion action; it now verifies the capsule remains reachable instead.
- Small caption text uses the primary text color for readability; the checked book and segment tint communicate the Zen state.
- Initial transcript top spacing clears the capsule; it remains scrollable content padding rather than a fixed header.

## Completion — 2026-10-27

- Completed the paired bubble/book capsule, independent actions, live/history wiring, sidebar removal, keyboard/reduced-motion behavior and documentation.
- Final related regression: **193 passed in 219.38s** (`.tmp/capsule-final-20261027.log`). Earlier 46/185-case runs are overlapping intermediate checks, not additive totals.
- Ruff passed for all 9 touched Python source/test files. Scoped mypy passed for `zen_button.py`, `chat_view.py`, `main_window.py`, `companion_mode.py`. `git diff --check` passed (only existing CRLF normalization warnings).
- Rendered and visually checked production-QSS light/dark compact, expanded and checked states, plus enlarged font layouts. Screenshots: `.tmp/capsule-qa/capsule-states.png` and per-state images. The offscreen render harness explicitly loads Windows fonts; no screenshot helper is shipped.
- Preserved unrelated work in the shared checkout. No package rebuild, user-app restart, new model requests or backend changes.
