# Single-row Advanced Selectors

**Goal:** Put actual endpoint and thinking level on one row, each occupying half the width, without changing the composer overlay or its animation.

**Architecture:** Reuse existing controls, popups and signals. Put two equally weighted field layouts into one horizontal row. Use concise visible labels (站点 / 思考), full accessible names and existing tooltips. Allow values to shrink and elide without letting long endpoint names change the split.

**Tech Stack:** Python, PySide6, pytest-qt.

## Steps
1. Test equal half-width fields, common baseline, compact height and containment at narrow/wide widths and larger fonts.
2. Update only the toolbar selector row, label widths and horizontal control padding; keep the endpoint expansion popup and thinking choices unchanged.
3. Run toolbar/overlay/picker/theme tests and animation regression. Render dark/light and narrow examples for review.

## Acceptance
- Both selectors occupy one row with equal field widths (at most one pixel rounding difference).
- Default toolbar height drops by one selector row (target <= 125px).
- Long endpoint names remain available via tooltip and expansion popup without widening either half.
- Composer overlap, closing behavior and reversible animation are unchanged.
- Preserve other in-progress changes; do not commit.

## Verification (2026-10-17)
- Default-font rendered height: 117px (previously 149px); both fields share one row and equally sized halves at 218/262/320/400px widths.
- Layout checks include 100% and 160% font scales, long endpoint names and full accessible names/tooltips.
- Related selector/toolbar/overlay/window/popup regression: 151 passed.
- Stabilized the existing animation reversal test by waiting for docking completion and applying pending layout before recording its geometry; the production animation implementation is unchanged.
- Ruff, targeted mypy and changed-file whitespace checks passed.
- Visually inspected dark/light and narrow rendered details under `.tmp/advanced-single-row/`.
