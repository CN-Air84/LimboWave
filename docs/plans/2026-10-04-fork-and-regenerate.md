# Fork and Regenerate Implementation Plan

**Goal:** Fork preserves the selected assistant response and its preceding context without generating; a separate adjacent action regenerates replies.

**Architecture:** Persist an explicit inclusive fork boundary (legacy edit/regenerate branches stay exclusive). Restore the selected completed run's runtime prefix instead of using Pi's user-message-only fork. Keep regenerate on the existing route and expose separate UI/controller signals. Capture completion-time branch-memory snapshots for inclusive forks.

**Tech Stack:** Python, PySide6, SQLite, pytest/pytest-qt.

### Task 1: Regression tests
- Add tests in tests/unit/test_fork_message.py for inclusive history, no send, correct runtime leaf, continuation, nested branches, and old-branch isolation.
- Update tests/ui/test_chat_view.py for distinct adjacent Fork/regenerate buttons and streaming behavior.
- Run targeted tests and confirm new expectations fail first.

### Task 2: Inclusive persisted branch boundary
- Extend src/limbowave/domain/conversation.py, infrastructure/database/migrations.py and sqlite_repositories.py with include_fork_message (default false for compatibility).
- Update application/branch_path.py history slicing and runtime-leaf fallback for completed-run anchors.
- Verify migration and encrypted repository round trips.

### Task 3: Dedicated fork operation
- Add fork_message to application/services/run_coordinator.py and session_controller.py.
- Validate active-branch membership, completed reply, available runtime, and capabilities; hold the runtime transition across finalization and restore.
- Restore only the selected run prefix, commit the new branch, publish BRANCHED without send; recover old runtime on failure.
- Save assistant-time memory snapshots in run finalization, with conservative legacy fallback.

### Task 4: UI integration and verification
- Split signals in ui/chat_view.py and app.py; reuse the retry icon for a separately labelled regenerate button next to Fork.
- Keep streaming/failed replies from exposing completed-reply actions.
- Run targeted and broader unit/UI suites, Ruff, and mypy; inspect a rendered action row when feasible.

**Commands:** .venv/Scripts/python.exe -m pytest tests/unit/test_branch_path.py tests/unit/test_retry_in_place.py tests/unit/test_sqlite_backend.py tests/ui/test_chat_view.py tests/ui/test_retry_ui.py tests/ui/test_branch_live_reload.py; .venv/Scripts/python.exe -m ruff check <changed Python files>; .venv/Scripts/python.exe -m mypy.

## Verification results

- Targeted assertions: 215 passed (116 unit, 73 chat UI, 24 retry UI, 2 branch UI).
- Unit, retry UI, and branch UI processes exit cleanly. The full chat-view module reports all assertions passing but crashes during Qt process shutdown; the unmodified HEAD chat-view source and its original 72 tests reproduce the identical Windows exit code 0xC0000005 in isolation.
- Ruff on all changed Python files and git diff --check: pass.
- Full unit/UI run: 1946 passed, 7 skipped, 7 failed. Failures are in shell child-output, blackout rendering, checkbox rendering, log-lock smoke, login animation, and high-DPI transition tests. No Fork/regenerate regression assertion failed.
- Mypy: 4 existing errors (duplicate _compose_prompt, optional prompt passed to send_message, QImage.save argument type, optional QListWidgetItem). Corresponding unchanged lines were checked against HEAD.
- Offscreen visual inspection confirms the copy, Fork, and regenerate icons form one row with consistent 28px targets and 2px gaps; the offscreen environment lacks Chinese fonts.
