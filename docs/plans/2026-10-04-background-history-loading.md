# Background history loading implementation plan

**Goal:** 会话/分支读取不阻塞 Qt 主线程，加载期间提供可见动画并防止误发/重复切换。

**Architecture:** 复用 asyncio + 后台线程的既有架构。历史读取使用有生命周期的单线程 reader；后台只做数据库、解密、附件解码与载荷准备。Qt 控件和 QPixmap 留在 GUI 线程。加载遮罩覆盖消息区但不阻塞窗口事件循环。

**Tech Stack:** Python 3.12+, PySide6, qasync, pytest/pytest-qt.

## Steps
1. Add a managed background reader with cancellation/shutdown coverage in `src/limbowave/application/services/history_reader.py` and `tests/unit/test_history_reader.py`.
2. Add a theme-aware animated loading overlay and independent loading state to `src/limbowave/ui/chat_view.py`; disable conflicting composer/navigation actions, preserving drafts and model availability.
3. Unify conversation and branch loading in `src/limbowave/app.py`. Prepare messages, attachment data and permissions off-thread; protect against duplicate loads; clear loading in success/error/cancellation paths. Refresh branch expansion asynchronously and drain readers before storage closes.
4. Add UI and actual app wiring regression tests for slow reads, thread identity, error recovery, branch expansion and shutdown. Keep existing crossfade behavior after loading and preserve prior local changes.

## Validation
- `.venv/Scripts/python.exe -m pytest tests/unit/test_history_reader.py tests/ui/test_history_loading.py -q`
- `.venv/Scripts/python.exe -m pytest tests/ui/test_chat_view.py tests/ui/test_sidebar.py tests/ui/test_retry_ui.py tests/unit/test_session_isolation.py -q`
- Ruff and mypy on changed source files; offscreen screenshot of the loading overlay if supported.

## Verification results
- Targeted regression: 145 passed (history loading, chat, sidebar, retry, message motion, reader, session isolation).
- Complete UI + selected unit run: 865 passed, 1 skipped, 5 failed. Isolated rerun of those failures: 3 passed; checkbox pixel assertion and diagnostics log-lock warning assertion still fail outside the history-loading path. The checkbox failure is also recorded in the repository's pre-existing UI logs.
- Ruff: changed files pass. Mypy: new loading code passes; existing `QImage.save(buffer, "PNG")` stub mismatch in ChatView remains.
- Offscreen visual QA: `.var/history-loading-preview.png`, with explicit Microsoft YaHei font loading for offscreen Chinese text.
- Added a real qasync/QTimer test proving animation ticks during a blocked background read; cancellation waits for the worker to release resources before shutdown.
- Rendering uses background-prepared Markdown and incremental Qt widget creation; existing synchronous callers remain compatible and the loaded view fades in.
