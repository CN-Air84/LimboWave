# AI Conversation Rename Implementation Plan

**Goal:** Add an AI rename button to the conversation rename floating panel, using every visible turn of the target branch rather than only the first exchange.

**Architecture:** Keep the existing isolated title-generation request, model routing, title normalization and first-response behavior. Extend the service with a full-message entry point. Add an opt-in asynchronous suggestion action to the existing prompt; generated names remain drafts until confirmed.

**Tech Stack:** Python, PySide6, asyncio/qasync, pytest/pytest-qt.

## 1. Shared title generation
- Modify `src/limbowave/application/services/conversation_title_service.py`.
- Test `tests/unit/test_conversation_title_service.py`: chronological multi-turn context, long messages, no reasoning/tool content, empty history, existing isolation behavior.
- Preserve the first-round API; both entry points share isolated execution and normalization.

## 2. Rename prompt and wiring
- Modify `src/limbowave/ui/floating.py` and `src/limbowave/app.py`.
- Add optional AI action, pending feedback, duplicate-request prevention, failure/retry, cancellation on close/destruction. Block submission while pending; fill but never automatically save.
- Read target history asynchronously. Prefer its displayed/active branch (including ancestor messages); otherwise resolve the target conversation's current branch. Never read another conversation's live context.
- Reuse current model/site routing and isolate the generation from the active run.

## 3. Verification
- Create `tests/ui/test_conversation_rename.py`: prompt interactions and actual app callback with real branch history and fake isolated kernel.
- Run new tests red before implementation, then run title, floating-panel, history and branch regression tests plus lint/type checks.
- Visually inspect the floating panel at idle/pending/success states.
- Preserve pre-existing worktree changes; do not commit unrelated work.

## Verification results
- 157 focused regression tests passed (title service, rename interactions, floating panels, history, branches, sidebar, model routing, and navigation during generation).
- Ruff passed for all touched Python files; mypy passed for the three modified source files.
- Inspected dark/light idle, pending, and success renders: action labels and controls fit, generated titles remain editable before confirmation.
- Added shutdown tracking and idempotent close cancellation so temporary title requests finish cleanup before history storage closes.
- Provider calls were simulated; no live model request or user conversation content was sent during verification.
