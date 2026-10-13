# 巡检未覆盖的可用性与交付断点

日期：2026-10-13。只读核对，没有改这些路径上的代码，也没有跑测试。

[项目可用性与交付风险巡检](2026-10-13-project-risk-review.md) 里已经核对过的项不再重复。回执与用户消息仍是两次提交，增量同步还没有按实体写回本地，Windows CI 和网页端到端仍未进流水线。下面四项是那份结论之外、对照调用处之后仍会卡住日常使用或让交付在测试变绿后对不上验收的问题。

## 1. 服务端事件流在空闲两小时后空转

局域网 `AuthStore.valid()` 在空闲到期时撤销设备并返回假。`tests/integration/test_web_stream.py` 的 `test_idle_expiry_closes_waiting_stream` 用的就是这个实现，空闲后流会结束。

服务端 `PersistentAuthStore.valid()` 不再把空闲写成 `revoked_at`。它只在绝对过期或显式撤销时失败。这修掉了「过夜后设备被永久撤销」，也拆掉了事件流原来的出口。

`src/limbowave/web/stream.py` 把空闲剩余时间当作 `asyncio.wait` / `wait_for` 的超时。剩余时间降到 0 以后，超时是 0。`valid()` 仍返回真，所以：

- `RevocableEventResponse` 的监视任务在 `TimeoutError` 上立刻重试，每一圈都调用 `valid()`。服务端的 `valid()` 每次新开一条 SQLite 连接，查询 `server_devices.revoked_at`。
- 生成器在没有新事件时连续写出 `: keepalive`。注释写明 keepalive 不是已认证的用户活动，不会刷新 `touched`。

浏览器停在聊天页、大约两小时没有别的 API 请求时就会进入这个循环。网页控制器在流正常时不会轮询，只有切回前台、上线，或收到会话变更 / 运行结束时才再请求。一条挂着的 SSE 就够。

局域网桌面嵌入的服务仍走内存 `AuthStore`，空闲会撤销并结束流，不受这一条影响。受影响的是 headless 服务：`ServerLifecycle` 把 `PersistentAuthStore` 交给同一个 `event_stream`。

位置：`src/limbowave/web/stream.py` 的 `event_stream` 与 `RevocableEventResponse.invalidated`；`src/limbowave/server/auth.py` 的 `valid`；`src/limbowave/web/auth.py` 的 `valid`；`tests/integration/test_web_stream.py`。

## 2. 全量快照的游标可以比内容新

`ChangeLogStore.snapshot()` 在一个工作单元里解密全部会话、分支、消息（含思考与工具步骤）、运行和记忆。这个 `with` 结束、只读事务退出之后，才用另一条连接读取 `change_log` 的最大 `seq` 和 `sync_state.revision`。

SQLite WAL 的读事务看不到事务开始之后才提交的写入。那些写入的序号却会被后面的游标读到。客户端保存这份快照和游标之后，`GET /api/v1/sync/changes` 只返回 `seq` 更大的记录，扫描窗口里的消息不会再出现。下一次全量快照如果再次碰上并发写入，窗口还在。

返回体里的 `truncated` 固定为 `false`，没有分页，也没有上限。扫描在异步的 `sync_snapshot` 里同步执行，会占住事件循环。`docs/development/server.md` 的小实例基线是 1 vCPU、1–2 GiB 内存。历史变长以后，这一次解密可以顶满这台机器。

设备心跳把窗口拉宽。`PersistentAuthStore.authenticate()` 每次触摸都会 `UPDATE server_devices.touched_at`。`_migration_016` 对 `server_devices` 建了 INSERT / UPDATE / DELETE 触发器，和会话、消息用的是同一套：更新 `sync_state.revision`，再插入 `change_log`。快照文档写明不包含设备授权，这些行却会计入游标。一次普通的会话列表或状态请求，也会在没有新消息时把游标往前推。

`prune_through()` 只在 `tests/unit/test_change_log.py` 和 `tests/integration/test_sync_protocol.py` 里被调用。生产路径不清理，变更表只增不减。

