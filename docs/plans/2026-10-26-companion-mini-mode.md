# Companion Mini-Mode Implementation Plan

**Goal:** Add a low-interruption desktop companion without creating a second conversation or send path.

**Architecture:** A lazily-created native Qt window projects the active ChatView's last three rows and shares its QTextDocument. A presentation-only controller delegates submission, queueing and stopping to the existing ChatView. MainWindow retains geometry and all business wiring. Existing concurrent workspace edits must remain intact.

**Tech Stack:** Python 3.12+, PySide6, pytest-qt; optional Windows RegisterHotKey adapter.

## Design / scope
- Native, resizable 420×480 mini window, with a compact collapsed bar, restore-main button and options menu. Reuse theme tokens, plain text previews, keyboard focus and readable opaque defaults.
- Alternatives considered: shrinking the main workspace would inherit its minimum widths and complex layout; a second full ChatView would duplicate run state. Use a separate thin view instead. Edge docking, tray and startup integration are deferred.
- Share the current live or history-preview draft document; never persist or copy chat content into settings. Send/queue/stop use the existing command path. Show attachments, edit state, loading/errors and queue warnings instead of silently dropping context.
- Ctrl+Shift+M works in the application. Optional configurable Windows global shortcut (default Ctrl+Alt+Space) starts on first use; conflicts degrade to button/local shortcut with a visible status. No global support is claimed on other platforms.
- Closing mini restores main, Esc collapses it, background deltas never activate it. Full-page/lock navigation hides and clears mini projection. Approved app close tears down mini and unregisters hotkey; a refused close does neither.
- Recover window geometry into a visible screen work area; test disconnected/negative-coordinate screens using deterministic rectangles.

## Task 1 — Regression contract
Create `G:/LimboWave/tests/ui/test_companion_mode.py` covering shared draft/undo, exactly-once send/queue/stop, history preview switching, collapsed and close behavior, full-page privacy, loading/errors/attachments, theme and bounded previews. Run before implementation (expected failure: companion integration absent).

## Task 2 — Thin mini view and controller
Create `G:/LimboWave/src/limbowave/ui/companion_window.py` and `G:/LimboWave/src/limbowave/ui/companion_mode.py`. Add narrowly-scoped hooks to `G:/LimboWave/src/limbowave/ui/main_window.py` for initialization, preview rebinding, full-page suspension and successful close cleanup. Do not alter business services or the existing composer.

## Task 3 — Platform shortcut
Create `G:/LimboWave/src/limbowave/ui/companion_hotkey.py` and `G:/LimboWave/tests/unit/test_companion_hotkey.py`. Use native API only on the Windows desktop Qt backend; offscreen tests use fakes. Validate single modified keystrokes, conflicts and unregister lifecycle. Configure via mini options menu; preferences are session-scoped and labeled as such.

## Task 4 — Verify and document
Run targeted companion, main-window, composer/drafts/queue, permission, close-guard and Zen regressions, then broader UI/unit tests if feasible. Run ruff and mypy on changed modules. Render light/dark/compact offscreen screenshots to `G:/LimboWave/.tmp/companion-qa/` and inspect visually. Document controls and explicit platform/first-version limits in `G:/LimboWave/docs/development/companion-mode.md`.

## Completion — 2026-10-26

- Implemented the independent mini window, collapsed bar, shared document, bounded live preview (including hidden stream buffers), existing send/queue/stop delegation, reversible geometry, optional opacity/pinning and configurable session-only global shortcut.
- Added privacy/close-guard/approval recovery and monitor work-area fallback. Existing unrelated workspace edits were preserved.
- Final targeted result: 35 passed. Related regression result: 322 passed (overlapping coverage, not additive). Ruff and scoped mypy passed. Native Windows hotkey register/dispatch/unregister smoke passed.
- Light/dark and collapsed screenshots were rendered and visually inspected. Docs: `G:/LimboWave/docs/development/companion-mode.md`.
- Deferred: edge docking, tray/startup integration, persistent window preferences, non-Windows global hotkeys. Physical DPI/monitor transitions and real IME candidate-window interactions remain manual checks.

## Follow-up — user bubbles and attachments (2026-10-26)

Use the writing-plans workflow for this extension. Keep edits limited to companion modules/tests/docs; preserve parallel workspace edits.

1. Regression-first in `G:/LimboWave/tests/ui/test_companion_attachments.py`: user bubbles right-aligned/content-sized and no speaker-card header; choose files with a mini-parented dialog; clipboard file/image and text cursor semantics; local drop; live removable attachment strip; unavailable/history/loading gates and stale dialog selection.
2. `G:/LimboWave/src/limbowave/ui/companion_window.py`: role-specific message rows using existing theme user bubble tokens; attachment button/AttachmentBar; media paste signal and local-file drop handling. No second upload service.
3. `G:/LimboWave/src/limbowave/ui/companion_mode.py`: delegate media paste to the original composer input, files to ChatView.files_dropped, removals to the original AttachmentBar. Revalidate source/generation after the file dialog. Reflect current attachments with a bounded horizontal strip and preserve source drafts.
4. Add real app-wiring tests that import and send PNG/document attachments through the mini window, retaining byte payload and not attaching stale imports to another draft.
5. Run companion startup/runtime/attachment tests and composer regressions; ruff/mypy; render/inspect dark/light screenshots and update usage docs. No release packaging or forced app restart.

Follow-up complete: user messages use right-aligned content-sized bubbles; message-author prefixes and the in-window title/key-hint labels are removed. Top and bottom actions are overlay controls without dedicated toolbar rows. File selection, clipboard files/images, local drag/drop and removable attachment drafts use the original application pipeline. Validation: 53 companion cases and 52 related regressions passed; ruff/scoped mypy passed. Native window behavior is unchanged; this is not a new frameless window implementation.
