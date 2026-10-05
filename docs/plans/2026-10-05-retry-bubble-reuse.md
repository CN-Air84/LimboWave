# Retry Bubble Reuse Implementation Plan

**Goal:** 重试只更新原回复，不追加重复气泡、错误提示或旧尝试分段。

**Architecture:** 保留现有手动重试和历史链折叠逻辑。自动重试复用当前 run 的回复卡和状态行，重置旧尝试内容、撤去本次失败的错误行；重复的手动重试接收事件按消息 ID 幂等处理。不按文本去重，不改变运行审计或正常多段 Agent 回复。

**Tech Stack:** Python / PySide6 / pytest-qt / pytest-asyncio.

### Task 1: Reproduce
- Add regression tests in `tests/ui/test_retry_ui.py` for repeated automatic retry, retained earlier messages, successful/failed completion and replayed manual retry events.
- Run `.venv/Scripts/python.exe -m pytest tests/ui/test_retry_ui.py -q` and confirm the new tests fail.

### Task 2: Minimal UI fix
- Update `src/limbowave/ui/chat_view.py`: show automatic retry state in the existing assistant card; clear failed-attempt state without replacing the card; ignore an already-applied manual retry event.
- Update `src/limbowave/app.py`: do not settle an active run merely on an error event; automatic retries must keep the current card alive until `settled`.
- Keep final failure visible and leave retry buttons usable.

### Task 3: Verification
- Run focused retry tests, chat view tests, and run coordinator tests.
- Run the broader UI suite and lint the changed code; review only this task’s diff without disturbing existing workspace changes.

## Verification results

- New regression cases reproduced the bug before the fix.
- Focused retry / chat / coordinator suite: **171 passed**.
- Ruff on the three changed Python files: passed.
- Full UI run: **863 passed, 1 skipped, 5 failed** (offscreen Qt). Failures cover checkbox rendering, locked-log smoke diagnostics, two login view checks and high-DPI transition capture; no retry tests failed.
- Isolated reruns with both the fix and the pre-fix versions of the two production files reproduced the checkbox and diagnostics failures; the other three passed in isolation. No unrelated UI fixes were made.
- Logs: `.var/retry-bubble-ui-tests.log`, `.var/retry-bubble-baseline-tests.log`, `.var/retry-bubble-isolated-tests.log`.
