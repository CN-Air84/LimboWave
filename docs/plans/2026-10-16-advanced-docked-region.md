# Docked Advanced Settings Implementation Plan

**Goal:** Replace the advanced-settings overlay with a persistent region beside the composer.

**Architecture:** ChatView owns the toolbar as a second item in its composer layout. At comfortable widths the composer and 320px toolbar sit side by side, bottom aligned; narrow workspaces stack them without covering messages. MainWindow retains intent wiring and Escape handling, but removes click-away dismissal and overlay positioning. Existing settings and nested pickers remain unchanged.

**Tech Stack:** Python, PySide6, pytest-qt.

## Steps
1. Replace popover-specific expectations in tests/ui/test_advanced_popover.py and tests/ui/test_main_window.py with fixed-region layout and persistent-focus behavior. Keep nested popup, compression cancellation and opt-in tests.
2. Extend _ComposerRowLayout in src/limbowave/ui/chat_view.py to lay out the optional panel, reserve its natural height, center the combined empty-state input region and retain the 800px composer cap.
3. In src/limbowave/ui/main_window.py, mount the toolbar into the composer layout; remove floating placement, shadow and click-away behavior. Keep explicit toggles and Escape; preserve history/settings transitions.
4. Update toolbar descriptions; run focused and broader UI tests plus lint/type checks. Render wide/narrow, dark/light examples and inspect the resulting layouts.

## Acceptance
- Advanced settings are in normal layout, right of the composer on wide windows and below on narrow windows, never overlapping composer or transcript.
- Typing, clicking the transcript and changing sidebar focus do not dismiss the region.
- Toggle/close/Escape hide it, clear pending compression confirmation and return keyboard focus appropriately. Nested pickers consume Escape first.
- Opening does not resize the sidebar or change transcript wrapping. Closing restores the centered composer. Messages/history never auto-open the region.
- Preserve unrelated existing work; do not commit the dirty workspace.

## Verification (2026-10-16)
- Focused region/window/toolbar tests: 76 passed.
- Related UI regression (chat, region, toolbar, pickers, background/materials, compression, conversation layout): 290 passed, 1 skipped (`offscreen Qt has no fonts`).
- Ruff passed for all six touched Python files; targeted mypy passed for the three source files; targeted diff whitespace checks passed.
- Rendered and inspected dark/light, narrow and empty-session layouts with explicitly loaded Windows fonts.
- An attempted full `tests/ui` run stopped making progress after its 20% line (around the conversation-renaming group) and was terminated after several minutes. This is not counted as a full-suite pass; the bounded related regression above completed normally.