网页聊天走 `/api/v1/conversations` 和分页消息，不走这份快照，所以现在用手机发消息不会因此缺历史。会缺内容的是 PC 远程缓存。`SyncClient.snapshot()` 把整份 JSON 存成 `remote_cache` 的一行，然后把状态游标设成响应里的 `cursor`。它不补拉扫描期间的变更，也不按 `entity_type + entity_id` 取回实体。安静库上的集成测试看到的游标和内容来自同一次静止状态，看不出这个空洞。

这和巡检第 7 条是两件事。第 7 条是增量日志只有元数据、客户端还没按实体应用。这里是全量快照本身的游标可以跳过已经提交的正文。

位置：`src/limbowave/infrastructure/database/change_log.py` 的 `snapshot` 与 `_cursor_state`；`src/limbowave/infrastructure/database/migrations.py` 的 `_migration_016`；`src/limbowave/server/auth.py` 的 `authenticate`；`src/limbowave/web/app.py` 的 `sync_snapshot`；`src/limbowave/application/services/sync_client.py`。

## 3. 网页请求体上限是 64 KiB，桌面发送没有这一层

`WebConfig.max_body` 与 `ServerWebConfig.max_body` 都是 65536。局域网服务用默认 `WebConfig`，公网示例 `deploy/limbowave-nginx.conf` 把 `client_max_body_size` 也设成 `64k`。超过上限时中间件返回 413，错误码 `body_limit`，页面上的句子是把下划线换成空格后的 `body limit`。

桌面 `RunCoordinator.send()` 只去掉正文两端空白，不按这个字节数拒绝。同一段长提示在电脑上能发，在手机上失败。UTF-8 汉字约 3 字节，再算上命令 JSON，大约两万字就会顶到上限。

位置：`src/limbowave/web/security.py`；`src/limbowave/server/web_config.py`；`deploy/limbowave-nginx.conf`；`web/src/api/client.ts` 与 `web/src/state/controller.ts` 的错误展示；`src/limbowave/application/services/run_coordinator.py` 的 `send`。

## 4. 按部署文档上机时没有模型内核

`docs/development/server-deployment.md` 的步骤是建系统账号、拷贝入口、`server init`、安装 systemd 与 nginx、SSH 解锁和配对。里面没有安装 Node，也没有安装合同里的 Pi 0.86.0。

`scripts/packager.py` 打进去的是 Python 应用和网页静态资源，不含 Node。`deploy/limbowave.service` 的账号是 `nologin`，工作目录是 `/var/lib/limbowave`。找不到 Pi 时 `build_kernel()` 记下 `kernel.unavailable` 并返回 `None`，进程仍可解锁，网页却没有可调用的模型。

开发基线写在另一份文档里：`docs/development/server.md` 要求 Node.js 和 Pi CLI，`docs/development/linux.md` 把包固定为 `@earendil-works/pi-coding-agent@0.86.0`。Linux CI 会全局安装这个版本。按部署文档在一台干净 VPS 上操作时，这两步都不会发生。

位置：`docs/development/server-deployment.md`；`deploy/limbowave.service`；`src/limbowave/composition.py` 的 `build_kernel`；`docs/architecture/pi-runtime-contract.md`。

## 建议的核对

1. 用 `PersistentAuthStore`、`idle_ttl` 短于测试时长，挂一条 SSE，断言空闲后流结束，且空闲等待期间不会按事件循环空转查询数据库。设备记录保持未撤销。
2. 快照扫描进行中插入一条消息，断言该消息要么出现在快照正文里，要么其 `change_log.seq` 大于返回的 `cursor`。设备 `touched_at` 更新不得写入内容同步日志。
3. 在手机页发送一段超过 64 KiB 的正文，确认失败提示说明的是长度限制；若这个上限要保留，桌面与网页的差异需要写进局域网说明。
4. 在未安装 Node 的 Linux 上按 `server-deployment.md` 走一遍解锁和发送，部署步骤里补上 Pi 0.86.0 的安装与服务账号的 `PATH`。


