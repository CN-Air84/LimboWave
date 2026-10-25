# Endpoint Panel Toggle Implementation Plan

**Goal:** Clicking the advanced settings actual-endpoint button a second time closes its panel instead of reopening it.

**Architecture:** Keep the existing FloatingPanel lifecycle and dropdown cleanup. Opt the endpoint trigger into anchor-toggle ownership so mouse press does not dismiss the panel before the button click toggles it. Other floating panels retain their existing outside-click behavior.

**Tech Stack:** Python, PySide6, pytest-qt.

### 1. Reproduce
- Create `tests/ui/test_endpoint_panel_toggle.py`.
- Cover real mouse clicks, keyboard activation, programmatic clicks, opening animation, closing/reopening and native dropdown cleanup.
- Run the new tests before changing production code and confirm the second activation leaves a panel open.

### 2. Fix
- Update `src/limbowave/ui/floating.py` with opt-in toggle-anchor handling in `popup` and the outside-click filter.
- Update `src/limbowave/ui/session_toolbar.py` to close an existing endpoint panel and return; preserve stale-wrapper recovery and dropdown cleanup.
- Leave unrelated in-progress changes intact. Do not auto-commit.

### 3. Verify
- Run `tests/ui/test_endpoint_panel_toggle.py`, `tests/ui/test_advanced_popover.py`, `tests/ui/test_session_toolbar.py`, and floating-panel tests with `QT_QPA_PLATFORM=offscreen`.
- Check outside-click, Escape and nested dropdown behavior, then run the broader UI suite.
- Run Ruff on changed Python files and inspect the final patch.

### 4. Resumed verification (2026-10-16)

- Retained the previous toggle implementation and all unrelated working-tree changes.
- Added a press/release regression: pressing the trigger must preserve the current panel;
  releasing it closes the panel once. This fails against the pre-fix snapshots and passes
  with the toggle fix.
- Made stale-reference recovery deterministic by delivering the owner's deferred deletion
  separately from the sibling shadow's deletion. This exposed an actual paint-time access
  to an already-deleted opacity effect. The shadow now hides synchronously when its owner
  is destroyed, before deferred cleanup; normal animated closes retain their shadow.
- Kept the outside-click regression independent of whether the surrounding advanced area
  is a popover or a persistent region, since that layout was being edited concurrently.
- Dedicated endpoint regressions: **14 passed**.
- Final isolated related suite (`test_endpoint_panel_toggle`, `test_floating_panels`,
  `test_floating_panel_chrome`, `test_popup_material`): **93 passed, 2 deselected**.
  The two excluded outline-pixel cases also fail against the saved pre-toggle
  `floating.py` (dark delta 14; light delta 0, versus the expected >15). Their existing
  styling behavior was not changed to make this toggle repair pass.
- A full UI run was attempted, then stopped after concurrent advanced-settings changes.
  A subsequent larger targeted collection hit an unrelated `ToolbarShadowLayer` import
  mismatch in `test_session_toolbar.py`. Full-suite green is **not** claimed.
- Offscreen rendered open/closed states were visually checked; second activation removes
  the endpoint panel and leaves the advanced area available.
- Ruff and scoped `git diff --check` passed. No commit was made.

Local diagnostic logs and snapshots are under `.tmp/endpoint-toggle-*`; the final
isolated test output is `.tmp/endpoint-toggle-resume-isolated.log`.
