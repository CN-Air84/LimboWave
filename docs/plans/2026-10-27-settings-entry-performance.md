# Settings entry responsiveness implementation plan

**Goal:** Start opening Settings promptly without constructing every hidden category or blocking chat startup.

**Architecture:** Keep the existing snapshot animation and retained SettingsPage. Register lightweight placeholders, materialize categories once on selection/access, and refresh only the active category on reopen. Connect application services through page-level signals and a one-time tab-created notification so wiring does not defeat lazy construction. Import probe/network UI only when needed.

**Tech Stack:** Python 3.12, PySide6, pytest-qt.

## Steps
1. Add regression tests for lazy construction, no eager imports, retained tabs, refreshed data, and service wiring. Establish an offscreen timing baseline.
2. Modify `src/limbowave/ui/settings_panel.py` to build categories on demand and defer hidden refreshes. Preserve tab order, public tab accessors, unsaved appearance handling, and existing transitions.
3. Modify `src/limbowave/app.py` to bind optional controllers only when their tabs exist, and pass the LAN panel factory instead of constructing it during settings entry.
4. Defer type-only and probe-task imports in `src/limbowave/ui/logical_models_tab.py`.
5. Run targeted and wider UI tests, Ruff, type checks, and the same timing probe. Check first-open, reopen, tab switching, close during animation, and appearance updates.

Baseline (local offscreen, empty configuration with appearance, cProfile enabled): import 215 ms, construct 423 ms, show/snapshot 202 ms; total about 841 ms. Warm reopen about 17 ms. Real-window latency can vary with history, DPI, theme, and model catalog size.

No startup prewarming, animation shortening, worker-thread QWidget creation, or unrelated working-tree changes.


## Implementation and verification
- All settings categories except the initial logical-model page now start as placeholders; each is created once on selection or explicit accessor use.
- Reopening refreshes the active model/endpoint/memory page only; already-created hidden pages catch up when selected.
- Reminder/presence controller bindings are attached once after category creation, with current live controller state. The LAN panel factory is not invoked on entry.
- Delayed hint styling reads the current theme and font size. Existing page durations and snapshot animation are unchanged.
- The same empty-config offscreen cProfile probe recorded approximately 4 ms import + 85 ms construction + 71 ms show/snapshot (160 ms total), with warm reopen around 18–20 ms. These are local samples, not a guaranteed latency; concurrent test load and CJK font initialization produced higher times in other runs.
- Eight new deterministic regressions cover deferred UI/network imports, lazy category reuse, refresh on demand, theme correctness, lazy LAN construction, and real application/controller wiring.
- Consolidated related UI regression suite: **304 passed** (including all eight new tests). Ruff and targeted strict mypy pass. Offscreen default and appearance screenshots were inspected with an explicitly loaded CJK font.
- Broader data-reset lifecycle tests time out in the existing interactive onboarding gate (`oobe_live._Gate.wait`) before app wiring or settings entry. That unrelated test setup was not changed.
