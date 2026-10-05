# Compression Runtime Integration Implementation Plan

**Goal:** Isolate summary generation and apply accepted summaries to the actual model context; retain intentional progress animation.

**Architecture:** Generate in AgentKernel.create_isolated() with bounded lifecycle and current route. Project application-owned compaction entries onto an immutable full-history runtime snapshot, restoring it with the existing validated runtime restore API (no extra model call). Commit activation only after runtime restore succeeds; compensate failures. Strip application compaction overlays before rebuilding authoritative history; reapply branch-local active versions on resume. Preserve originals, whitelists, and post-preview turns. The peer chat owns compression_trigger.py and its tests; this chat integrates its API without editing that module.

**Tech Stack:** Python, asyncio, PySide6, existing Pi JSONL runtime restore, pytest with real Pi + local mock provider.

## Tasks
1. Add failing unit regressions for isolated preview lifecycle and accepted/reverted runtime context.
2. Add pure compaction snapshot projection/removal and coverage-boundary validation.
3. Add isolated generation wrapper; abort/shutdown only the temporary kernel, including timeout/cancellation.
4. Add coordinator runtime transition for accept/rollback and apply active versions when resuming.
5. Route preview dialog accept/rollback through async runtime handlers; preserve standalone service tests. Wire isolated stream and peer trigger guard in app.py, without editing progress animation.
6. Run focused unit/UI tests, real Pi/mock-provider context checks, and lint/type checks; inspect final diff without disturbing pre-existing workspace edits.

No live database mutation, live provider requests, commits, or worktree reset.

## Verification

- Production animation implementation in chat_view.py was not edited.
- Real Pi + local mock provider verifies preview isolation, applying without another provider call, post-preview tail preservation, repeated resume/continue, and rollback via actual outgoing request bodies.
- Focused final regression: 29 passed (runtime, managed UI, peer trigger callback tests, real Pi).
- Extended regression: 137 passed; mixed Windows subprocess cleanup emitted non-failing asyncio pipe warnings, while the final focused run was clean.
- Ruff passed for changed code; mypy passed for eight affected production files.
- Peer scope: compression_trigger.py plus 13 trigger unit tests and 9 app callback tests.
- Final full unit suite: 1207 passed, 6 platform-dependent skips (37.66s).
