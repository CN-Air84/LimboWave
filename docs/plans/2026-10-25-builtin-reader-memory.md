# Editable built-in reader memory preset — Implementation Plan

**Goal:** Ship one editable global memory, “忠实的阅读者”, expressing the user's machine-honesty, reader-first and evidence-based transparency principles.

**Architecture:** Store the default as an ordinary encrypted global MemoryItem. Explicitly initialize it at desktop service wiring before exposing memory to UI/runtime; do not seed in constructors or read methods. An encrypted receipt in memory_documents makes initialization one-time, preserving edits and deletions across restart/backup. Existing global/session editing and prompt injection remain the only behavior path; no hidden system prompt or OOBE override.

**Tech Stack:** Python, existing MemoryService/UnitOfWork (SQLite and in-memory), PySide6, pytest.

## Decisions
- Prefer a persisted editable memory over hardcoded system instructions (not user-editable) or a display-only template (not active by default).
- Install for new and existing vaults without replacing user entries. If the global scope is full, defer until a later startup with room rather than truncate data or block startup.
- Name the card “忠实的阅读者 · 内置预设”; use the existing editor and deletion confirmation. Explain editability in the existing editor-page hint.
- Do not promise unlimited recall, exclusive attachment, guaranteed permanence, unavailable actions or an exemption from safety boundaries. Explain tool intent and memory provenance only when grounded in actual actions/available context. Avoid formulaic diagnosis and crisis scripts without forbidding concrete help in immediate danger.

## Task 1 — Contract tests
- Create G:/LimboWave/tests/unit/test_memory_presets.py, reusing the dual-backend memory_stack fixture.
- Cover one-time installation, existing data preservation, edits/deletion across service recreation and SQLite reopen, encrypted persistence, capacity deferral, session override and next-run prompt behavior, and transaction rollback.
- Run targeted tests and confirm the missing initializer failure.

## Task 2 — Implementation
- Create G:/LimboWave/src/limbowave/domain/memory_presets.py with one source-controlled Chinese preset.
- Modify G:/LimboWave/src/limbowave/application/services/memory_service.py with an explicit transactional ensure_builtin_preset method and a receipt independent of the global list.
- Modify G:/LimboWave/src/limbowave/app.py to initialize off the GUI thread before attaching the service to the controller/tools.
- Modify G:/LimboWave/src/limbowave/ui/session_memory_panel.py only to explain that the built-in preset is editable/deletable.
- Run the contract tests.

## Task 3 — Integration and documentation
- Add an actual preset editing UI test in G:/LimboWave/tests/ui/test_memory_panel.py.
- Update G:/LimboWave/tests/ui/test_new_session_memory.py to verify wired initialization and first-turn injection alongside existing custom global memory.
- Document the preset, scope, persistence/receipt and capacity behavior in G:/LimboWave/docs/development/memory.md.
- Run memory service/runtime/UI/integration and startup regressions; run Ruff and mypy. Inspect the existing editor with the full preset if feasible.

## Verification results

- Confirmed the initial red phase: 19 preset contract cases failed on the missing initializer, then passed after implementation. Added two more cases protecting an existing preset identity without a receipt.
- Final combined memory service/runtime/preset, panel, first-message wiring, startup responsiveness and popup regression run: **190 passed**. Three runtime warnings originate from the concurrently added reminder scheduler (`to_thread` not awaited); no changes made to that feature.
- Real Pi + local mock-provider integration suite: **7 passed**, including both storage backends for the new preset edit/delete request test. The integration/preset combined run was 28 passed (21 preset + 7 Pi).
- Ruff passed for the preset module, memory service/panel and all affected memory tests. Focused mypy passed for the three source modules. Changed-file whitespace check passed.
- Full-project mypy was attempted: 21 errors in concurrent chat_view, memory_timeline and unrelated app wiring changes; left untouched. This is not a clean full-project type-check claim.
- Rendered six previews in G:/LimboWave/.tmp/reader-preset-preview. Inspected the dark card and the light 150% editor at the start/end of the full 1137-character preset. Explicitly loaded the installed Chinese font for the offscreen renderer; Chinese text, scrolling and Save/Cancel remain readable at 620 px width.
- Concurrent memory write-lock/snapshot integration in MemoryService was preserved, as were all unrelated in-progress repository changes. No migration, dependency, OOBE rewrite or hidden system-prompt injection was added by this feature.

