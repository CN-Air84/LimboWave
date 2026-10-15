# History Recovery Performance Implementation Plan

**Goal:** Remove repeated ancestry walks and unnecessary sorting without changing recovery semantics or touching concurrently edited storage/runtime files.
**Architecture:** Keep runtime mirrors immutable. Memoize ancestry results only within one operation, using iterative walks with existing cycle rules. Select earliest captures and latest tips in linear passes instead of sorting unrelated records. No persistent caches, schema changes, or dependencies.
**Tech Stack:** Python 3.12+, pytest, existing in-memory and SQLite integration coverage.

## Scope and project review (2026-10-14)
- Reviewed application/service and repository boundaries, history pagination, desktop and web history rendering, stream state, backup I/O, and existing pressure tests.
- Existing uncommitted work is preserved. Sidebar metadata, search limits, attachments, shutdown and web state are actively changing elsewhere and are excluded from this patch.
- Prefer focused algorithm changes over a cross-layer rewrite or long-lived cache.

## 1. Establish behavior and measurements
- Create `tests/unit/test_history_recovery_performance.py` with operation-count guards, deep/unsorted ancestry, missing parents, cycles, shared removed prefixes and immutable input checks.
- Create `scripts/benchmark_history_recovery.py` for repeatable synthetic before/after measurements, excluding fixture construction.
- Run new tests before changes; record failing complexity guards and baseline timings.

## 2. Recovery ancestry
- Modify `src/limbowave/application/services/runtime_state_service.py`.
- Cache whether a node descends from a safe user root and the nearest retained parent of removed nodes.
- Preserve no-pollution identity fast path, root boundaries, model/thinking entries, removed summaries, orphan handling and cycle behavior.
- Pick the earliest `(captured_at, id)` mirror per entry without sorting the entire conversation for a branch-only snapshot.

## 3. Compression overlay ancestry
- Modify `src/limbowave/application/services/compression_context.py`.
- Cache resolved parents across descendants while preserving immutable inputs, cycle errors, output order and context-usage invalidation.

## 4. Branch tip selection
- Modify `src/limbowave/application/branch_path.py` only in `_mirror_leaf`.
- Compute parent IDs once and select the maximum tip by `(captured_at, id)` in a single pass; preserve tie behavior and no-tip result.

## 5. Verify and report
- Run new tests, existing branch/recovery/compression tests, then broader unit/integration suites where feasible.
- Run Ruff and type checks for modified code; distinguish unrelated baseline failures.
- Repeat benchmarks at increasing sizes and write `docs/audits/2026-10-14-history-recovery-performance.md` with actual figures, limitations and deferred project findings.
- Do not commit, revert or overwrite unrelated working-tree changes.

## Completion record
- Completed the scoped changes in three production files; no dependencies or schema changes.
- Added 18 regression tests and a repeatable benchmark script.
- Final targeted suite: 55 passed. Unit/integration suite: 2233 passed, 7 skipped, 2 cleanup warnings. Relevant UI suite: 26 passed.
- 20,000 seeded before/after structural comparisons matched. Targeted Ruff/Mypy and diff whitespace checks passed.
- Final measurements, limits and deferred findings are in `docs/audits/2026-10-14-history-recovery-performance.md`.
