# Tool-step Performance Implementation Plan

**Goal:** Keep tool visibility switches responsive while tools stream, without changing audit records or expansion state.

**Architecture:** Reuse the existing warm ConversationProcess for tool result normalization and display projection. Keep ordered event bookkeeping and all QWidget work in the GUI process. Reuse transition spacer items, cache wrapping measurements, and animate viewport rows only.

**Tech Stack:** Python / PySide6 / asyncio / multiprocessing spawn / pytest-qt.

## Decisions
- Merely shortening the animation hides symptoms; a full UI/process rewrite adds unnecessary lifecycle risk. Optimize the measured hot path and extend the existing IPC boundary instead.
- Display strings are transient, not authoritative audit fields; never persist them or truncate stored arguments.
- Queue events behind in-flight tool normalization to preserve tool/message/settled order. Drain before finalization and shutdown; report backend errors without doing expensive fallback work on the GUI thread.
- Keep reversible fade/slide, live updates, reduced-motion fallback, and detail expansion state.

## Steps and verification
1. Add regression tests for stable spacer identity, cached measurement, offscreen rows, and prepared display data. Run to demonstrate failures.
2. Implement Qt-free tool projection and process operation; verify real child PID, ordered events, duration, persistence, and worker failure.
3. Optimize layout/height work without changing geometry semantics. Run visibility, details, history and coordinator regressions.
4. Run lint/type checks and wider tests; record any unrelated failures. Preserve existing uncommitted changes; do not commit or create a worktree.

## Outcome
Implemented and verified. See docs/audits/2026-10-05-tool-step-performance.md for scope, benchmark methodology, 86 focused passes, and full-suite limitations.
