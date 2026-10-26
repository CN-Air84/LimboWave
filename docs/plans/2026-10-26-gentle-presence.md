# Gentle Presence Implementation Plan

**Goal:** 实现默认关闭、随时可静音、不要求回应的低压在场。

**Architecture:** 本地偏好保存授权及触发条件；独立应用服务用可注入的单调时钟和本地日历判断是否允许展示。聊天区只展示临时的问候，不修改聊天记录、队列、权限或模型运行状态。没有托盘实现，因此本次不引入系统托盘、系统通知、声音或后台模型调用。

**Tech Stack:** Python / PySide6 / pytest / pytest-qt。

## 交互及安全边界
- 默认关闭；独立的永久静音开关。空闲默认 60 分钟（可选 15–240），夜间默认不开启，可预设 HH:mm；问候或仅在场标记可选。
- 仅在可见、激活的聊天页展示；专注/纯净模式、模型运行、压缩、历史加载、草稿、粘贴、模态窗、设置和登录页均抑制。
- 每次 45 秒，关闭不催促；输入/点击立即收起。每段空闲仅一次；每日最多两次、两次至少间隔四小时，跨重启保留限频。
- 夜间仅在预设时刻后的 5 分钟窗口尝试，需要至少 5 分钟无交互；忙碌/睡眠/重启后不补发。休眠检测使用单调时钟的轮询间隔。
- 不监听全局键盘，不读取聊天内容，不产生通知、未读计数或消息；静音不影响用户明确设置的定时提醒。
- 限频记录写入失败即不展示；损坏记录采用当日抑制，避免意外重复打扰。

## Task 1 — 配置与策略
Create: `src/limbowave/domain/presence.py`, `src/limbowave/application/services/gentle_presence.py`.
Modify: `src/limbowave/application/services/preferences_service.py`.
Test: `tests/unit/test_gentle_presence.py`.
先验证旧偏好默认关闭、严格授权解析；用假时钟验证空闲/夜间、限频、静音、休眠、时钟回退及持久化错误。

## Task 2 — 临时展示及设置
Create: `src/limbowave/ui/gentle_presence.py`.
Modify: `src/limbowave/ui/chat_view.py`, `src/limbowave/ui/settings_panel.py`.
Test: `tests/ui/test_gentle_presence_ui.py`.
提供无需回应的轻量问候、关闭和静音；设置即时保存，保存失败明示且关闭运行时触达。

## Task 3 — 生命周期接线
Modify: `src/limbowave/app.py` (仅偏好装配和设置接线，保留同时进行的其它功能).
窗口事件过滤器仅记录本窗口交互，关窗及 QApplication 退出停计时器。输入/运行状态变化立即清除问候。

## Verification
`.venv/Scripts/python.exe -m pytest tests/unit/test_gentle_presence.py tests/ui/test_gentle_presence_ui.py tests/ui/test_zen_mode.py tests/ui/test_settings_panel.py tests/unit/test_theme_and_preferences.py --basetemp=.tmp/presence-tests`
对新增文件和修改文件运行 Ruff，并运行源代码严格 mypy。真实系统托盘和跨平台系统通知不属于本次实现。


## 实施结果（2026-10-26）

已完成配置、原子偏好保存、持久化限频、静态问候/在场标记、永久静音、活动窗口/权限面板抑制、专注与队列抑制、睡眠与日历跳变处理，以及应用装配、回滚、销毁接线。

- 新增策略和界面测试：**51 passed**，包含真实 `_wire` 设置及关闭接线。
- 主窗口、设置、纯净模式、启动、主题偏好及架构扩大回归：**399 passed, 1 failed, 1 deselected**。失败为原有 `PreferencesService` 动态导入 Qt 的架构约束；已用 HEAD 源码确认该导入在本次变更前存在。排除项为同时进行的 `companion_window.py` 硬编码字号检查，首次完整相关回归已确认该项失败，没有修改该文件。
- 扩大回归还出现独立 ReminderScheduler 的 `to_thread` 未 await 警告；本功能的隔离测试不运行其 I/O 循环，51 项最终回归无警告。
- 本功能修改/新增文件 Ruff 通过；相关源文件严格 mypy 通过。全源 mypy 的剩余两项错误在其它改动中的 `environment_service.py`（daily/previous 类型标注），本次未混入其修复。
- Qt offscreen 深浅主题视觉检查通过。测试截图位于忽略目录 `.tmp/presence-visuals`，不是生产数据或打包资源。

使用及架构说明：`docs/development/gentle-presence.md`。
