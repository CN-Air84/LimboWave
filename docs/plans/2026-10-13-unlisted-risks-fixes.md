# Unlisted Risks Fixes Implementation Plan

**Goal:** 修复 2026-10-13 补充审计的四项断点，并保留现有安全边界。

**Architecture:** SSE 的连接空闲寿命与持久设备授权分离。SQLite 快照显式启动读事务，在同一连接读取游标和正文；解密前检查容量，后台线程执行并限制并发。新增迁移移除设备同步触发器，限制日志保留窗口。网页保留入口上限并清楚提示；部署明确外部模型运行时。

**Tech Stack:** Python / SQLite WAL / FastAPI / React / Vitest / systemd。

## 1. SSE 空闲退出
- 修改 `src/limbowave/web/stream.py`，统一连接剩余寿命计算；到期退出生成器和阻塞写监视任务，不主动撤销持久设备。
- 在 `tests/integration/test_web_stream.py` 补持久授权、空闲退出、无空转、阻塞网络写测试，先确认旧实现失败，再修复。

## 2. 快照与日志
- 修改 `src/limbowave/infrastructure/database/sqlite_repositories.py`、`change_log.py`，同一显式读事务取元数据与实体；保留清理后的高水位。
- 修改 `migrations.py`，新增而不改历史迁移；移除设备日志触发器，生产写入自动保留最近 50,000 序号窗口。
- 默认全量快照最多 10,000 实体、8 MiB 数据预算；超限明确拒绝，绝不返回不完整正文并推进游标。
- 修改 `src/limbowave/web/app.py`，后台构建和编码快照，每个应用最多一份在构建，繁忙快速失败。
- 在 `tests/unit/test_change_log.py`、`tests/integration/test_sync_protocol.py` 验证并发写不会漏同步、触摸不产生日志、清理高水位、容量和事件循环响应。

## 3. 请求体限制
- 修改 `src/limbowave/web/security.py`、`web/src/api/client.ts`：应用和代理的 413 都转换成中文长度限制提示，不重试、不清空草稿。
- 测试 JSON/HTML 413、UTF-8 长正文和无执行副作用；在 `docs/development/lan-web.md` 说明 64 KiB 是整个 JSON 的字节数，桌面无此入口限制。

## 4. 部署运行时
- 修改 `docs/development/server-deployment.md`、`deploy/limbowave.service`：Node >=22.19.0、Pi 精确 0.86.0、系统路径、服务账号预检和真实发送验收。
- `tests/integration/test_server_deployment.py` 固化文档与 unit 路径合同；不把静态检查宣称为真实 VPS 验收。

## 验证与交付
逐项运行回归测试，再跑 Python 单元/集成、前端测试和构建、静态检查。回写审计修复记录，保留最初审计原文。不扩展到另一份审计的增量实体应用、回执原子性或 CI 工作。
