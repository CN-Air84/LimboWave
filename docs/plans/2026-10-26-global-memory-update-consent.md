# Global memory update with explicit per-call consent

**Goal:** Let `update_memory(memory_id, updated_content)` propose edits to global originals, with an actual confirmation popup instead of a read-only rejection.

**Boundary:** No automatic write under FULL_ACCESS or the add-memory allow policy. Resolve the target from the current branch or effective global entries visible to the current conversation. Bind the one-shot approval to the resolved scope and original version. For a global target the popup shows the actual global original, the proposed text, and the effect on all inheriting conversations; existing per-conversation overrides are not replaced. Do not add global deletion through `delete_session_memory`.

**Implementation:** Add a defaulted scope to MemoryMutation (legacy session receipts stay readable); select and persist the correct document transactionally; return the affected scope; expose allowed_operations in search; update the Pi preflight and descriptions; give global edits distinct UI copy.

**Verification:** Both repositories: approval required, replay, scope tampering, stale originals, hidden-global isolation, cancellation, quota rollback, preserved overrides and provenance. Real popup tests include FULL_ACCESS global allow/reject; real Pi test reproduces global update reaching consent and completing. Preserve current session-only archive/delete tests and run the memory regressions.

## Completed

- `update_memory` now resolves visible global originals and presents explicit global-impact consent instead of a read-only block. The approval/receipt carries the resolved scope; persistence updates the global document, never a branch copy.
- Search now advertises per-item `allowed_operations`; the extension allows global update preflight but retains the session-only delete boundary. Mixed older app processes receive a restart instruction rather than a misleading permission error.
- Global original/version checks, capacity limits, active-run checks, scope-bound single-use approval, high-impact audit, existing conversation overrides and provenance are preserved. Older session receipts default to session scope.
- Real global confirmation UI rendered with synthetic data; short before/after proposals use a bounded taller scrolling preview.
- Verification: global-update/unit + real popup checks **32 passed**; active-memory real Pi suite **10 passed**, with **4 global update allow/deny cases** rerun after the final preflight change; existing memory/runtime/timeline/UI/Pi/bridge regressions **182 passed**. Earlier combined new-feature suite: **76 passed**. Related Ruff and mypy checks passed.
- No real user memory was edited during implementation; sample popup was dismissed. Restart the application to load the new host and Pi tool contracts.
