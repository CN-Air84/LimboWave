# Runtime Message Anchor Repair Implementation Plan

**Goal:** Keep Fork and regeneration usable when Pi normalizes a user prompt, and safely repair already-unlinked runtime records.

**Architecture:** Associate newly captured user entries with the timestamp of the actual accepted user message, not only its text. Repair legacy missing links only when the run identity, conversation, unique user entry and run time window agree. Preserve exact attachment-note repair for older records without timestamps; refuse ambiguous or foreign anchors.

**Tech Stack:** Python, SQLite, pytest, PySide6, process-isolated storage.

## Steps
1. Add regression tests for transformed/image-hint prompts, repeated text, legacy orphan repair, ambiguous/foreign candidates, and real storage-process execution.
2. Capture accepted user-message timestamps in live runs and use them when building runtime mirrors; keep text fallback for kernels without timestamps.
3. Repair legacy links using bounded run evidence before editing/regeneration, without rewriting message content or inventing runtime entries.
4. Verify Fork/regeneration after reopening, original-branch isolation, worker execution, and app button wiring; run related tests and static checks.

Existing backdrop/main-window changes are unrelated and must remain untouched. Do not modify the user's live database during development; repairs occur transactionally when the updated application next performs the operation.

## Verification

- Before implementation: 6 regression cases failed because normalized prompts had no application-message anchor; 8 refusal/safety cases passed.
- Final related unit/process selection: 209 passed, 1 deselected.
- Real Pi 1.1.0 + local mock provider + encrypted SQLite: 4 passed (large resized image with text, image-only input, and both corresponding legacy NULL-link repairs).
- Desktop button wiring and UI responsiveness: 7 passed.
- Ruff on all changed implementation/test files: passed. Focused strict mypy on the coordinator: passed. git diff --check: passed.
- The deselected pre-existing test is test_switch_preparation_keeps_event_loop_alive_and_reuses_reads. It counts list_for_conversation calls, but current branch recovery reads metadata/selected entries instead. Its `0 == 1` failure was reproduced against the unmodified HEAD coordinator in memory, without changing workspace files. No unrelated performance-test changes were made.
- User database was inspected read-only for metadata. No user records, credentials, runtime files, or existing backdrop changes were modified.
