# Cross-conversation Drafts Implementation Plan

> Execute locally, task by task, with regression checkpoints; preserve unrelated workspace changes.

**Goal:** Allow text drafting in other conversations while one run is generating, without interrupting or redirecting that run.

**Architecture:** Keep the single runtime and its live view unchanged. Give history previews a draft-only composer, with read-only message actions and disabled send/model/permission/attachment controls. Keep composer snapshots in window-lifetime memory by conversation and branch; restore them only after successful navigation. Do not persist plaintext drafts to disk, queue submissions, or introduce concurrent generation.

**Tech Stack:** Python, PySide6, asyncio/qasync, pytest-qt, Ruff, mypy.

---

## Design and alternatives

- Recommended: editable drafts with explicit waiting feedback; smallest runtime risk and solves the blocked-input complaint.
- Deferred: queued sends require cancellation, ordering, scope validation and failure UX.
- Deferred: parallel runs require independent runtimes and routing changes.
- Text previews cannot import attachments or change the live model/permissions. Existing live draft attachments and edit state must be isolated and retained during successful navigation.
- Show a persistent note that drafts are retained when switching, only for the current app session, and are not automatically sent. Failed restore keeps the draft and a retry hint.

### Task 1: Regression cases

**Files:** `tests/ui/test_session_switch_during_generation.py`, `tests/ui/test_conversation_drafts.py`

1. Add failing tests for editable draft previews and blocked direct/keyboard submission.
2. Cover multiple conversations, branches, return to live, clearing a draft, completion during read/render, failed restore, and original draft/attachment preservation.
3. Run targeted tests and verify the expected failures.

### Task 2: Draft presentation

**Files:** `src/limbowave/ui/chat_view.py`, `src/limbowave/ui/main_window.py`, `src/limbowave/ui/attachment_bar.py`

1. Add an explicit draft-only preview mode separate from normal send capability.
2. Keep input and plain-text paste enabled; disable attachment import, model/permission changes and all transcript mutations.
3. Add persistent, wrapping waiting text and enforce the send guard in the handler, not only on the button.
4. Add composer snapshot/restore APIs that retain text, edit state and attachment presentation without persisting to disk.

### Task 3: Scope-aware draft lifecycle

**Files:** `src/limbowave/app.py`

1. Snapshot the currently displayed and live composers before navigation can replace either.
2. Restore the target draft after successful history load; keep the old draft on invalid navigation/restore failure.
3. Carry preview text into the live composer on completion, without automatic submission.
4. Isolate drafts on successful new-session transitions and remove snapshots when deleting a conversation.

### Task 4: Verification

1. Run the targeted navigation/draft tests with the offscreen Qt platform.
2. Run UI, unit and integration regression tests plus Ruff and mypy.
3. Capture and inspect the draft-only preview with a synthetic local conversation, if the environment supports it.
4. Report test results and the session-only/text-only/no-queue boundaries.

## Validation results

- Draft/navigation/history-loading regression suite: **39 passed** (including 22 draft/session-switch cases).
- Updated the old global-composer expectation in `tests/ui/test_history_loading.py`: text stays intact while loading, then the destination gets its own draft rather than inheriting the source text.
- Ruff and strict mypy passed for the changed production files; Ruff also passed for the three affected test modules.
- Inspected synthetic local screenshots at 1120 x 760 and 720 x 640: waiting and restore-failure notices wrap without obscuring the draft.
- The full-suite run, started before the final test-expectation updates and while other workspace changes were ongoing, reported **2942 passed, 13 failed, 7 skipped, 1 teardown error**. Two failures were the old global-draft expectations, now passing in the 39-test rerun. Remaining failures involve checkbox/font/rendering, diagnostics, theme popup, marquee/tool animation, compression ordering, and shell execution; no full-suite success is claimed.
- Drafts are retained only in memory for this open application session. Sending remains manual; there is no send queue or concurrent generation.
