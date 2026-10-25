# Advanced Settings Popover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the side-by-side advanced settings patch with an anchored popover without moving or resizing the composer.

**Architecture:** Keep SessionToolbar and its existing intent signals; position it as a content-host overlay above the Advanced trigger. Remove side-panel reservations from ChatView. Reuse the shadow and short progress animation, with full-width vertical motion rather than horizontal unfolding. Keep all existing route, thinking, compression confirmation and export behavior.

**Tech Stack:** Python, PySide6, pytest-qt, existing theme tokens and inline QSS.

---

### Task 1: Lock layout and interaction requirements
**Files:** tests/ui/test_main_window.py, tests/ui/test_chat_view.py, tests/ui/test_session_toolbar.py.
1. Replace side-by-side expectations with unchanged composer geometry at wide and narrow widths, before and after opening.
2. Check top anchoring, bounds, resize, click-away, Escape/focus return and nested popup ownership.
3. Verify no automatic opening after messages/history loading and no transcript reflow.
4. Run targeted tests with QT_QPA_PLATFORM=offscreen; new behavior should fail before implementation.

### Task 2: Implement overlay and quiet controls
**Files:** src/limbowave/ui/chat_view.py, src/limbowave/ui/main_window.py, src/limbowave/ui/session_toolbar.py.
1. Remove all reserved advanced-panel width and composer shifting.
2. Position the natural-height toolbar above the trigger, clamp within host and fall below only when needed; keep full-width shadow throughout motion.
3. Add outside-click/Escape dismissal without swallowing other clicks or closing owned combo/route popups. Cancel pending compression on dismissal.
4. Use a 320px panel, 16px padding, aligned 64px labels, 12px secondary labels (scaled with app settings), 5% filled selectors without the arrow divider, subtle action tiles and a quiet export row.
5. Refresh inline styles when appearance changes; preserve unrelated in-progress changes.

### Task 3: Verify and review
1. Run focused UI tests, then the full UI suite and targeted appearance tests.
2. Run ruff on changed files and inspect the diff.
3. Render local dark/light, narrow and expanded/collapsed preview images and visually inspect them.
4. Report results and any unrelated pre-existing failures. Do not auto-commit the user's worktree.
