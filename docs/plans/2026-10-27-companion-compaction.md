# CMP-01 Companion Compaction Implementation Plan

**Goal:** Preserve companionship continuity with a dedicated, reviewable and reversible summary.

**Architecture:** Reuse encrypted CompressionVersion storage and runtime projection. Register immutable prompt identities (1 = existing task template, 2 = companion template), so legacy versions retain their meaning without a schema migration. New application requests default to companion; retries may explicitly select either template. Distill bounded chronological batches, carrying forward only the intermediate summary in a fresh isolated session. Never auto-promote summaries into global memory.

**Tech Stack:** Python, PySide6, existing encrypted SQLite/in-memory repositories, pytest/pytest-qt.

---

## Task 1 — Domain and regression tests
- Modify `src/limbowave/domain/compaction.py`: profile registry, companion template, temporal/source rules, explicit boundary precedence and uncertainty.
- Create `src/limbowave/domain/companion_compaction.py`: deterministic bounded batching, dated role/source labels, no silent whitelist truncation.
- Create `tests/unit/test_companion_compaction.py`: template, profiles, source labels, batching, whitelist and storage round trips.
- Run `.venv/Scripts/python -m pytest tests/unit/test_companion_compaction.py -q` before and after implementation.

## Task 2 — Service and configuration
- Modify `src/limbowave/application/services/compression_service.py`: freeze template identity on version creation, companion batch generation, fresh-session isolation between batches, failure/cancellation safety, immutable input prefix validation.
- Modify `src/limbowave/domain/configuration.py`: companion default profile while keeping task support.
- Test multi-batch handoff, bounded input, failure, cancellation, version persistence, and accept/rollback in both stores.

## Task 3 — Desktop client
- Modify `src/limbowave/ui/compression_widgets.py`: expose profile and full version time/source scope, template selector for regeneration, explicit history restoration labels.
- Modify `src/limbowave/app.py` and chat controls: use configured profile for automatic/manual requests, preserve selected retry profile, allow history inspection without model generation.
- Add focused UI tests and preserve runtime-managed apply/rollback paths.

## Task 4 — Verification and documentation
- Run existing compaction, runtime projection, inheritance and UI tests alongside new tests.
- Run ruff and mypy on touched code; document unrelated failures rather than changing concurrent work.
- Document new defaults, client history/retry actions, branch-local memory semantics, bounded-generation failures and unchanged privacy restrictions.

## Invariants
- Original messages and prior generated summaries remain available; no partial batch becomes active.
- All source messages are represented in the version boundary; post-preview turns remain outside it.
- Recent mood is dated and not a diagnosis/personality label; assistant speculation is not user fact.
- Explicit no-proactive-topic boundaries outrank reminders and emotional continuity.
- Existing privacy filtering/denial remains authoritative; this feature does not bypass it.
- Model extraction quality is probabilistic: tests verify contracts and lifecycle, not semantic guarantees.


## Completion — 2026-10-27

- Tasks 1–4 completed in the existing workspace; no database migration and no automatic global-memory writes.
- Combined compaction unit/UI/integration, toolbar, context usage, and privacy-boundary verification: **270 passed** in 75.52 seconds.
- Ruff passed on all feature-touched Python files and tests. Mypy passed on 7 relevant source files using `--follow-imports=silent`.
- Real Pi + local mock provider exercised companion multi-batch generation, native session reset, live-chat isolation, restart/resume and rollback. No paid/external model call was used for semantic evaluation.
- Qt offscreen screenshots checked with the installed Chinese font: `.tmp/cmp01-preview.png` and `.tmp/cmp01-toolbar.png`; version controls fit the existing layout.
- Usage and limitations: `docs/development/companion-compaction.md`. Original messages remain available; extraction quality remains model-dependent.
- Other concurrent workspace changes were preserved; no commit was created.
