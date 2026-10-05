# Compaction-safe attachment context implementation plan

**Goal:** Preserve access to registered text attachment references across compaction without widening tool permissions.

**Architecture:** Build a metadata-only manifest from persisted user-message intent snapshots on the active branch. Sync it to Pi via an authenticated, acknowledged control command before each prompt. A context hook injects one transient manifest per model request, including post-compaction requests within the same run; session changes clear it. Do not infer authorization from summary text, enumerate all registered files, reload image bytes, or modify the permission gateway.

**Tech Stack:** Python, SQLite repositories, Pi RPC, TypeScript extension, pytest with local mock provider.

## Tasks
1. Add failing regression tests for branch manifest persistence, fork boundaries, empty/reset context, and exact file reads after real Pi compaction.
2. Add metadata projection in RunCoordinator and the conversation storage worker. Keep stored user text and image payloads unchanged.
3. Add the kernel context port, authenticated adapter command with run-id acknowledgement, and transient context hook. JSON-encode file metadata as reference data; clear on session_start but not session_compact.
4. Verify targeted unit tests, real Pi integration tests, permission-denial regressions, and lint. Existing unrelated working-tree edits must remain intact.

## Alternatives considered
- Re-uploading or relying on the generated summary: cannot guarantee stable references and duplicates user work.
- Granting shell or broad filesystem access: does not restore attachment identity and unnecessarily broadens permissions.
- Application-owned branch manifest (selected): derives identity from durable records and survives any model-history compaction.

## Verification results
- Before implementation: all 5 initial regression tests failed (no durable attachment context was synchronized).
- New coverage: 7 unit/process tests passed, including reopening persisted conversations without re-upload; 2 real-Pi attachment tests passed.
- Related real-Pi integration group: 26 passed (attachments, memory, adapter, compression runtime, tool bridge).
- Full unit suite: 1228 passed, 6 skipped, 1 compression-marker ordering failure. The affected marker test module passed all 16 cases when rerun; its implementation was not changed for this fix.
- Changed Python files passed Ruff; git diff --check passed.
- Real-Pi evidence: after native compaction, the original file ID read lines 58001-58200 through the actual ToolGateway without re-upload/rebinding; a terminal directory-listing call remained denied.
- Delivery is source-only; no executable packaging, running-app restart, or user-data migration was performed.
