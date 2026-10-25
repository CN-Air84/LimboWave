# Compact Advanced Composer Overlay Implementation Plan

**Goal:** Correct the previous interpretation: advanced settings cover the right half of the composer without reserving space, and retain animated opening and closing.

**Architecture:** Keep the composer as the only layout item. Mount a clipping viewport inside its right half and slide the existing toolbar inside it using a reversible progress animation. Compact padding and row heights instead of removing settings. Do not apply a parent opacity effect, which conflicts with the existing action-button effects.

**Tech Stack:** Python, PySide6, pytest-qt.

## Steps
1. Replace docked-region layout assertions with unchanged composer/transcript geometry, right-half overlap, bounded panel height and animated intermediate/reversal states.
2. Restore progress animation in ChatView and position a clipped child overlay on resize. Keep toolbar wiring and nested popup/Escape ownership in MainWindow.
3. Compact SessionToolbar margins, gaps and control heights, preserving export and compression confirmation.
4. Run targeted regression, lint and type checks. Render and inspect wide/narrow, dark/light, empty and animation midpoint examples.

## Acceptance
- The panel covers the composer right half; it never sits beside or below the input and never shrinks the input or moves messages.
- At default font size the settings panel is at most 160px tall and fits inside the composer.
- Both directions animate; rapid reversal continues from the current position, with no click-through at visible settings.
- All settings and nested picker behavior remain functional; typing on the uncovered input does not dismiss settings.
- Preserve other ongoing edits; no automatic commits.

Supersedes the space-reserving design in 2026-10-16-advanced-docked-region.md.


## Verification (2026-10-17)
- Default-font rendered height reduced from approximately 247px to 149px, retaining every action; export moved into the header.
- Focused overlay/window/toolbar suite: 83 passed. Includes intermediate animation progress, rapid reversal, right-half hit testing, larger fonts, native/nested pickers and explicit dismissal.
- Related UI regression: 297 passed, 1 skipped (offscreen font availability). No full-suite claim.
- Ruff and targeted mypy passed; touched-file whitespace checks passed.
- Rendered and inspected wide/narrow, empty, dark/light and animation-midpoint images; saved a GIF from actual widget renders in `.tmp/advanced-composer-overlay/motion.gif`.
