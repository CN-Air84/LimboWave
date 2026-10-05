# History Loading Motion Implementation Plan

**Goal:** 输入框先完成下移，加载蒙版与浮层随后一起淡入，结束时一起淡出。

**Architecture:** ChatView 在停靠完成且布局落定后显示浮层，后台读取与下移并行，不依赖固定延迟。HistoryLoadingOverlay 用统一绘制透明度实现可反向的淡入淡出，提前结束不闪现。

**Tech Stack:** Python, PySide6, qasync, pytest-qt.

## Steps
1. 在 `tests/ui/test_history_loading.py` 测试下移期间隐藏、完成后淡入、快速结束不闪现、中断/重入、无浮层加载不改布局；更新真实慢读取测试。
2. 在 `src/limbowave/ui/history_loading.py` 增加统一透明度动画，淡出结束才隐藏并停止转圈；重复调用不重启，反向时保持透明度连续。
3. 在 `src/limbowave/ui/chat_view.py` 分离显示请求与业务加载态，先停靠再显示；结束时取消待显示请求，恢复空会话布局，不覆盖高级栏状态。
4. 运行加载、停靠、消息动画、会话切换回归测试以及相关静态检查。

## Validation
- `.venv/Scripts/python.exe -m pytest tests/ui/test_history_loading.py -q`
- `.venv/Scripts/python.exe -m pytest tests/ui/test_chat_view.py tests/ui/test_main_window.py tests/ui/test_message_send_motion.py tests/ui/test_session_switch_during_generation.py -q`
- `.venv/Scripts/python.exe -m ruff check src/limbowave/ui/history_loading.py src/limbowave/ui/chat_view.py tests/ui/test_history_loading.py`

## Design decisions
- 使用停靠完成回调而非固定计时，避免动画中断或布局延后时浮层抢先显示。
- 淡出不延长业务加载锁，快速加载不强制等待转圈。
- `show_overlay=False` 继续只禁用输入，不触发停靠或加载动画。

## Verification results
- 加载时序/动画回归：17 passed，覆盖真实 qasync 慢读取、快速完成、空结果、显示请求取消以及淡入淡出中反向。
- 聊天视图、主窗口、消息发送动画、生成期间切换会话、分支实时重载、重新生成响应和重试 UI：156 passed。
- Ruff 与两个修改源文件的 mypy 检查通过；相关文件 `git diff --check` 通过。
- Qt 离屏截图检查通过：`.var/history-loading-motion.png` 展示空会话、输入框下移、加载浮层和淡出四个阶段。
