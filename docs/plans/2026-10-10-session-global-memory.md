# Session-scoped Global Memory Customization Implementation Plan

**Goal:** Let users edit or remove inherited global memories for one conversation without changing the global originals or any other conversation.

**Architecture:** Store an encrypted `global-overrides:<conversation_id>` document in the existing memory repository. Its map contains global item IDs mapped to replacement text or null (removed for this conversation). Resolve it against current global items when listing or building run context; no full copy or schema migration. All branches of one conversation share the overlay. Deleted global originals disappear everywhere; untouched items keep inheriting future global changes.

**Tech Stack:** Python, existing UnitOfWork repositories (SQLite/in-memory), PySide6, pytest/pytest-qt.

## Task 1: Service contract and persistence
- Modify `src/limbowave/domain/memory.py` with a typed view of inherited/modified/deleted global entries.
- Add dual-backend tests to `tests/unit/test_memory_service.py` for edit/delete/restore, cross-conversation and branch isolation, invalid targets, size constraints, encrypted reopen and cascade cleanup.
- Run new tests first and confirm the missing API failures.
- Modify `src/limbowave/application/services/memory_service.py`: add conversation validation, overlay CRUD and effective-memory resolution. Read global and branch prompt content within one transaction. Preserve existing global CRUD and model-only branch write restrictions.
- Re-run service tests.

## Task 2: Session memory UI
- Modify `src/limbowave/ui/session_memory_panel.py`: add a session-only scope selector inside the existing editor page. Reuse the card list and modal editor, but capture scope when opening editors/confirmations. Add visible inheritance/customization/removal labels and a restore button. Hide global addition and promotion in customization mode.
- Add tests in `tests/ui/test_memory_panel.py` for scope switching, localized edit/delete/restore, cancellation, editor target stability, empty/error states and unchanged global settings UI.
- Keep removed entries visible for recovery, but never inject them into model context.

## Task 3: Runtime verification and documentation
- Extend `tests/unit/test_memory_runtime.py` to verify the coordinator injects only the current conversation's effective global memories, including after switching conversation or branching. Active runs keep their existing memory snapshot; the next run reads updates.
- Update `docs/development/memory.md` with UI entry point, inheritance rules, branch semantics, recovery and encrypted persistence.
- Run targeted tests, broader unit/UI regression tests, lint and type checks. Render session customization in representative widths for layout verification where feasible.

## Acceptance criteria
1. Editing/deleting an inherited global item changes only the selected conversation's effective prompt.
2. Global originals, other conversations, and branch memories remain unchanged.
3. Customization survives restart, stays encrypted and is removed when its conversation is deleted.
4. Users can distinguish inherited, modified and removed items and restore inheritance explicitly.
5. Failed/cancelled operations do not silently modify any scope.

## Alternatives considered
- Copy all global memories into a conversation: rejected because future global updates would stop propagating.
- Convert edited entries to ordinary branch memory: rejected because this duplicates inherited content and has the wrong branch-level scope.
- Conversation overlay: selected for explicit isolation and compatibility with existing persistence.


## Verification results

- Memory service/runtime tests: 86 passed.
- Memory panel tests, including stale asynchronous reloads: 59 passed.
- Real Pi memory integration tests with a local mock provider: 5 passed.
- Ruff on changed Python files: passed. Full-project mypy: 198 source files passed.
- Rendered and inspected the customization view in dark mode (620 px) and light mode with enlarged fonts (420 px); Chinese glyphs, controls and scrolling remain usable.
- Broader unit suite: 1690 passed, 6 skipped, 2 failures outside this change (compression marker ordering and hardcoded font sizes in the onboarding demo).
- Related UI regression suite (memory, toolbar, conversation switching, settings and main window): 227 passed, 3 failures outside this change (settings navigation hover colors and thinking popup theme background). Those failures also reproduce in isolation; their implementations were not modified by this task.
- Existing backup/restore changes in the workspace were preserved. No database migration or model tool permission expansion was needed.


## Follow-up: open memory before the first message

The old application entry point refused to open memory while the new conversation had no persisted identity. Fix this by lazily persisting an empty conversation and branch when opening memory, rather than sending a dummy message or globally pre-creating every new chat. Route persistence through the existing process worker, arbitrate competing commands, preserve drafts and first-response title behavior, and keep the current scope unchanged on failure. Cover the real wired button, first-turn memory injection, repeated opening, new-conversation isolation, storage rollback and process-backed persistence.


### First-message entry-point fix verification
- 176 coordinator/controller/memory/branch/process tests passed.
- 83 memory-entry, memory-panel, conversation-switch and draft UI tests passed.
- Full-project mypy passed (198 source files); changed coordinator/process/test modules passed Ruff.
- The real button is tested both with and without a configured model. Tests cover no synthetic messages, retained draft text, first-turn customized memory, unchanged first-response title events, worker-backed creation, duplicate-command arbitration and clean isolation after creating another chat.


## Follow-up: temporary windows and mouse-driven dropdown dismissal

Reproduced both reported regressions with Qt show-event observation and actual mouse events: the scope combo, scope hint and promote button were shown while still unparented (three transient native windows), and QWidget.isAncestorOf did not treat the combo's Qt.Popup as part of the floating panel. Parent controls before visibility changes and walk the full parentWidget ownership chain for outside-click dismissal. Keep unrelated popups dismissing the panel, preserve Escape handling, and test nested menus and successful editing after a real mouse selection. Do not rely on setCurrentIndex-only coverage for popup interactions.


### Popup interaction fix verification
- Reproduced three transient top-level widgets during session-panel construction before the fix; the show-event observer now sees none.
- Reproduced mouse dropdown selection closing the parent before the fix; actual mouse movement/press/release now selects global memory, opens the editor, and saves a conversation-only override without closing the parent.
- Also prevent blank-card mouse presses from bubbling into false outside-click dismissal, and reuse the active memory panel on repeated open requests.
- Focused popup and memory-entry tests passed. The expanded UI run passed 211 tests; two existing material-toggle rendering cases failed and also raised teardown slot errors. Re-running those cases with the original HEAD floating-panel implementation in memory reproduced the same failures without modifying workspace files.
- Ruff on changed popup/memory/test files passed; full-project mypy passed (198 source files).
