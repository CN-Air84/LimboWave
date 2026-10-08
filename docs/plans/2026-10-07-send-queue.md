# Send Queue Implementation Plan

**Goal:** Add a session-only FIFO for text sends across existing conversations and branches, without concurrent generation or silently discarding drafts.

**Architecture:** A UI-independent in-memory queue stores immutable request IDs, scope and text. App wiring owns dispatch through the existing command reservation, history restore, model synchronization and coordinator acceptance. Queue UI is separate from the editable composer. Background dispatch preserves the currently displayed conversation whenever it has a saved scope.

**Tech Stack:** Python, asyncio, PySide6, pytest, pytest-qt, Ruff, mypy.

## Scope and trade-offs
- Implement text-only FIFO, not parallel generation or durable/restart-safe scheduling.
- Queue multiple messages, show their global order and per-conversation pending list; cancel before dispatch and restore cancelled text to its draft.
- Once coordinator acceptance creates the normal user message, never resend that queue entry automatically, even when provider startup subsequently fails.
- Pause remaining work on stop/error; explicit resume/retry or cancellation is required.
- Pin conversation and branch; restore target permissions and recheck context limits before dispatch. Use the existing model-routing path and command arbitration rather than calling the kernel directly.
- Keep the displayed saved conversation and its new draft intact while another queued request runs. An unsaved empty/new view may activate the queued target; its draft is snapshotted before navigation.
- No imports, attachment edits, or old-message edits through draft-only previews.

## Tasks
1. Add queue model and unit tests for FIFO, state transitions, cancellation, retry, pause, capacity, and at-most-once acceptance.
   Files: src/limbowave/application/services/send_queue.py; tests/unit/test_send_queue.py.
2. Add compact queue panel, enqueue keyboard/button path, visible pause/retry/cancel state.
   Files: src/limbowave/ui/send_queue_panel.py; src/limbowave/ui/chat_view.py; tests/ui/test_send_queue_ui.py.
3. Wire serial dispatch, scope-pinned restoration, settlement scheduling, user acceptance, stop/error handling, and lifecycle cleanup.
   Files: src/limbowave/app.py; tests/ui/test_send_queue_integration.py.
4. Verify draft isolation, navigation races, failed restores, cancelled entries, missing scopes, async acceptance failures and queued background runs.
   Run queue and existing draft/navigation/loading/attachment/chat regressions, Ruff and mypy. Inspect synthetic screenshots. Retain unrelated concurrent workspace edits.

## Verification results

- Queue model/UI/app integration and existing draft/navigation tests: **47 passed**.
- Related chat, history loading, attachments, retry and live branch reload modules, each in its own test process: **145 passed**.
- Run coordinator, branch paths, runtime facade, recovery and web runtime modules: **87 passed**.
- Total: **279 related tests passed across these final batches**. This is not a claim that the entire repository suite passed.
- An earlier combined regression process terminated with a native Qt access violation during test teardown, after 121 passing test progress markers. Re-running the same modules in the isolated batches above passed. The crash log and per-module results are retained under .var/send-queue/.
- Ruff and strict mypy passed for all five touched production modules; Ruff also passed for the three new test modules. git diff --check passed.
- Inspected local synthetic screenshots of queue and paused states at wide and narrow sizes. Labels wrap, cancellation and resume controls remain visible, and drafts stay editable.

## Delivered behavior

- Existing conversation/branch text sends queue FIFO. Each entry keeps an immutable scope and text; at most 50 entries are retained in window-lifetime memory.
- The enqueue button and Ctrl+Enter share validation. A successful enqueue clears only its own composer; rejected/stale submissions leave newer drafts intact.
- Cancellation returns text to the owning draft, without overwriting another draft or silently cancelling an existing message edit.
- Preparation locks the active queue entry; other waiting entries remain cancellable. Runtime command arbitration prevents overlapping device submissions.
- Queued background sends retain the selected saved conversation. Scope existence, target restoration, model catalog synchronization, permissions and context limits are checked before acceptance.
- Persisted user-event acceptance removes the queue entry before provider I/O, so a later provider failure cannot duplicate it on resume.
- Stop, remote abort, runtime interruption and errors pause remaining work. Retry/resume is explicit. Shutdown clears the ephemeral queue; no automatic replay on restart.
