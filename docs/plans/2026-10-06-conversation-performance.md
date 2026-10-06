# Conversation Responsiveness Implementation Plan

> Execute locally, task by task, with regression checkpoints; preserve concurrent workspace changes.

**Goal:** Improve perceived responsiveness while sending, streaming, reading and paging through desktop conversations without changing startup or model behavior.

**Architecture:** Keep authoritative events and persistence unchanged. Coalesce only presentation updates, retain immediate first-token feedback and message-boundary ordering, and give the shared Qt/asyncio loop bounded opportunities to process input. Reuse already-materialized history rows instead of re-rendering the entire expanded transcript.

**Tech Stack:** Python 3.12+, PySide6, qasync/asyncio, pytest-qt, Ruff, mypy.

---

## Constraints / alternatives

- Do not change startup composition, login, eager/lazy service initialization, model warmup, LAN/web work or existing uncommitted feature changes.
- Prefer bounded updates in the existing widgets over migrating the whole transcript to a virtualized model/view (larger compatibility and interaction risk).
- Do not trade away content, tool auditing, retry/stop correctness, text selection, run grouping, compression markers or user-controlled scroll position.
- Measure synthetic local UI/transport workloads, not a real provider. No claims about provider first-token latency or actual compositor FPS.

### Task 1: Establish baseline and regression cases

**Files:**
- Create: `scripts/benchmark_conversation.py`
- Create: `tests/ui/test_conversation_performance.py`
- Modify: `tests/unit/test_pi_rpc.py`
- Local measurements: `.var/conversation-performance/`

1. Run existing chat, thinking, Markdown and message-list tests.
2. Measure document update counts and elapsed time for bursts and multi-line streaming.
3. Measure repeated history expansion and count surviving/rebuilt rows.
4. Add failing tests for bounded updates, immediate first output, exact final contents, boundary/reset/retry safety, selection and ordered cooperative transport dispatch.

### Task 2: Coalesce assistant presentation work

**Files:**
- Modify: `src/limbowave/ui/chat_view.py` (streaming row/segment methods only)
- Create if useful: `src/limbowave/ui/streaming_text.py`
- Modify: `tests/ui/test_thinking_scroll_performance.py`, `tests/ui/test_message_list_limits.py` (await the bounded display cadence, not synchronous per-token rendering)

1. Keep first body output immediate; buffer subsequent text at a bounded non-debouncing cadence.
2. Append with a separate QTextCursor, preserving selection and disabling undo.
3. Avoid repeated full-body whitespace scans and per-token scroll/layout work.
4. Flush/cancel at finalization, segment transitions, stop, retry, history replacement and widget disposal; stale buffered content must not leak.
5. Run streaming, stop/retry, compression and scroll regressions.

### Task 3: Keep transport bursts cooperative

**Files:**
- Modify: `src/limbowave/infrastructure/pi_rpc.py` (stdout receive/dispatch only; no startup changes)
- Modify: `tests/unit/test_pi_rpc.py`
- Modify: `src/limbowave/application/services/run_coordinator.py` (queued-event draining only)
- Create: `tests/unit/test_conversation_event_fairness.py`

1. Add an explicit bounded cooperative yield while draining readily available small frames (awaiting an immediately-completing coroutine does not yield).
2. Avoid rescanning/copying an ever-growing incomplete JSON frame on each pipe read.
3. Preserve byte-newline framing, Unicode separators, CRLF, response/event ordering, invalid frames and trailing partial-frame handling.
4. Verify heartbeat/input opportunity during synthetic bursts and large fragmented frames.

### Task 4: Reuse history rows on pagination

**Files:**
- Modify: `src/limbowave/ui/chat_view.py` (history rendering and load-earlier methods)
- Modify: `tests/ui/test_conversation_performance.py`

1. Extract the existing grouped-history entry renderer without changing its initial-load semantics.
2. Prepend only the next page; keep existing widgets, expanded reasoning/tools, selections and active streaming state.
3. Preserve a visible anchor across deferred Qt layout; retain run alignment and compression dividers.
4. Run history/grouping, live-branch, session-switch and scroll regression tests.

### Task 5: Verify and record measured results

**Files:**
- Create: `docs/audits/2026-10-06-conversation-performance.md`

1. Re-run the same benchmark inputs against baseline and optimized snapshots; report counts and median timings.
2. Run relevant UI/unit/integration tests, Ruff and targeted mypy; run broader regressions if feasible.
3. Inspect final diffs and concurrent modifications; report limitations and unrelated failures explicitly.

## Implementation notes

- The initial baseline is saved under `.var/conversation-performance/baseline/`; it includes the pre-existing uncommitted chat changes. No startup changes were reset or overwritten.
- Streaming uses a dedicated GUI-thread `StreamingTextBuffer`; first output and message/tool boundaries remain immediate, hidden rows pause document work, and copy/stop flush while authoritative finalization/retry/reset discard superseded fragments.
- Scroll state now retains explicit upward-scroll intent through a temporary zero-range dock animation, rather than accidentally re-enabling bottom-follow.
- Load-earlier prepends only new rows with the outer transcript layout suspended for that batch; existing widget identity, reasoning/tool expansion, selection and live-run pointers survive. A short-lived visible anchor absorbs deferred Qt height updates and yields to explicit scrolling.
- Both the RPC reader and the post-tool event queue yield after 64 events or about 8 ms; queue liveness is checked again after yielding. Large fragmented RPC frames use a bytearray and scan only the new suffix.
- Do not interpret the benchmark as provider latency or real display FPS. Large individual Qt/Markdown operations and initial history rendering remain separate work; this change is not a complete virtualized transcript rewrite.
