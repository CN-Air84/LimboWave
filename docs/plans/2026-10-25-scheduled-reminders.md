# Scheduled Reminders Implementation Plan

**Goal:** Give the model real, persistent, conversation-scoped one-shot reminders without pretending it can perceive time or act while the app is closed.

**Architecture:** The existing tool gateway delegates to a Qt-free reminder service. Encrypted SQLite records retain UTC deadlines and original timezone offsets. A cancellable application scheduler atomically changes a pending reminder to delivered and adds a clearly labeled system-generated conversation message; UI and web receive a separate scoped event, never assistant streaming events.

**Tech Stack:** Existing Python, SQLite/unit-of-work, asyncio/qasync, PySide6, Pi TypeScript tool proxy. No new dependency.

## Decision and boundaries

- Prefer persisted in-app scheduling over volatile timers (lost on restart) or an OS service (installation, vault unlock, and platform complexity).
- One-shot `set_reminder(target_time, reason)`; also support `delay_seconds` instead of target_time for exact relative delays. Absolute times must include a UTC offset; reject ambiguous, past, non-finite and malformed values.
- `list_reminders` and `cancel_reminder` see only the trusted active conversation, never a model-supplied owner. Bind delivery to the original branch. Deleted scopes cascade away.
- Creating a reminder uses a dedicated permission capability and the existing approval policy; reading/cancelling this conversation's reminders is low-risk. Web-origin tool denial remains unchanged.
- Only unlocked persistent storage enables reminders. The app must remain running; shutdown/sleep gaps are caught up after resume/unlock. No OS wake, automatic model call, shell execution, or recurring schedules.
- Reason text is encrypted. Delivery state and message commit together; conditional state updates prevent cancellation/delivery races and duplicate messages. Poll in small batches off the UI thread and retry transient failures without logging private text.

## Steps

1. Add service tests for parsing, relative time, ownership, listing, cancellation, due delivery, restart catch-up, encryption, rollback and concurrent delivery. Run to confirm missing implementation.
2. Add immutable reminder model, repository port, SQLite migration/repository and in-memory repository; implement service and atomic delivery. Run focused tests.
3. Register Python/Pi tools; validate before approval, inject trusted scope, enforce origin and capability boundaries. Test manifest parity, gateway calls and permissions.
4. Add cancellable scheduler; wire startup independent of model availability and stop before storage close/reset. Emit scoped notifications without touching active model runs. Add desktop append and web event/invalidation support.
5. Add scheduler and UI/integration regression coverage. Run targeted tests, ruff, mypy, and broader unit/integration tests; document limitations and verification results.

## Verification

Use `.venv/Scripts/python.exe -m pytest` for tests, `.venv/Scripts/ruff.exe check` for touched Python files, and `.venv/Scripts/mypy.exe src` for static typing. Exercise two independent SQLite service instances at the same deadline. UI tests must prove streaming rows and the selected conversation are not hijacked by reminders.

## Verification results — 2026-10-26

- Related Python regression suite: **550 passed, 1 skipped**, including real Pi tool registration, SQLite storage/migration, reminder permissions, web-origin denial, scheduler shutdown, cancellation races and UI streaming isolation.
- Web: **52 passed** across 12 files; TypeScript `tsc --noEmit` passed.
- Reminder domain/service/scheduler/SQLite adapter: strict mypy passed. Reminder-related Python files and tests passed ruff.
- Rendered the actual ChatView and inspected a local screenshot. Caught and fixed the initially empty assistant-style notice card by finalizing its text; the UI regression now asserts rendered content as well as stream ownership.
- Full-repository collection is independently blocked by the existing missing `scripts.package_server` import. Full-source mypy also reports errors in unrelated, concurrently edited UI modules; those changes were not overwritten.
- No production vault was modified and no installer was rebuilt. Restart the source application to load the new tools and scheduler.
