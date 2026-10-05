# Compression Marker Implementation Plan

**Goal:** 在会话中持久恢复压缩前后的视觉边界。
**Architecture:** 从已有 accepted 压缩版本投影到最后一条覆盖消息的展示元数据，不修改数据库或原始消息。ChatView 渲染主题化分隔线，接受/回退成功后仅同步分隔线，不重建聊天气泡。明确分支作用域，防止继承消息导致误取父分支的压缩状态。
**Tech Stack:** Python, PySide6, pytest-qt, memory/SQLite repositories.

## Steps
1. Add unit tests in tests/unit/test_compression_markers.py: accepted-only, boundary, repeated versions, rollback, branch scope, reload (both repositories).
2. Add UI tests in tests/ui/test_compression_dividers.py: order, pagination, incremental rendering, live synchronization without bubble replacement, clearing/branch replacement.
3. Extend src/limbowave/application/history_payload.py with optional compression metadata and explicit branch scope.
4. Add CompressionDivider in src/limbowave/ui/compression_widgets.py; render/sync in src/limbowave/ui/chat_view.py.
5. Wire history reads and successful apply/rollback in src/limbowave/app.py, including stale scope protection.
6. Run focused tests, relevant existing UI/unit regressions, lint, and an offscreen visual check. No commits or unrelated changes.

## Verification
- 26 new unit/UI/wiring checks passed (memory + SQLite persistence, accepted-only, rollback, live sync, paging and stale scope).
- Related regression run: 209 passed, one existing hover test timed out in a batch; its standalone rerun passed. Another UI run likewise passed 105 with the same batch-only hover timeout.
- Ruff passed for all changed Python files; mypy passed for history_payload, compression_widgets and chat_view.
- Offscreen screenshots checked in both dark and light themes; explicit Windows CJK font loaded for the preview renderer.
