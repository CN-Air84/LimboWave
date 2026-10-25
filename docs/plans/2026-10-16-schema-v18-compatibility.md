# Schema v18 Compatibility Implementation Plan

**Goal:** Allow main to open the existing server-branch v18 library and upgrade older libraries safely.

**Architecture:** Carry over the published migrations 014–018 verbatim, including synchronization triggers and covering indexes. Preserve migrations 001–013 and the downgrade guard. Existing v18 databases require no DDL.

**Tech Stack:** Python, SQLite, pytest, PySide6.

## Implementation and validation

1. Add regression tests for v13 upgrade, v18 reopening without schema/data changes, sync-trigger write and rollback behavior, and future-version rejection.
2. Port migrations 014–018 and their registry entries from codex/limbowave-on-server; set CURRENT_VERSION to 18.
3. Keep the compression-inheritance regression scoped to target=13.
4. Run focused database, backup/restore and startup tests plus lint checks.
5. Validate migration on a consistent SQLite backup of the local library, then launch the source application for login.

Existing working-tree edits and the original library must be preserved. Do not import the server runtime or change schema_version manually.

## Completed verification

- Database compatibility/repository/compression tests: 22 passed.
- Bootstrap and startup UI/cleanup tests: 29 passed.
- Backup, restore, request log and restore lifecycle tests: 54 passed, 1 failed.
- Remaining failure: test_restore_lifecycle[cancel] crashes with Windows access violation 0xc0000005 when the test immediately calls parent.close(). Reproduced in an isolated temporary library with the original HEAD v13 migration module. It is independent of this schema change; no unrelated production or test code was changed.
- Ruff checks and mypy for the migration module passed.
- Original local library opened read-only; consistent SQLite backup saved under .var/schema-v18-verification/20261016-164858.db. The backup passed PRAGMA quick_check, matched the fresh v18 schema, and reopening it performed no migration writes or version-history changes.
- Started the source application from main. Window is responsive at the login screen; actual vault unlocking still requires the user's master password.
