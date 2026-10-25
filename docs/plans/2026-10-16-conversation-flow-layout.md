# Conversation Flow Layout Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep desktop conversations in a centered 800px reading column, remove assistant card chrome, and make Markdown lists readable.

**Architecture:** Preserve ChatView message/run grouping and all actions. Use a transparent, unpadded assistant container, exact viewport-based column sizing, and Qt-supported list typography rather than browser-only CSS. Preserve existing uncommitted changes.

**Tech Stack:** Python, PySide6, markdown-it-py, pytest-qt.

---

### Task 1: Lock presentation expectations
- Modify `tests/ui/test_chat_view.py`: replace assistant panel expectations with transparent, borderless, unpadded presentation and aligned actions; test odd/even desktop widths and resizing.
- Modify `tests/unit/test_theme_and_preferences.py`: verify transparent assistant styling in both palettes while keeping user/error styling.
- Modify `tests/ui/test_markdown_render.py`: verify 160% line height in tight/loose lists, preserved nesting, ordered start numbers, and unchanged code blocks.
- Run these focused tests and confirm the old implementation fails the new expectations.

### Task 2: Implement layout and typography
- Modify `src/limbowave/ui/theme.py`: assistant background transparent, border none, radius zero.
- Modify `src/limbowave/ui/chat_view.py`: remove assistant padding; retain content/action spacing; cap the column at exactly 800px with at most 1px asymmetric gutters on odd viewports.
- Modify `src/limbowave/ui/markdown_render.py`: annotate list tokens and list paragraphs using Qt-supported margins and line-height, preserving actual nested indentation and ordered-list attributes.

### Task 3: Verify
- Run `.venv/Scripts/python -m pytest tests/ui/test_chat_view.py tests/ui/test_markdown_render.py tests/unit/test_theme_and_preferences.py` with offscreen Qt.
- Run nearby streaming, history, and tool/action UI tests and lint changed code.
- Render local offscreen desktop previews in light/dark palettes, wide/narrow windows, and check message geometry and list spacing.
- No unrelated rewrites, automatic commits, or changes to the separate mobile web client.

## Verification notes
- New presentation expectations: 11 failures reproduced before implementation; all 15 targeted checks passed after implementation.
- Ruff passed for all six touched source/test files; mypy passed for the Markdown renderer.
- Offscreen previews checked at 1401px in both palettes and 600px in dark mode. Actual message widths: 800px and 536px; assistant text and actions share the same left edge.
- Extended 286-test run: 283 passed / 3 tool-visibility animation timing failures with fallback fonts; using Windows fonts: 285 passed / 1 tool resize-animation failure. Legacy assistant padding and frame were restored at runtime (without rewriting workspace files) for comparison; animation endpoint failures also reproduced with the old panel (74 passed / 2 failures). Tool animation code/tests were left unchanged rather than weakening assertions or expanding this visual change into an unrelated timing fix.
- Existing concurrent composer/theme work in the checkout was preserved; no commit or release package was created.
- Final focused run: all 156 chat layout, Markdown rendering, and theme/preferences tests passed (Windows fonts, offscreen Qt).
