# Login Startup Performance Implementation Plan

**Goal:** Keep the login blackout responsive while startup prepares persistent storage and runtime services.

**Architecture:** Retain the synchronous startup orchestration and its Qt-pumping worker bridge. Move only non-UI work into that bridge; keep widgets, signals, IPC event-loop lifecycle, and final view updates on the GUI thread. Reuse the appearance already applied before login, defer settings UI imports until after the transition, and read the initial conversation list once in a worker.

**Tech Stack:** Python 3.12+, PySide6, qasync, concurrent.futures, SQLite, pytest-qt.

## Baseline (isolated, offscreen, fresh temporary data)
- `_wire`: 1.403 s cumulative.
- Settings UI cold imports: 0.582 s.
- PowerShell probe: 0.437 s.
- Redundant appearance application: 0.230 s.
- These are local profiling observations, not production latency guarantees.

## Steps
1. Add `tests/ui/test_startup_responsiveness.py`: worker/UI thread affinity, timer progress during blocked work, migration, one initial history read, prepared appearance reuse, and exception cleanup. Run the new tests first to demonstrate failures.
2. Update `src/limbowave/app.py`: offload secret migration, SQLite migration, runtime construction, shell probing, and startup history reads; defer settings import; avoid reapplying the prepared appearance. Keep SQLite connections scoped to the thread doing each transaction; no live connection is returned by factory construction or history reads.
3. Verify existing login, recovery, startup cleanup, settings, appearance, persistence and composition coverage; run Ruff and relevant type checks.
4. Repeat the isolated measurement with a GUI heartbeat and real blackout transition. Record limitations; do not alter password/KDF security, animation timings, or unrelated user changes.

No commits or broad formatting changes: this checkout already contains extensive in-progress work.

## Implementation and verification notes
- Startup completion now uses a queued Qt signal instead of 20 ms polling, including immediate-result and exception tests.
- SQLite factory construction returns no live connection; history transactions finish in the worker before the GUI receives the snapshot.
- `tests/integration/test_composition.py` also runs a real mock-provider conversation after constructing the runtime in a worker, checking that asyncio objects remain usable on the owning event loop.
- Updated the recovery test driver in `tests/ui/test_startup_panel_flow.py` to wait for enabled login input and ignore non-interactive, fading-out panels. Added a watchdog so regressions fail instead of hanging.
- Isolated post-change offscreen heartbeat observation: 113 timer callbacks during wiring, maximum gap 37.7 ms, blackout reached opacity 1.0. This measures event-loop responsiveness, not display frame rate or a production latency guarantee; total startup time still depends on the local shell and runtime.
- Focused verification: 6 new startup tests, 6 recovery/panel tests, 5 reset-lifecycle scenarios, and 6 composition integration cases passed. Ruff and mypy (app.py, follow-imports=silent) passed.
- Final combined regression run on the current checkout: 240 passed, 1 skipped in 148.43 s. The skip is the existing offscreen-font-dependent backdrop test. Final Ruff, app.py mypy, and diff whitespace checks passed.
