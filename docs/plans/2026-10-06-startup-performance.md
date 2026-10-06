# Startup and Interaction Performance Implementation Plan

> **For Codex:** Execute and verify each bounded task in the current checkout; preserve all existing in-progress changes. Do not commit or change vault/KDF security parameters.

**Goal:** Reduce startup CPU/memory and the time until chat is usable, while preserving desktop/web behavior, security boundaries, and visual appearance.

**Architecture:** Keep the existing Qt/asyncio loop and worker isolation. Load opt-in services and optional UI only when needed, make storage preparation non-blocking for readiness, and avoid repeated parsing/rendering of unchanged content. Use deterministic regression contracts and a repeatable isolated benchmark rather than brittle timing assertions.

**Tech Stack:** Python 3.12, PySide6, qasync, SQLite, pytest-qt; React 19, Vite, Vitest.

---

## Evidence and scope

- Profiled the working tree, including the user's uncommitted LAN and export work. No reset, clean, stash, or automated commit.
- Isolated fresh-process/offscreen probe uses temporary data, no real credentials/network/model, a deliberately tiny *test-only* KDF, and a stubbed shell probe. Production KDF stays untouched.
- Initial profiled `_wire`: 1.44 s, including about 1.06 s importing the disabled Web server stack. Profiling overhead is substantial; retain separate unprofiled samples.
- Initial unprofiled observations: app import ~0.92 s; `_wire` ~0.50 s; storage prewarm ~0.68 s; settings prewarm ~0.40 s. Widget count grows from 145 to 720 with hidden settings prewarm. These are local observations, not OS-cold-launch guarantees.
- Baseline full unit/UI run is captured in `.var/performance-baseline-tests.log` before implementation. Track existing failures separately.

## Task 1: Defer optional desktop/LAN startup work

**Files:** `src/limbowave/app.py`, new `src/limbowave/ui/lan_controller.py`, `tests/ui/test_startup_responsiveness.py`, new `tests/ui/test_lan_controller.py`.

1. Add regression tests: wiring must not import or construct LAN services; shutdown/reset before LAN activation remains safe; opening settings constructs UI on Qt and reuses it; first LAN activation preserves validation/error/cancellation behavior.
2. Extract lazy LAN ownership from the large app closure. Keep event subscription, password verification, sockets, and model catalog absent until explicitly enabled. Import the Qt-free server stack off-thread, but construct loop-bound services on the GUI/event-loop thread.
3. Stop polling when the LAN pane is hidden and do not rebuild identical device lists. Keep pairing expiry, revoke, reset invalidation, and shutdown intact.
4. Remove mandatory hidden settings prewarm from the ready path. Preserve explicit on-demand construction and reuse.
5. Do not wait for an unused storage process before declaring readiness; verify its first actual request still starts the worker off the UI thread and shutdown drains submitted work.

## Task 2: Reduce cold imports and repeated computation

**Files:** `src/limbowave/domain/model_display.py`, `src/limbowave/ui/markdown_render.py` and focused tests as indicated by measurements.

1. Assert ASCII model ordering/display does not import the pinyin dictionary; retain exact Chinese ordering.
2. Add a bounded sort-key cache and lazy non-ASCII pinyin import, without altering IDs or stable sorting.
3. Defer Markdown/highlighter initialization where not needed for empty startup; cache only safe bounded reusable parser/lexer resources, not unbounded private message contents.
4. Re-profile and retain only meaningful changes; no broad speculative rewriting.

## Task 3: Web initial load and streaming rendering

**Files:** `web/src/App.tsx`, `web/src/components/MessageList.tsx`, related Vitest tests.

1. Keep heavy Markdown code out of the login/connection page's eager dependency graph.
2. Memoize unchanged message/Markdown rendering; do not parse collapsed thinking until opened. Keep sanitized links and remote-image blocking unchanged.
3. Verify streaming updates, scroll anchoring, accessibility, and first-open behavior; run all web tests, typecheck, and production build.

## Task 4: Reproducible verification and handoff

**Files:** new `scripts/performance_benchmark.py`, new `docs/audits/2026-10-06-startup-performance.md`.

1. Provide a standard-library benchmark runner with child-process isolation, temporary data, JSON stage metrics, GUI heartbeat, and Windows working-set samples; no interaction with the real vault or listener.
2. Run focused Python tests during each change, then the full unit/UI/integration suites, Ruff/mypy on changed code, and web tests/build.
3. Repeat the same benchmark without profiling; report medians and limitations, including any cost moved to first use and pre-existing failures.
4. Inspect the final diff and ensure all changes are performance-related and all user edits are retained.

## Implemented refinements

- Added scoped history attachment projection to both repositories and the history-view loader. The measured whole-vault snapshot scan was a material interaction bottleneck; no schema migration was needed.
- Also made ASCII slug generation lazy so opening empty settings does not immediately reload the pinyin dictionary.
- Browser verification exposed CommonJS/React helpers pulling Markdown into the entry chunk despite lazy loading. Explicit runtime chunk ownership and a real network-request E2E regression now protect the boundary. Password crypto is loaded alongside the challenge only on explicit verification.
- Added cancellation/reset/recreation and real qasync-listener coverage for the lazy LAN owner.
- Measurements, first-use tradeoffs, baseline failures and verification are recorded in `docs/audits/2026-10-06-startup-performance.md`.
