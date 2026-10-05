# Compression Branch Restart Implementation Plan

**Goal:** Keep valid compression context and transcript markers across fork/regeneration and application restart, including existing affected branches.

**Architecture:** Copy accepted compression versions into each new branch transaction, only when the complete input prefix survives the fork. Copies have independent activation and edit/rollback state. Restore the persisted branch projection before sending. A one-time metadata-only migration repairs legacy branches without touching original messages or decrypting summaries; local compression decisions take precedence. Always remove stale application overlays before applying the branch-local active version.

**Tech Stack:** Python, SQLite, existing encrypted repositories, Pi runtime restore, pytest.

## Tasks
1. Add failing fork/regeneration/restart tests against fresh SQLite connections, covering context, markers, independent rollback, and forks inside compressed ranges.
2. Add shared inheritance eligibility checks and transactional branch-local version copies; return branch-specific history and compression snapshots through the storage worker.
3. Normalize restore snapshots and reapply effective branch compression before sends.
4. Add schema migration 13 to repair only missing legacy branch compression state, preserving ciphertext and checking ancestry, time and complete prefix.
5. Verify migration on a temporary read-only-source database backup, run focused and broader regression tests, lint/type-check changed code. Do not modify the live database or unrelated workspace changes.

## Findings and verification
- Read-only metadata audit: the latest accepted compression remained active on the parent; the next regeneration created a child without any compression versions. Runtime restore and history marker projection correctly read branch-local state, exposing the missing inheritance after restart.
- Fork/edit/regeneration now copy eligible accepted versions in the branch transaction, with separate IDs and active flags. Regenerating an inherited message uses the actual current branch as parent, not the message's physical owner.
- Restore removes stale application overlays before reapplying the branch's accepted version. The post-fork provider call uses that same durable state; the history event includes branch-specific markers.
- Migration 13 was exercised only on a temporary SQLite backup of the real database: all 17 original compression records preserved, 21 eligible inherited records added, latest affected child recovered its active 182-message compression. Counts remained 262 messages, 37 branches, 1192 runtime mirrors. The live database was not modified and the temporary database was deleted after checking.
- Final full unit suite: 1327 passed, 6 platform-dependent skips.
- Final combined focused regression: 26 passed (fresh SQLite/kernel restart, storage subprocess, migration, Qt separator recreation, and real Pi/local-provider fork/edit/regeneration requests plus rollback).
- Ruff passed for all changed sources/tests; mypy passed for six changed production modules; tracked-file diff whitespace checks passed.
- No live provider requests, application restart, packaging, commits, or edits to unrelated workspace changes.
