# Sidebar Visual Hierarchy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove competing blue accents from the desktop sidebar and make Settings a full-width navigation action.

**Architecture:** Keep the existing PySide6 sidebar and its intent signals. Scope neutral interaction styles to named sidebar controls and the workspace splitter; preserve other lists, primary buttons, theme customization, and existing uncommitted work.

**Tech Stack:** Python, PySide6/QSS, pytest-qt.

---

## Design

Choose the requested ghost-button direction rather than retaining a saturated New Conversation button. Use theme-text overlays at 8% for the button and selected rows, 4% for row hover, and 12% for stronger button hover. This follows light, dark, and wallpaper-backed themes without introducing another blue. Remove the active conversation rail and native item focus rectangle; retain the active title weight, branch status, keyboard navigation, and visible selected-row fill and neutral button focus feedback. Settings is a left-aligned, 40px-tall full-width row with an 18px theme-aware gear. The workspace divider is a quiet 1px line, stronger only on hover.

## Task 1: Regression coverage

- Modify `tests/ui/test_sidebar.py`: replace the obsolete moving-rail requirement with selection-without-rail coverage; keep all behavior tests.
- Create `tests/ui/test_sidebar_appearance.py`: cover scoped colors across palettes, keyboard focus, settings icon/alignment/width and whole-row click, theme refresh, and splitter styling.
- Run the new tests before implementation and confirm failures reflect the requested changes.

## Task 2: Sidebar controls and selection

- Modify `src/limbowave/ui/sidebar.py`: name controls, replace the accent property, add the settings gear and row sizing, remove the rail/animation-only wiring, and suppress the native delegate focus rectangle while keeping active rows softly selected.
- Modify `src/limbowave/ui/theme.py`: add scoped neutral button/list styles with hover, pressed, disabled, and keyboard-focus states. Do not change global primary-action styles.
- Run sidebar behavior and new appearance tests.

## Task 3: Workspace boundary and verification

- Modify `src/limbowave/ui/main_window.py`: name the workspace splitter; scope the quiet divider rules in `theme.py` to it.
- Run sidebar, main-window, theme/preferences, theme-effects, and backdrop-related tests plus lint on touched files.
- Render local offscreen dark/light sidebar previews, inspect pixels/layout, and verify full-width settings and readable states. No live user data or running application is modified by the preview.
- Report changed behavior and any verification limitations; do not commit unrelated pre-existing changes.
