# Active Memory Query & Transparent Scratchpad Implementation Plan

**Goal:** Add run-bound targeted memory retrieval, explicitly approved mutations, and a transparent branch-local scratchpad.

**Architecture:** Reuse encrypted `memory_documents` and existing run identity / one-shot approval gates. Keep periodic formal memory reminders unchanged. Search only current branch plus effective global overrides; global originals remain user-managed. Scratchpad contains short factual working context, not hidden reasoning; it is visible and editable by users.

**Tech Stack:** Python services / SQLite and in-memory UnitOfWork, TypeScript Pi extension, PySide6, pytest.

## Decisions
- Prefer literal case-insensitive token matching with ranked bounded excerpts over a new vector index: local, deterministic, no provider or migration dependency. `scope`: all/session/global/archived; all excludes archived.
- `update_memory(memory_id, updated_content)` and `delete_session_memory(memory_id, archive=true)` target current branch only. Always ask, regardless of add policy or FULL_ACCESS. Bind approval to original item, normalized payload, operation, run and call; reject stale edits and mismatched retries. Archive is recoverable; delete removes current entry, not historical snapshots.
- Scratchpad is one 4000-character document per branch, read/write tools with revision compare-and-swap, plus transparent UI. Empty content clears it. Capture alongside turn snapshots so historical forks never receive future notes; old turns inherit an empty pad. Archive lists are snapshotted too.
- Deny remote-origin access as for existing app tools; no content in extension diagnostic logs. Treat retrieved text and notes as untrusted reference data, never permissions or instructions.

## Steps
1. Add `tests/unit/test_active_memory.py`: scope/override/ranking/limits; operation consent, replay, stale-original rejection; scratchpad bounds/revision/fork/encryption/cascade. Run targeted tests red.
2. Extend `src/limbowave/domain/memory.py` and `src/limbowave/application/services/memory_service.py` with search, archive, mutation approvals/receipts and scratchpad APIs. Keep add compatibility. Run service tests green on both backends.
3. Extend `src/limbowave/application/services/tool_gateway.py`, `src/limbowave/app.py`, permission capability labels, `src/limbowave/infrastructure/pi_runtime/environment_builder.py`, and `src/limbowave/infrastructure/extensions/policy_enforcement.ts`. Register all tools with server-bound identities, redact diagnostics, inject transient notes each turn. Test real approval and IPC paths.
4. Extend `src/limbowave/ui/session_memory_panel.py` using a small dedicated scratchpad component, and archive view/restore controls. Follow existing asynchronous persistence and error display; protect against lost updates. Add UI tests and inspect a rendered panel.
5. Extend `tests/integration/test_memory_pi.py` for context replacement and tool binding, document contracts in `docs/development/memory.md`. Run memory/UI/integration regression tests, Ruff and mypy; report any unrelated baseline failures separately.

## Verification commands
- `.venv/Scripts/python.exe -m pytest tests/unit/test_active_memory.py tests/unit/test_memory_service.py tests/unit/test_memory_runtime.py -q`
- `.venv/Scripts/python.exe -m pytest tests/ui/test_memory_panel.py tests/ui/test_scratchpad_panel.py tests/integration/test_memory_pi.py -q`
- `.venv/Scripts/python.exe -m ruff check src tests`
- `.venv/Scripts/python.exe -m mypy src`

No commits or separate worktrees are needed for this implementation; preserve unrelated working changes.

## Completion and verification

Implemented the five new model tools across gateway, permission handler, runtime allowlist and real Pi registration. Added encrypted branch scratchpad/archive documents, historical snapshots, exact-ID lookup, bounded search results, original-version-bound one-shot consent, replay protection and optimistic scratchpad revisions. Added transparent scratchpad and archived-memory panels, including restore/delete and draft-preserving conflict handling. Documented contracts and privacy limits in `docs/development/memory.md`.

Verification completed during this implementation:
- Related service/gateway/history/runtime unit suites: **269 passed, 1 backend-specific skip**.
- Memory/scratchpad/popup/new-session UI regression: **87 passed** on the successful rerun; latest scratchpad checks: **12 passed**. One earlier combined UI process encountered a Windows Qt access violation; the isolated full UI rerun passed.
- Real Pi active memory + existing memory + tool bridge + remote-origin boundary: **235 passed**.
- Related Python source/test files: Ruff passed. Related eight source modules: mypy passed with imported modules checked silently; full-repository mypy still reports errors in concurrently edited environment service code.
- Full-repository Ruff has unrelated concurrent environment/presence/packaging errors; no claim that the whole working tree is clean.
- Light/dark panels rendered and visually inspected with synthetic data under `.tmp/active-memory-qa/`; previews contain no user data.

No commit was created. Unrelated in-progress working-tree changes were preserved.
