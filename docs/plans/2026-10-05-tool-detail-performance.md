# Tool Detail Expansion Performance Implementation Plan

**Goal:** Smooth individual tool-detail expansion/collapse, including long wrapped arguments and concurrent updates.

**Architecture:** Keep the existing reversible clip animation and audit contract. Remove layout-request feedback, cache text measurements across the widths Qt probes, and use QLabel's retained selectable-text layout instead of re-laying out long plain text on every paint.

**Tech Stack:** PySide6 / Python / pytest-qt.

## Decisions
- Shortening/removing the animation would hide the symptom; bitmap snapshots add invalidation and memory cost. Prefer retained text layout and bounded geometry work while preserving existing controls and interaction.
- Widgets stay on the GUI thread; the previous backend projection remains unchanged.
- Content/font/width changes must invalidate the relevant caches. A hidden detail should not format or measure long text during unrelated layout passes.

## Steps
1. Add deterministic measurement-budget and lifecycle tests in tests/ui/test_tool_detail_performance.py; verify failures before changes.
2. Modify only src/limbowave/ui/tool_steps.py; preserve rapid reversal, independent panels, live updates, resize, global visibility and no-animation fallback.
3. Run tool/detail/history-related UI regressions, native Qt checks, Ruff and Mypy. Profile identical 12-step/long-detail workloads before/after; visually inspect transition states.

Existing uncommitted changes remain in place; no automatic commits or new worktrees.

## Outcome
Implemented in tool_steps.py; 161 related UI tests and 48 native Qt tests passed. Detailed measurements and verification limits are recorded in docs/audits/2026-10-05-tool-detail-performance.md.
