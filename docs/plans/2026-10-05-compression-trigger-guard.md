# Compression Trigger Guard Implementation Plan

> **For Claude:** Execute this focused plan in the current shared checkout; the other chat owns core compression/runtime wiring. Do not modify those shared files here.

**Goal:** Prevent manual compression, retries, and post-reply usage refreshes from launching duplicate compression jobs.

**Architecture:** A synchronous application-layer guard owns one active attempt and a per-branch handled set. Both manual and automatic jobs consume the branch's automatic-preview opportunity, but explicit manual retries remain available. Existing threshold selection, persisted compression versions, and progress animation remain unchanged.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio.

---

## Ownership

- This chat creates `src/limbowave/application/services/compression_trigger.py` and `tests/unit/test_compression_trigger.py`.
- Chat `01a10a67-4d78-7b92-bc53-6d1918b7b771` owns core compression generation/application plus `app.py` integration.
- Never write to the user's live conversation database during this fix.

## Task 1: Reproduce trigger failures

Create unit tests for manual -> finish -> reply/usage refresh, repeated automatic refreshes, manual retry, simultaneous branch requests, cancellation/failure cleanup, restored accepted versions, and stale completion tickets. Run `.venv/Scripts/python.exe -m pytest tests/unit/test_compression_trigger.py -q`; expect an import error before implementation.

## Task 2: Implement the guard

Create `CompressionTrigger` with `try_begin(branch_id, automatic=False)`, `finish(ticket)`, `mark_handled(branch_id)`, and read-only `busy`. Claim before any await; only the active ticket may release the claim. Declined claims must not consume a different branch's automatic-preview opportunity. Release in the caller's `finally` block.

## Task 3: Integrate via the owning chat

Send the verified API to the other chat. Replace `auto_preview_done` with this guard in its `app.py` patch. Route manual, automatic, and retry generation through the same guard; retain actual usage-based send blocking. Restored accepted versions may be marked handled without creating a job.

## Task 4: Verify

Run focused tests, Ruff, and mypy for the new module. Read the integration diff for claims-before-await, all-path cleanup, and preserved history/preview scope checks. Re-run relevant shared regressions once the other chat's changes are ready. Do not claim full end-to-end success before core integration finishes.

## Verification completed

- Added actual app callback regressions in `tests/ui/test_compression_trigger_wiring.py` with the core owning chat's agreement.
- 13 guard tests + 9 app callback tests pass, including duplicate retry reads and stale BLOCK reports across both asynchronous usage-read stages.
- Combined compression service/runtime/widgets/state suite: 103 passed.
- Ruff passes for all three owned Python files; mypy passes for the guard module.
- The core owning chat also verified the real Pi/local mock provider path: preview isolation, accepted summary in subsequent payload, resume, and rollback restoring original content. Broad suite results are reported by that chat.
