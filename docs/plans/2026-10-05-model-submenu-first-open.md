# Model submenu first-open repair

**Goal:** The first brand click opens its model submenu reliably, without switching to another brand first.

**Investigation:** Qt 6.11.2 QMenu keeps doChildEffects enabled for the first submenu, then disables it for subsequent ones. The root currently suppresses native effects only during its own popup call. On Windows the first child can temporarily be hidden by the scroll effect while its proxy window is shown; subsequent children use a different path. Existing tests wait past animation and directly click QWidgets, missing this transitional state.

**Plan:** Add regression tests for first-open hover/click/keyboard paths with native scroll and fade enabled; suppress native menu effects only while dispatching selector menu input/timer events (not app-wide for the lifetime of the menu); preserve Qt's submenu ownership, positioning and keyboard/closing behavior. Validate original selection/site/sorting/animation tests on offscreen and Windows Qt. Keep unrelated working changes intact; no commit.

## Implemented and verified
- Suppressed native effects only during selector menu timer/mouse/context dispatch, direct activation and submenu keyboard navigation. QMenu::event can flush the pending hover timer before mousePressEvent, so the event boundary must also be covered.
- Brand press explicitly activates its submenu; release is consumed rather than treated as a leaf/close. Repeated clicks on the same brand stay usable. Site popups use the same no-native-proxy path.
- Existing app changes introduced _clear_thinking_trial in routing; the previously created callback test fixture now supplies that dependency (production routing unchanged here).
- Final related offscreen regression: 179 passed.
- Final Windows-native regression, run serially to avoid Qt mouse-test cursor interference: 25 passed, 1 deselected. The excluded pre-existing long-list screen-size test failed before this repair on the mixed-DPI multi-monitor setup (1079px menu vs 1032px available height); it passes in the offscreen suite and is outside this repair.
- Ruff and focused mypy passed. Original branch sorting, site selection, keyboard navigation and root animation settings remain intact; no app-wide persistent animation preference change.
