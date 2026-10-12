# Settings page motion implementation plan

**Goal:** Make settings entry/exit visually quiet, directional and continuous without changing settings layout or login choreography.

**Architecture:** Use two bounded snapshots for normal full-page transitions, painted on an opaque theme surface with staggered fades and small horizontal offsets. Keep live widgets stationary. Capture the current composite before interrupted navigation, then start the next transition from that exact frame. Preserve the legacy login/scroll transition path.

**Tech Stack:** Python, PySide6 QPainter/QPropertyAnimation, pytest-qt.

## Steps
1. Add deterministic frame and lifecycle regressions in `tests/ui/test_page_transition.py`: exact endpoints, restrained text overlap, direction, bounded capture cost, interruption, resize, retained pages, and login isolation. Run before implementation to establish failures.
2. Update `src/limbowave/ui/main_window.py`: opt-in layered painting for non-login transitions, 280 ms entry / 210 ms exit, frame continuity on interruption, and unchanged live geometry. Keep the existing 230 ms deferred sidebar refresh outside the exit interval.
3. Run new tests plus main-window/settings/login/animated-stack regressions, lint and type checks. Render actual settings opening/closing frames in dark and light themes for visual inspection using temporary configuration only.

## Acceptance
- First/last frames match the outgoing/incoming pages; no transparent edge or full-strength destination leaking through.
- Incoming text appears after outgoing text has mostly faded; motion stays small even in wide windows.
- No live-page movement or per-frame page capture, and both snapshots retain the existing pixel budget.
- Reversing direction starts from the currently displayed composite; resizing safely settles to the live destination.
- Login exit, retained settings state and unrelated user edits are preserved.

## Verification results
- 17 new deterministic motion tests passed, including rapid reversal, nested settings-tab interruption, opaque endpoints, high-DPI capture limits, resize recovery and 16 ms animation ticks.
- 104 existing relevant tests passed in isolated groups: 18 main-window cases and 86 settings/login/blackout/animated-stack/startup cases. Lint, strict type checking of the changed source and whitespace checks passed.
- Removed only the layered transition's redundant frame-rate gate after a regression demonstrated that it discarded alternating 16 ms ticks. Legacy login transitions retain their original path.
- Reviewed actual settings entry/exit keyframes in the built-in dark and light themes, rendered with disposable test configuration under `.var/settings-motion-review/`.
- Existing test limitations were reproduced by loading the committed, pre-change main-window module in a separate process without replacing workspace files: the thinking-popup theme test fails, and the older high-DPI palette test is sensitive to stylesheet state when run after the complete login/startup group. These unrelated behaviors were not changed.
