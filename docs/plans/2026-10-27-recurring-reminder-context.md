# 生活节律提醒与约定回溯 Implementation Plan

**Goal:** 在现有一次性提醒上增加轻柔周期提醒，并从通知/提醒卡片回到设定时的消息。

**Architecture:** 复用本地 ReminderService、SQLite 事务与现有历史预览。增加 daily / weekdays（周一至周五，不包含节假日规则）/ weekly 和 IANA 时区；首次 target_time 决定固定本地时刻。加密保存周期规则与来源消息 ID。投递及推进下一次时间原子提交，错过多次只补发一次；取消停止整个周期。UI 只发消息 ID，服务校验其所属会话/分支后解析来源，旧提醒或已删除来源回退到提醒消息。不创建后台服务、不自动运行模型、不改动全局通知偏好。

**Tech Stack:** Python 3.12 / zoneinfo / SQLite / PySide6 / pytest / TypeBox。

---

## 方案取舍

1. **扩展现有提醒（采用）**：保留权限、通知去重、轻柔音和会话持久化，变更范围最小。
2. 新增独立日历：管理能力更多，但重复调度/权限/通知，超出轻量延伸。
3. 使用 OS 后台计划任务：能覆盖退出期间，但需额外服务和资料库解锁策略，本次不做。

## Task 1: 周期时间规则和持久化

- 修改 `G:/LimboWave/src/limbowave/domain/reminder.py`；新增 `G:/LimboWave/src/limbowave/domain/reminder_recurrence.py`。
- 修改 `G:/LimboWave/src/limbowave/application/repositories.py`、`G:/LimboWave/src/limbowave/infrastructure/memory_repositories.py`、`G:/LimboWave/src/limbowave/infrastructure/database/reminder_repository.py`、`G:/LimboWave/src/limbowave/infrastructure/database/migrations.py`。
- 先补 `G:/LimboWave/tests/unit/test_reminder_recurrence.py` 和周期服务测试，运行得到未实现失败，再实现。
- schema 20 增加 nullable 加密 details；旧记录视作一次性。条件推进同时校验 pending 和原 due_at，避免两个实例重复投递。
- 验证日/工作日/周跨越、夏令时的重复与不存在时刻、非法时区与参数、旧库原数据保留。

## Task 2: 服务与工具

- 修改 `G:/LimboWave/src/limbowave/application/services/reminder_service.py`、`G:/LimboWave/src/limbowave/application/services/tool_gateway.py`、`G:/LimboWave/src/limbowave/infrastructure/extensions/policy_enforcement.ts`、`G:/LimboWave/src/limbowave/domain/permissions.py`。
- 创建时捕获活动运行的用户消息；非运行调用回退到原分支最新用户消息，不允许模型指定其他会话或消息。首次 target_time 必须为未来、与 IANA 时区一致；周期不接受 delay_seconds。
- 周期仍为 pending，每次使用稳定的 occurrence 消息 ID，事务内只补一次并推进到 now 之后。
- 取消、回滚、并发及重启测试覆盖 memory 和 SQLite；工具无效参数须在授权前失败。

## Task 3: 气泡和会话卡片回溯

- 修改 `G:/LimboWave/src/limbowave/ui/chat_view.py`、`G:/LimboWave/src/limbowave/app.py`、`G:/LimboWave/src/limbowave/ui/reminder_notifications.py`。
- 仅 durable notice_reminder_ 消息显示“回到当初的约定”动作，键盘可达；历史重载也恢复该动作。
- 气泡与卡片复用安全导航，异步解析来源，保留正在生成的会话、草稿和权限对话；找不到来源则退回通知本身。
- 测试 live/history/read-only preview、原消息不在首屏、跨会话、缺失来源、持续运行的回复不被中断。

## Task 4: 回归与文档

- 更新 `G:/LimboWave/docs/development/reminders.md` 和 `G:/LimboWave/README.md`，给出自然语言和工具例子、工作日定义、补发/时区/退出限制。
- Run: `$tests = @((Get-ChildItem tests/unit/test_reminder*.py).FullName) + @((Get-ChildItem tests/ui/test_reminder*.py).FullName); .venv/Scripts/python.exe -m pytest @tests tests/integration/test_reminder_delivery.py -q`；预期全部通过。
- Run: `.venv/Scripts/ruff.exe check src/limbowave tests/unit/test_reminder* tests/ui/test_reminder*`；预期无新增问题。
- Run: `.venv/Scripts/python.exe -m mypy src/limbowave`；预期无新增类型问题。
- Run: `npm --prefix web test` 与 `npm --prefix web run typecheck`；确认既有移动端提醒事件不回归。
- 不自动提交、不打包、不替换正在运行的应用。


## 完成与验证（2026-10-27）

- [x] Task 1：周期规则、SQLite schema 20 增量迁移、加密来源与规则、条件推进。
- [x] Task 2：工具协议、创建确认、活动运行消息锚点、周期取消与一次性兼容。
- [x] Task 3：Windows 气泡和提醒卡片共用安全来源跳转；保留草稿/流式生成/权限弹窗。
- [x] Task 4：README 与开发文档已更新；完成专项回归及浅色/深色视觉复核。

验证结果：

- 提醒单元/桌面 UI/通知/迁移/工具桥接：157 passed，最终结果见 `G:/LimboWave/.tmp/recurring-reminder-targeted.log`。
- Web：12 个测试文件、52 项测试通过；TypeScript 检查通过。
- 提醒相关变更文件 Ruff 检查通过；相关 diff whitespace 检查通过。
- 浅色/深色卡片已渲染并目视检查：`G:/LimboWave/.tmp/reminder-context-light.png`、`G:/LimboWave/.tmp/reminder-context-dark.png`。
- 增补权限弹窗回归：弹窗打开时通知点击不会关闭弹窗、切走对话或丢失草稿。

全仓验证限制（不改动无关功能）：

- 全量单元收集受现有 `tests/unit/test_server_package.py` 引用不存在的 `scripts.package_server` 阻断。
- 排除此文件后的 fail-fast 检查：150 passed 后，架构边界测试在 `application/services/preferences_service.py` 对 PySide6 的依赖处失败。
- 当前全仓 mypy 在 `application/services/web_content.py:31` 有 `str | None` 调用 `.lower()` 的类型错误；提醒相关代码未报错。
- 未提交 Git、未打包、未重启或替换用户正在运行的应用；工作区其他功能的并行改动保留原状。
