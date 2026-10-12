# Safe backup restore implementation plan

**Goal:** Replace manual folder replacement with validated, reversible restore and normal restart.

**Architecture:** Decrypt and validate into a private staging directory without touching live files. After GUI services close, journal moves of the owned data files into a retained rollback folder, install staged files, and restart after diagnostics release their locks. Startup repairs interrupted transactions before opening storage. Logs/runtime/themes are not moved. New backups include a password-wrapped vault key and encrypted credentials, and SQLite uses its backup API to include WAL commits.

**Tech Stack:** Python, SQLite, PySide6/qasync, existing encrypted LWB archives.

## Tasks
1. Add regression tests for WAL snapshots, complete manifests, unsafe archive paths, portable vault backups, staging validation, installation, failed installation rollback, and interrupted transaction recovery.
2. Extend backup creation to include encrypted vault metadata/credentials; wrap the existing key with the backup password (never persist a plaintext key). Decrypt an archive once per restore and require exact manifest coverage.
3. Implement a journaled restore service limited to owned data paths. Validate SQLite and migrate staging before shutdown. Preserve old data automatically; recover unfinished moves at startup. Reject link targets and incompatible legacy backups safely.
4. Wire settings confirmation and shutdown callbacks. Prevent repeated restore/reset, show truthful errors, apply only after shutdown, and relaunch normally in source and packaged builds.
5. Run targeted unit tests and real Qt lifecycle tests in temporary directories, then lint/type checks and relevant regression suites. Do not operate on the user's actual data or launch a replacement application during tests.

## Acceptance
- Cancel/password/corruption/version failures do not change current data.
- Confirmation shows content summary, automatic retention and restart, and the password needed after restart.
- Switch runs after background tasks/storage stop; shutdown failure prevents it.
- Mid-switch errors roll back; interruption is repaired on next launch.
- Old data stays in an identifiable local recovery folder after success.
