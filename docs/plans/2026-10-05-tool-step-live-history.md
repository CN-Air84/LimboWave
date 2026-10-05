# Tool step live/history consistency implementation plan

**Goal:** Show tool arguments/results/timing immediately and preserve the live order after reload.

**Architecture:** Emit the coordinator's authoritative ToolStep on each tool event. Persist encrypted assistant segments (text, thinking, tool-call IDs) beside the existing final message and audit steps, retaining message/run IDs and fork semantics. Render those same segments on reload; legacy records remain readable. Update existing chips in place so open details stay open.

**Tech Stack:** Python, PySide6, SQLite, pytest/pytest-qt.

### 1. Reproduce
- Add coordinator tests for full live tool records, multi-message rounds, partial/interrupted turns and SQLite reopen.
- Add real app-wiring UI tests for immediate details and live/history timeline equality.
- Run the new tests and confirm the existing failures.

### 2. Repair data flow and persistence
- Modify domain/conversation.py, services/run_coordinator.py and database migrations/repositories.
- Add optional message segments with encrypted storage and legacy defaults; keep the existing final content API unchanged.
- Emit the same ToolStep object used for persistence, including orphan end events.

### 3. Repair rendering
- Modify app.py, ui/chat_view.py and ui/tool_steps.py.
- Forward complete records, update steps by call ID in their owning segment, preserve expanded details, and replay stored segments in order.

### 4. Verify
- Run the new regressions, existing tool/run/history/fork tests, lint and typing checks.
- Do not commit unrelated working-tree changes.

## Verification
- Initial regressions reproduced missing live details and missing persisted segments.
- Final focused regression run: 248 passed (Qt offscreen, including coordinator, SQLite, chat, tools, history, retry and fork tests).
- Domain, history projection, SQLite repository and tool view pass targeted mypy checks.
- Legacy v11 migration, encrypted storage update/reopen, delayed results and preserved expansion/hidden state are covered.
- Existing live and concurrent workspace edits were retained; history projection had moved to application/history_payload.py during implementation, so the fix follows that shared projection.
