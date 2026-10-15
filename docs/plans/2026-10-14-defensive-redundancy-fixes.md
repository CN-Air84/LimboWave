# Defensive Redundancy Reduction Implementation Plan

**Goal:** 消除审计确认的五类重复工作，保持安全边界和现有行为。

**Architecture:** 选择局部复用而非跨层全局缓存或删除校验：模块级 delta schema、队列携带 size、同步轻量 is_available、QObject 持有单次定时器、恢复结果附带可选验证结果。保留当前工作树其他修改，不创建会丢失这些上下文的新工作树，不提交无关改动。

**Tech Stack:** Python 3.12 / asyncio / PySide6 / pytest；TypeScript / Zod / Vitest。

## 1. 先建立失败的性能行为测试

- 新建 `tests/unit/test_redundancy_performance.py`：广播/回放仅计算一次大小；字节限额、消费者隔离不变；真实 facade 可用性查询不调用 stream_snapshot；Pi 恢复读取 entries 一次；未附验证结果的 kernel 仍验证，附带失败结果仍拒绝恢复。
- 新建 `tests/ui/test_hover_install_batching.py`：一批 ChildAdded 仅扫描一次；主动更新设置消费待安装任务；销毁根对象后无延迟回调。
- 修改 `tests/integration/test_web_stream.py`：测试替身实现轻量查询；添加多事件与心跳只调用一次完整 state 的断言。
- 新建 `web/src/state/chat.performance.test.ts`：delta 处理中不创建 schema；非法 payload 仍报错；思考和正文仍按序更新。
- 在修改业务代码前运行上述新测试，确认预期断言失败。

## 2. 按最小变更实现

- `web/src/state/chat.ts`：模块级 DeltaPayloadSchema。
- `src/limbowave/application/services/event_broker.py`：queue 条目改为 `(copy, size)`；发布、回放、resync、invalidate 和出队统一账目；保留 publish 返回值契约及必要深复制。
- `src/limbowave/application/services/runtime_facade.py`：增加同步 `is_available()`，复用现有检查，不查询输出快照。
- `src/limbowave/web/stream.py`：异步等待后使用轻量检查，删除无调度间隙的第二次检查；初始化仍使用完整 state。
- `src/limbowave/ui/theme_effects.py`：控制器持有 single-shot QTimer，未激活时才启动；主动扫描先停 pending timer，QObject 销毁自动取消。
- `src/limbowave/domain/runtime_state.py`：RuntimeRestoreResult 末尾增加可选 validation，兼容已有构造。
- `src/limbowave/infrastructure/pi_adapter.py`：恢复内置验证保留，并把结果放入返回值。
- `src/limbowave/application/services/runtime_recovery_coordinator.py`：复用结果中的 validation；缺失时执行既有校验。
- 更新 kernel 恢复接口说明及必要测试替身，避免引入有完整快照成本的兼容兜底。

## 3. 验证与交付

1. 重跑新增测试和事件/恢复/主题/SSE/前端相关原有测试。
2. 运行定向 lint、类型检查和尽可能广的单元/集成测试；区分已有失败与本次回归。
3. 用上一轮合成输入复测计数：1,000 事件/4 消费者 size 从 9,000 降为 1,000；SSE 快照 2,001 降为 1；恢复 entries 2 次降为 1；扫描 20 次降为 1。
4. 更新审计结果文档；不将局部微基准描述为整应用提速。

## 执行结果

- 五项最小修改及新增回归已完成。
- 新回归先在原实现上确认失败，再在修改后通过。
- 定向 Python 93 项通过；前端 Vitest 67 项通过；前端类型检查、定向 Ruff/Mypy、差异空白检查通过。
- 扩大单元测试结果与两处无关的收集/fixture 问题见审计报告。未修复无关进行中修改，未提交工作区。