---

## 修复回填（2026-10-13）

以上保留首次只读巡检原文；以下为本次实际修改和验证结果。

### 已落地

1. **事件流空闲退出**：SSE 生成器和阻塞写监视任务统一检查连接剩余寿命，到期终止，不再按零超时重试；持久设备授权保持未撤销。局域网原有的到期撤销行为保留。测试同时覆盖正常等待与阻塞网络写，限制授权检查次数并验证新实例仍可重新认证设备。
2. **快照一致性与资源边界**：SQLite 显式 `BEGIN`，内容、游标、revision 都来自同一个工作单元连接和读视图；分别在会话扫描和消息扫描时并发提交消息，验证遗漏于快照的消息仍出现在后续变更页。游标取持久序号高水位，清空日志也不回退。快照在后台线程解密和编码，工作线程持有互斥锁直到结束，HTTP 请求取消也不会放入第二份并行导出。默认预检 10,000 实体 / 8 MiB 存储字段，另限 8 MiB JSON 响应；超限返回 `413 snapshot_too_large`，不返回部分内容或游标，也不改写已有客户端缓存。忙时返回可重试的 `503 snapshot_busy`。
3. **日志维护**：新增 schema 17 迁移，移除设备 INSERT/UPDATE/DELETE 同步触发器及历史设备日志，不重置序号/revision、不删除设备授权。内容写入时由事务内触发器自动保留最近 50,000 序号窗口；旧游标按协议要求重新全量同步。已测旧版数据库升级、心跳无日志和生产保留窗口。
4. **长正文提示**：保留 64 KiB HTTP 防护，不给桌面发送新增限制。Python 的 `body_limit` 与 nginx HTML 413 均显示中文长度限制提示；UTF-8 长正文及分块上传会在命令执行前被拒绝，页面保留草稿，不重试或查询发送回执。桌面/网页差异已写入局域网说明。
5. **部署前置条件**：部署文档补上 Node >=22.19.0、精确 Pi 0.86.0、官方 Node 24 LTS 安装示例、校验与服务账号预检；systemd 显式设置 HOME/PATH，并在启动前检查 Node/Pi 能否运行。保留 nologin、ProtectHome 和根目录只读保护，运行时安装目录由 root 持有。验收要求真实模型回复，而不是只看解锁或健康检查。

### 验证记录

- 本次相关 Python 回归：**59 passed**（事件流、同步、入口安全、部署合同、设备授权、缓存）。新增测试先在原实现上确认关键失败，再修复。
- 全部前端单元测试：**53 passed**；TypeScript 检查与生产构建通过。
- 修改过的 Python 文件 Ruff 检查通过；全项目 mypy：**211 个源文件通过**；差异空白检查通过。
- 扩大到 Python 单元与集成测试的一次运行：**2031 passed / 4 failed / 7 skipped**。4 个失败分别是：压缩历史边界的 memory 用例、OOBE 硬编码字号、模型切换图片端到端的 fork/restore 用例。
- 以修改前 `HEAD=35c7d8b` 导出独立源码并复跑：OOBE 和图片端到端的 **3 个失败原样复现**；压缩 memory 用例在原版与当前单独重跑均通过，保留为一次全套运行中的未稳定复现失败，不宣称全套全绿。
- 构建有上游 Zod/Rollup 注释位置警告，不影响产物生成。Linux 特有测试在本机 Windows 上按标记跳过。

### 尚未宣称完成的验收

本次没有在干净 Linux VPS 实际安装、启用 systemd/nginx 或调用真实模型，部署修复通过的是文档/unit 合同检查。全量快照尚无分页：超过上述预算会明确拒绝，不会无界解密或悄悄跳游标。PC 增量按实体写回、回执与用户消息原子性，以及 Windows CI/网页端到端接入流水线仍属前一份巡检的独立事项，本次未改动。
