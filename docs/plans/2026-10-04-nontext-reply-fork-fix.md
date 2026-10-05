# Non-text Reply Fork Fix

**Goal:** Make Fork work for completed replies containing tool steps or thinking but no body text, without changing the existing Fork/regenerate behavior.

**Architecture:** Persist a reply when any supported visible content exists (text, thinking, or tool steps). Keep the emitted application message ID and run linkage; reuse existing inclusive branch/runtime restoration and failure validation. Truly empty runs must not produce blank messages.

**Tech Stack:** Python, PySide6, SQLite, pytest/pytest-qt.

1. Add dedicated non-text reply tests with memory and SQLite repositories. Reproduce the missing persisted assistant ID, then check Fork, runtime restoration, regeneration, and incomplete/empty boundaries.
2. Extend the finalization content guard in src/limbowave/application/services/run_coordinator.py; preserve all existing uncommitted work.
3. Add app-wired button tests for tool-only and thinking-only replies; verify branch creation, content after reload, and no new model request.
4. Run targeted tests, related unit/UI tests, Ruff, and mypy. Report environment-related failures separately.

No migration or repair of historical missing messages is included: those IDs were not persisted.

## Verification

- Before the fix: 12 non-text regression cases failed and 2 truly-empty cases passed.
- After the fix: all 14 new unit cases and 2 app-wired UI cases pass.
- Related coordinator/branch/retry/SQLite unit selection: 113 passed.
- Retry UI: 24 passed; branch live reload UI: 2 passed. Ruff on the changed coordinator and new tests, and git diff --check: passed.
- Full unit suite: 1,141 passed, 6 skipped, 1 unrelated shell-executor failure (test_real_child_process_output_decoded; reproduced in isolation).
- Chat view UI: 73 assertions passed, but the process exits with Windows access violation 0xC0000005 during teardown; the pre-fix baseline also showed this exit failure.
- Mypy reports 4 errors at existing, untouched code: QImage.save format type, optional QListWidgetItem, duplicate _compose_prompt, and optional send_message prompt.
- Existing uncommitted Fork/regenerate changes were preserved; the only coordinator change in this task is the non-text finalization guard and its explanatory comments.
