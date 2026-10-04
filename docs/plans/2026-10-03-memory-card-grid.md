# Memory Card Grid Implementation Plan

**Goal:** Replace the memory editor row list with a vertically scrolling two-column card grid, using each memory’s first line as its title.

**Architecture:** Keep the existing QListWidget model, selection, floating editor, and MemoryService workflow. Add a responsive icon-mode list and a theme-token-based card delegate; preserve complete content in the existing item role and tooltip. Reuse the shared panel for global and branch memories.

**Tech Stack:** Python, PySide6, pytest-qt.

## Steps
1. Add regression tests in tests/ui/test_memory_panel.py for exact two-column geometry, resize/scroll behavior, full first-line titles, and keyboard/edit interactions. Run the new tests to confirm the current row list fails.
2. Add src/limbowave/ui/memory_card_list.py with equal-width, font-aware cards, vertical scrolling, plain-text titles/previews, and an empty state. Wire it into session_memory_panel.py without changing persistence.
3. Run focused memory and adjacent settings/floating-panel tests, lint and type checks; render light/dark populated and empty states for visual verification.

## Acceptance
- Exactly two cards per row with no horizontal scrolling after resize.
- First physical line supplies the complete title (visual overflow is elided); subsequent text is only a preview.
- Selection, double-click/keyboard editing, add/delete/promotion, and full-body preservation still work.
- No changes to existing unrelated working-tree edits.
