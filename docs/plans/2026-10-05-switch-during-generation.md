# Switch During Generation Implementation Plan

**Goal:** Allow browsing another conversation/branch while the active model reply continues, without changing the running kernel's context.

**Architecture:** Keep the live ChatView mounted in a stacked host; use a separate read-only history view for navigation during a run. Sidebar selection follows the displayed scope, not the runtime scope. Returning to the running branch restores the original widgets; after settlement, activate the currently displayed history through the existing safe kernel switch.

**Tech Stack:** Python, PySide6, asyncio, pytest-qt.

## Steps
1. Add regression tests in tests/ui/test_session_switch_during_generation.py: navigate while streaming, preserve the live widgets/draft/stop action, isolate background deltas, retain sidebar selection, activate the viewed conversation after settlement, and handle completion during history loading.
2. Run the tests before implementation and confirm the current busy guard rejects navigation.
3. Add read-only history presentation in src/limbowave/ui/main_window.py and src/limbowave/ui/chat_view.py. Disable mutation controls but retain scrolling/copying/history paging.
4. Update src/limbowave/app.py navigation and sidebar scope only; preserve the RunCoordinator busy guard and runtime isolation. Never abort a run to navigate.
5. Run focused UI/runtime isolation tests, lint/type checks and the broader UI suite. Preserve unrelated working-tree edits.

## Verification
- Confirmed the original regression: both initial navigation tests failed at the busy guard before the patch.
- Final focused suite: 37 passed, exit 0 (11 new navigation/read-only/race/layout cases plus controller, isolation and branch-path coverage).
- Earlier broader focused UI/controller suite: 130 passed.
- Full UI run: 841 passed, 5 failed, 1 skipped; the Qt process crashed during exit. The five failures concern checkbox rendering, diagnostics log locking, login font/transition rendering and the standalone high-DPI snapshot function. These names were also in the pre-existing last-failed cache; none exercise conversation navigation.
- Ruff passed for all four edited/new Python files; git diff --check passed.
- Mypy reports the pre-existing QImage.save(buffer, "PNG") overload issue in chat_view.py:1412 (also present in HEAD); no new typing diagnostics.
- Visually inspected a styled, Chinese-font offscreen capture at .var/switch-during-generation.png; history selection, read-only explanation and layout are visible without the live reply leaking into the preview.

## Scope
This preserves the single-kernel execution model. Other conversations can be viewed during generation; sending in them waits for the current run to finish. No abort, parallel kernel launch or change to RunCoordinator's context-isolation guard is used.
