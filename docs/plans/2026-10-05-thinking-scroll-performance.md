# Thinking Scroll Performance Implementation Plan

**Goal:** Keep scrolling responsive while reasoning streams, preserving complete text and existing interactions.

**Architecture:** Buffer collapsed reasoning without touching Qt text/layout. Render expanded reasoning with batched QTextCursor inserts rather than resetting a growing QLabel. Avoid repeated whole-card presentation passes and full-document body resets. Cache wave geometry and skip offscreen/reduced-motion/settled animation work.

**Tech Stack:** Python / PySide6 / pytest-qt.

## Decisions
- Disabling every animation only masks repeated text/layout work; a transcript virtualization rewrite is disproportionate here. Optimize both existing hot paths without changing persisted data or truncating reasoning.
- Expanded reasoning retains selectable plain text and document-driven height, with at most 50 ms display batching. Collapsed data remains immediately available through a text accessor.
- Preserve existing uncommitted changes. No commit or worktree creation.

## Steps and verification
1. Add deterministic regressions in tests/ui/test_thinking_scroll_performance.py: collapsed document untouched, expansion batches deltas, selection retained, hide/show/replacement lifecycle, no repeated card layout passes, incremental body updates, offscreen waves and static errors. Run before implementation to confirm failures.
2. Extract the thinking viewer into src/limbowave/ui/thinking_block.py and integrate into src/limbowave/ui/chat_view.py. Update private-widget assertions in existing thinking tests to use buffered content.
3. Optimize src/limbowave/ui/run_state_label.py with cached geometry and visibility/motion guards; preserve water-wave appearance.
4. Run targeted tests, wider UI regressions, Ruff and mypy. Record verification and any limitations without claiming real hardware frame-rate measurements.

## Verification results
- Initial regression run: all 9 new cases failed against the original behavior (including 500 whole-card syncs for 500 reasoning deltas, body setPlainText reset, offscreen repaint requests, and hidden-parent toggle state).
- Final focused run: **196 passed** across reasoning performance, run-state label, chat view, compression routing, smooth scroll, tool steps, retry, and branch reload.
- Tool-step visibility animation module: **14 passed** in isolation. In the combined run, its tool-only height assertion failed (209 passed / 1 failed); an isolated control restoring the previous body/layout paths also passed. This indicates order/timing sensitivity; it has not been established as a pre-existing failure, and is not claimed fixed.
- Full UI suite: **1037 passed, 9 failed, 1 skipped, 1 teardown error**, 376.60 s. Failures covered checkbox pixels, diagnostics log-lock expectation, login font/fade, transition snapshot, thinking-level dropdown theme, renamed model lists, and the combined-run tool animation assertion. The suite is not claimed green. Details saved in .var/thinking-scroll-last-test.json.
- Ruff: all changed implementation/test files passed. Mypy: all 3 changed implementation modules passed. git diff --check: passed (repository line-ending warnings only).
- Offscreen rendered screenshot inspected: .var/thinking-scroll-performance.png; selectable reasoning, line break layout and wave appearance retained.
- These are deterministic work-count and functional checks, not a measurement of native Windows scroll FPS. The running app was not restarted or interrupted; source changes require a restart to load.
