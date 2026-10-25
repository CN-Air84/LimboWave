# Cross-conversation Chat History Implementation Plan

**Goal:** Add `query_chat_history` for saved visible messages; every model invocation asks unless the caller conversation uses FULL_ACCESS.

**Architecture:** Register a proxied Pi tool in the existing extension and dispatch through ToolGateway. A read-only ChatHistoryService queries the authoritative repositories after permission checks; SQLite streams only visible content, not thinking/tool/runtime payloads. A dedicated HISTORY_READ capability always requires per-call confirmation except FULL_ACCESS; persistent grants never bypass it. Reuse the existing floating permission prompt with only deny/allow-once choices.

**Tech Stack:** Python 3.12+, SQLite encrypted repositories, PySide6, Pi TypeScript extension, pytest.

## Contract
- Optional `query` (literal case-insensitive substring of title/body), `conversation_id`, `branch_id`; `limit` default 20 (1..50), `offset` default 0 (0..10000).
- Return newest-first stored messages with owning conversation/branch IDs, conversation title, role, timestamp, status and content excerpts. Include count, has_more, next_offset and explicit content/output truncation. No runtime entries, thinking or tool details; inherited fork prefixes are not duplicated.
- Searches happen in process after decryption; do not add a plaintext search index or alter the database schema.
- Approval precedes any cross-conversation lookup/decryption. Web-origin runs remain denied even under FULL_ACCESS. Validate args before prompting; re-check active conversation and origin after the user answers.

## Task 1: Regression tests
Create G:/LimboWave/tests/unit/test_chat_history.py covering both memory and SQLite, keyword/title filters, paging, stable order, branch ownership, malformed args, bounded UTF-8 results, exclusion of private payloads, repeated approval and all presets. Extend G:/LimboWave/tests/ui/test_permission_prompt.py to assert no session grant and safe dismissal.
Run `.venv/Scripts/python.exe -m pytest tests/unit/test_chat_history.py -q`; expect failures before implementation.

## Task 2: Read-only storage/service
Modify G:/LimboWave/src/limbowave/application/repositories.py and both repository implementations to stream visible messages with optional ID filters. Create G:/LimboWave/src/limbowave/application/services/chat_history_service.py for validation, matching, paging and budgeted output.

## Task 3: Permission + gateway
Modify G:/LimboWave/src/limbowave/domain/permissions.py and G:/LimboWave/src/limbowave/application/services/permission_service.py for independent per-call history permission. Modify G:/LimboWave/src/limbowave/application/services/tool_gateway.py for schema, validation, service dependency and dispatch. Wire service and allow-once popup in G:/LimboWave/src/limbowave/app.py; label HISTORY_READ in G:/LimboWave/src/limbowave/ui/permissions_dialog.py.

## Task 4: Registration + verification
Synchronize G:/LimboWave/src/limbowave/infrastructure/extensions/policy_enforcement.ts and G:/LimboWave/src/limbowave/infrastructure/pi_runtime/environment_builder.py. Run history, gateway, permission UI, memory and web boundary regressions plus tool-list/real-extension integration tests. Run ruff/mypy on touched code, inspect diff for unrelated modifications. No commit of pre-existing user changes.

## Verification results
- Combined focused suite (history service + all presets + real Pi calls + tool IPC/registration + memory + SQLite + composition + Web hard boundary + permission UI): **363 passed, 1 skipped**. The skip is the SQLite-specific projection test on the memory fixture; its SQLite counterpart passes.
- Ruff passes for all touched Python files; mypy passes for 10 touched production files.
- Broader unit run excluding an already-uncollectable packaging test: **2012 passed, 6 skipped, 2 failed**. The failures are `test_layer_does_not_import_qt[application/services/preferences_service.py]` and `test_switch_preparation_keeps_event_loop_alive_and_reuses_reads`, in existing preference/session work not changed for this feature. Full collection also finds the existing missing `scripts.package_server` import. These unrelated changes were preserved.
- Existing SQLite grant schema only permits the four persistently grantable capabilities. HISTORY_READ is deliberately rejected by grant/custom-grant APIs rather than adding a meaningless persistent permission or changing the schema.
- Usage reference: G:/LimboWave/docs/development/chat-history-tool.md.
