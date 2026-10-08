# Retry Context Isolation Implementation Plan

**Goal:** Manual and automatic retries send the original turn exactly once in model context while retaining attempt audits and the existing in-place UI.

**Architecture:** Reconstruct the pre-attempt runtime prefix from persisted entry mirrors and retry message-ID links. Reuse the existing verified runtime restore/new-session operations, without creating an application branch. Fail closed when prior history cannot be restored. Restore before routing and resynchronize memory/attachment context; automatic retries resend the composed prompt, not just plain text.

**Tech Stack:** Python, asyncio, Pi RPC, pytest, local OpenAI-compatible mock provider.

## Tasks
1. Add a real-Pi/local-HTTP regression test with two injected failures. Assert request user-message counts remain constant for manual and automatic retries; retain independent identical earlier input, audit attempts, and a single projected UI turn.
2. Extend `RunCoordinator._prepare_retry` to return a pre-attempt snapshot, resolve retry chains by IDs, and reject stale/non-tail targets. Restore that snapshot (or verified empty session) before resending. Keep branch IDs and audit records unchanged.
3. Apply the same restore path to automatic retries. Preserve composed document prompts/images and resync runtime contexts after restoration. Restore failures must finalize without another provider call and leave runtime invalid.
4. Add focused tests for repeated failures, reopened conversations, attachments, missing mirrors, restore failure/cancellation, and legitimate repeated text. Run retry, route, branch, compression, storage-worker and UI regression tests, then broader tests and lint.

## Verification
Run `.venv/Scripts/python.exe -m pytest tests/integration/test_retry_context_e2e.py -q` before and after implementation (red then green). Run `.venv/Scripts/python.exe -m pytest tests/unit tests/ui -q` and relevant integration tests after focused fixes.

## Workspace constraints
Existing unrelated uncommitted edits remain intact. No new application branches, database migration, or automatic replay of old conversations. No real provider calls or real credentials in tests.

## Results
- Added 20 retry regressions (12 unit/process cases and 8 real-Pi HTTP cases). All pass. The initial four HTTP cases failed before the fix with increasing user-message counts.
- Broader checks: 1655 unit tests passed, 6 platform skips; 174 selected UI tests passed; 17 real-Pi integration tests passed.
- Unrelated existing failures left unchanged: `test_no_hardcoded_font_sizes_in_views` flags OOBE demo hardcoded sizes; `test_confirmed_failover_retries_original_message_not_text` omits `_send_with_catalog_sync` from its callback test namespace. The unit pass count excludes the former; selected UI run includes the latter failure.
- Ruff, coordinator mypy, and git whitespace checks pass.
- No production UI, schema, or kernel protocol changes. Historical attempt audits remain; retry restoration is triggered by a retry, not a blanket data migration.