## Follow-up — 2026-10-26

- Recheck the shared working tree after concurrent memory/runtime changes, without expanding the feature into timeline or reminder work.
- Add G:/LimboWave/tests/unit/test_memory_preset_backup.py: exercise real encrypted BackupService + staged RestoreService, verify keep/edit/delete choices, per-conversation customization and first-time initialization of an older archive without a preset receipt.
- Extend G:/LimboWave/tests/unit/test_memory_presets.py to verify concurrent initialization from multiple service instances produces one item and one successful installation.
- Run the complete related service/runtime/UI/startup/backup/restore/Pi suites, Ruff, focused mypy and the full-project type check. Record remaining unrelated failures separately.

### Wider regression finding and correction

The first expanded run finished with 269 passes and six failures: one source-inspection startup test and all five Qt restore lifecycle scenarios. Isolated restoration with preset initialization disabled passed, while the same scenario with the preset enabled emitted `当前内核不支持记忆上下文`. The shared FakeKernel inherited AgentKernel's intentionally rejecting fallback; it only worked previously because the global list was empty. Add a recording memory-context channel to G:/LimboWave/tests/unit/test_run_coordinator.py's FakeKernel, and assert the actual preset reaches the restore scenario's kernel in G:/LimboWave/tests/ui/test_restore_lifecycle.py. Keep the production rejection intact and cover it explicitly with UnsupportedMemoryKernel tests. The startup source-inspection test is re-run against the current file without changing its assertion.

The restore cancellation scenario also needed to acknowledge the concurrently added close guard asynchronously (QTimer), just as the real confirmation dialog does. Synchronous acceptance inside closeEvent caused a reentrant close to be ignored. The test now records subprocess output to a temporary file, preventing an inherited stdout pipe from making timeout cleanup hang. Only the verified stalled regression runner and its log-identified orphan storage workers were stopped; other application/test processes were left alone. The corrected cancellation scenario passed in isolation.

The startup AST test passes in a fresh standalone process. Its combined-run StopIteration occurs while other tasks edit app.py: inspect.getsource uses a function's imported line positions against a later on-disk file. Leave its assertion unchanged, run it separately, and exclude only that case from the final combined regression batch; do not count a deselection as a pass.

### Final follow-up verification

- Preset/backup-focused suite: **29 passed** (includes dual-backend concurrent initialization and explicit unsupported-kernel rejection).
- Final expanded service/runtime/active-memory/coordinator/backup/restore/UI/Pi batch: **334 passed, 1 deselected**, with three existing reminder-scheduler `to_thread` warnings. All five real Qt restore lifecycle scenarios passed with the preset present. The one deselected AST-based startup source-inspection case was run separately and **passed**; it is not counted among the 334.
- Ruff passed across the preset source, shared memory modules and every affected test, including the common FakeKernel and restore-lifecycle harness. Focused mypy passed for the three feature source modules. Whitespace checks passed.
- Latest full-project mypy: **2 errors in G:/LimboWave/src/limbowave/application/services/environment_service.py** (annotations for `daily` and `previous`), among 221 checked source files. Earlier concurrent timeline/companion type errors were no longer present at this check. Environment-service and reminder-scheduler changes remain outside this preset task.
- Follow-up production behavior is unchanged: the additional work is regression coverage, a compatible test kernel, asynchronous test consent and bounded subprocess-output handling. All new restore cases use temporary vaults, not the user's real library.
