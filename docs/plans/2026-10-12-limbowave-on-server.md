# LimbowaveOnServer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在一台 Linux VPS 上运行单用户 LimboWave 权威实例，使 PC 关机后仍可通过网页正常对话；PC 客户端恢复联网时静默同步会话与记忆。VPS 每次启动后必须由用户手动解锁，绝不把可自动解锁的凭据落盘。

**Architecture:** 服务端独占权威资料库、模型调用和 Agent Runtime；网页与 PC 远程模式都通过同源 HTTPS API 操作服务端。实时生成走现有 SSE，持久化会话/记忆变更走新建的数据库事务内变更日志与游标；PC 只维护独立加密的只读缓存和本地草稿，不形成第二个可写主库。单用户、单进程、同一时刻最多一个运行中的模型任务是首版的刻意约束。

**Tech Stack:** Python 3.12/3.13、FastAPI/Uvicorn、SQLite WAL、现有 Vault/MemoryService/Pi Runtime、PySide6 桌面 UI、现有 Web 前端、Linux systemd 与反向代理、pytest/pytest-qt、Ruff/mypy。

---

## 已确认的边界与不做的事

- 当前 `src/limbowave/web/server.py` 是桌面 qasync 中的嵌入式服务；`src/limbowave/app.py` 约 4200 行处由 GUI 的 `SessionController` 构造 `RuntimeFacade`，不能直接当 Linux 守护进程运行。`src/limbowave/web/security.py` 的 `WebConfig` 只允许具体私有 IPv4 地址，且校验证书 IP SAN；公网域名 + loopback 反代需要独立部署配置和 Host/Origin 信任边界，不能把 `allow_insecure_http` 用于公网。
- `src/limbowave/web/auth.py` 的 `AuthStore`、`src/limbowave/application/services/command_receipts.py`、`event_broker.py` 均以进程内状态为主。重启后必须持久化设备授权/撤销、命令接收回执和同步游标；SSE 仍只是实时提示，不承担持久同步。
- 当前 `RuntimeFacade` 有单个当前会话/分支和全局 revision。服务端不得把浏览器 A 切换会话当成浏览器 B 的当前会话；操作必须显式携带目标 ID 与版本，首版跨会话写操作串行化，运行归属按目标会话/分支校验。
- 记忆包括全局条目、会话级全局覆盖/删除、分支条目及按轮次保存的快照；同步必须保留原始 ID、作用域和删除语义，不能“同步一段记忆文本”。模型写入审批仍由 `MemoryService` 执行；服务端高危工具不因网页登录而自动获权。
- **不做**多用户、多服务端副本、离线发送/编辑后合并、自动 VPS 解锁、把模型凭据交给浏览器、全量端到端加密、同步 SQLite/WAL 文件、首版跨设备附件/本地文件工具、在 VPS 开放 shell/任意本地文件/浏览器自动化工具。所谓“加密存储”不是“服务器无法解密”：VPS 解锁运行期间有明文密钥驻留内存。
- 工作区目前已有记忆、备份与恢复的未提交变更。本计划只新增本文档；真正实施前确认这些变更的基线与测试状态，不覆盖或顺手提交它们。

## 交互与数据流

```text
浏览器 -- HTTPS --> 反向代理 -- loopback --> headless FastAPI -- 服务层/Pi -- VPS SQLite + Vault
PC 远程模式 -- HTTPS --> 同一 API           |                         |
   |                                   SSE（实时）          事务内 change_log
   +-- 本地 Vault 加密缓存、离线只读、草稿 -- 游标拉取/重建 -------+
```

- 开机：systemd 启动锁定态进程，仅健康探针可用；SSH 进入 VPS，以交互式 TTY 输入主密码给权限受控的本机 Unix socket 上的 `limbowave server unlock` 命令；解锁成功后加载密钥、数据库、凭据、模型与运行时并开放经认证的数据 API。密码不得出现在命令行参数、环境变量、journald 或磁盘文件。重启后重复此步骤；不在时如有可信 SSH 可远程操作。锁定态网页只展示“待解锁”，不得泄露数据/用户存在性或提供公网密码尝试面。
- 会话/记忆写入：请求携带设备身份、唯一操作 ID、实体预期版本与明确作用域；服务层在同一事务内完成变更和变更日志。冲突返回服务端版本而不静默覆盖；新消息提交回执先于模型运行启动。重连先查命令回执，再按持久游标拉取；不能因为 404/超时自动重发不确定的 `send`。
- 生成中：现有 SSE 推送文本/工具状态；断线后按服务端消息/运行状态重新获取，而非重放带副作用的命令。锁库/重启中断运行标记为中断，不能自动重新调用模型。
- PC：登录远程实例时先完成一次受控导入/切换；此后本地旧库保留为可回滚副本但不再写入。轮询或 SSE 提示只触发增量拉取，缓存更新在本地单事务内完成；服务器游标只有提交缓存后才前移。离线能看已缓存内容、保留草稿，发送/记忆编辑明确不可用。

## 阶段与任务（每项先失败测试，再最小实现，再回归；每个任务单独提交）

### Phase 0 — 先证明 Linux 可运行

**Task 0.1：Linux headless 预检与运行合同。**
- 检查 `src/limbowave/composition.py`、`src/limbowave/infrastructure/pi_adapter.py`、`src/limbowave/infrastructure/pi_runtime/` 和 `src/limbowave/application/services/run_coordinator.py` 的 Linux 依赖、Pi 可执行文件定位、工作目录、子进程清理、平台专用工具/DPAPI 路径；确定服务端最小安全工具白名单。新增 `tests/integration/test_server_linux_smoke.py`，在 Linux CI 上验证“空实例配置、解锁、模型路由、1 轮流式回复、关停”；模型调用用假内核，另以人工测试核对真实 Pi。不能运行时先修跨平台差异，不以 GUI 的 offscreen 模式冒充 headless。
- 在 `docs/development/` 增加环境依赖及资源基线（CPU/RAM/磁盘、并发上限）；小 VPS 压测失败则限制功能/规模，不假设可用。

### Phase 1 — 独立服务端与手动解锁

**Task 1.1：抽取 Qt-free 组合根。**
- 新建 `src/limbowave/server/composition.py`、`src/limbowave/server/lifecycle.py`；复用 `src/limbowave/composition.py`、`runtime_composition.py`、`SessionController`、`RuntimeFacade`、`MemoryService` 等业务服务，不复制聊天逻辑；把 GUI 独有的窗口、提示、剪贴板和 Windows 恢复路径留在桌面入口。需要对 `src/limbowave/app.py` 作最小重构时，先保持原 LAN 行为测试不变。
- 新增 `tests/integration/test_server_lifecycle.py`：锁定态不能构造可用运行时；解锁→运行→锁定/关闭的异常清理、进程互斥、失败回滚；`tests/integration/test_web_lifecycle.py` 必须继续通过。

**Task 1.2：服务端 CLI/锁定态。**
- 扩展 `src/limbowave/cli.py`，新增 `src/limbowave/server/__main__.py`、`control.py`：`server init` 创建独立资料目录和主密码包装；`server run` 不读取密码就启动锁定态；`server unlock` 用交互式 TTY 取密码，经 owner-only Unix socket 送入运行进程，失败限速/统一错误；`server lock` 撤销会话并结束活动运行。既有 `Vault` 密码包装复用，禁止自动解锁文件和命令行密码选项。
- 新增 `tests/unit/test_server_control.py`、`tests/integration/test_server_locked.py`：锁定/解锁、错误密码、并发尝试、权限不足、信号与重启；断言进程参数、环境、日志、HTTP 响应均无密码/主密钥。Vault 解锁后仍需清理用户输入引用；文档明确 Python 内存不保证可靠擦除。

**Task 1.3：服务端安全权限默认值。**
- 新建 `src/limbowave/server/tool_policy.py`，集成 `application/services/tool_gateway.py`、`permission_service.py`：首版仅允许对话及经独立审批的记忆写入；拒绝或不可注册 shell、任意路径读写、桌面交互及其他宿主高危工具。不能仅隐藏网页按钮。
- 新增 `tests/integration/test_server_tool_boundary.py`：从恶意提示词、直接 API 和伪造工具回执均不能越权；人工核对服务进程的 Linux 用户权限。

### Phase 2 — 公网访问的认证与重启一致性

**Task 2.1：反代安全边界，不直接开放现有 LAN socket。**
- 新建 `src/limbowave/server/web_config.py`，调整 `src/limbowave/web/security.py` 和 `web/app.py` 使部署模式仅绑定 loopback/Unix socket，并显式配置唯一公开 HTTPS origin；代理负责 TLS，应用验证 Host/Origin 与可信代理头、CSRF、Secure/HttpOnly/SameSite Cookie，绝不信任客户端直传的 `X-Forwarded-*`。生产环境无公开 HTTPS origin 时拒绝启动。反代配置示例放 `deploy/`，并在防火墙层拒绝直连应用端口。
- 新增 `tests/integration/test_server_proxy_security.py`：非 HTTPS 原始入口、伪造转发头、错误 Host/Origin、跨站写请求、超大请求、SSE 断线、日志脱敏均被拒绝或安全降级；原 LAN 测试不受影响。不要把 IP SAN 约束简单删除后复用于现有 LAN 模式。

**Task 2.2：持久设备身份与受控配对。**
- 扩展 `src/limbowave/web/auth.py` 或增设服务端持久适配器；通过 `src/limbowave/infrastructure/database/migrations.py` 新增设备表（设备 ID、仅散列的 token/元数据、过期、撤销），服务器解锁后由本机 CLI 开启一次短时配对窗口；首个浏览器/PC 设备也须带该一次性凭据。支持列出/撤销设备、会话过期与令牌轮换，不因重启重新开启匿名配对；严禁将 auth token/主密码写入可读日志。
- 新增 `tests/unit/test_server_auth.py`、`tests/integration/test_server_pairing.py`：重启后已授权设备仍可用、被撤销设备无法复活、锁定态不可配对、爆破限速、无授权时访问全部数据/API/SSE 均失败。原内存 `AuthStore` 用于 LAN 的语义保留。

**Task 2.3：持久化接收回执与安全重试。**
- 新增 `src/limbowave/server/command_store.py` 与迁移表，改造 `runtime_facade.py` 中的服务端命令注册：唯一键 `(device_id, operation_id)`、请求指纹、接收状态和结果均需可恢复；新建消息和接收回执在同一事务提交，后台运行状态另行落库。复用现有 `client_command_id`、`server_epoch` 合同，明确重启后旧 epoch 的查询/恢复通道；丢失回执不能自动重发。
- 新增 `tests/unit/test_server_command_store.py`、`tests/integration/test_server_send_recovery.py`：模拟“提交后断线/杀进程/重试”，只能存在一条用户消息和至多一次已接受运行；拒绝不同载荷复用 ID；服务端重启后未完成运行标记中断并要求人工重试。

### Phase 3 — 网页独立对话与记忆

**Task 3.1：消除远程设备共享‘当前会话’副作用。**
- 改造 `src/limbowave/application/services/runtime_facade.py`、`session_controller.py`、`run_coordinator.py`：命令明确指定会话/分支及实体版本；服务端持有自己的“当前活动运行”，客户端的导航只是本地 UI 状态；首版全局串行运行，忙时返回可解释的状态而不是切换别人的会话。桌面 LAN 兼容路径保留并测试。更新 `src/limbowave/web/schemas.py`、`web/API-CONTRACT.md` 和 `web/src/api/adapter.ts` 的版本合同。
- 新增 `tests/integration/test_server_multi_device.py`：网页 A 查看会话 A、PC 查看 B、同时发起请求/切换/中止，均不得串上下文、误终止他人运行或串记忆；旧版客户端收到明确升级错误。

**Task 3.2：网站完整对话路径。**
- 复用 `web/src/` 及 `/api/v1`，增加锁定态、服务端状态、会话/分支导航、模型选择、生成中 SSE 重连和错误提示；UI 不直接触碰 Vault/模型密钥。文本对话为 MVP 验收；附件/图片、复杂工具审批列为后续独立任务，不能假装首版支持。
- 扩充 `tests/integration/test_web_runtime.py`、`web/tests/`：PC 完全关机时的浏览器对话、刷新/掉线/服务器重启后的历史和运行状态、冲突展示；至少一次真实浏览器端到端测试。

**Task 3.3：记忆 API 与网页界面。**
- 新增 `src/limbowave/web/memory.py` 或在 `web/app.py` 注册同源路由，直接调用 `MemoryService`：全局列表、会话级覆盖、分支记忆、升格、策略及明确的版本冲突；删除保留墓碑，轮次快照只读、不允许客户端覆写历史。更新 `web/src/` 记忆管理 UI，修改权限须二次确认，模型写入仍遵循现有审批策略。
- 新增 `tests/integration/test_web_memory.py`、`web/tests/`：全局变更传播、会话覆盖、分支分叉、删除/恢复、并发编辑、未授权读写；运行 `tests/unit/test_memory_service.py tests/integration/test_memory_pi.py` 回归。

### Phase 4 — PC 自动静默同步（服务器主库）

**Task 4.1：持久增量变更日志和游标协议。**
- `src/limbowave/infrastructure/database/migrations.py` 新增 `change_log(seq, entity_type, entity_id, scope, operation, revision, ...)` 与实体版本/墓碑；在 `HistoryService`、`MemoryService` 对应**持久化业务写入**的同一事务附记变更（可由 UoW 提供 `record_change`，禁止业务写完后异步补日志）。日志只存最小元数据，敏感内容由认证读 API 获取。新增 `GET /api/v1/sync?cursor=…&limit=…` 和受界限快照接口；游标过期返回 `resnapshot_required`，重建快照期间的后续变更不得丢失。
- 新增 `tests/unit/test_change_log.py`、`tests/integration/test_sync_api.py`：提交/回滚原子性、删除墓碑、分页、并发写与快照边界、版本升级、事件留存清理、旧游标恢复。SSE 只唤醒拉取，不代表已落库。

**Task 4.2：PC 远程模式与独立加密缓存。**
- 新建 `src/limbowave/application/services/sync_client.py`、`src/limbowave/infrastructure/database/remote_cache.py`；PC UI 在 `src/limbowave/app.py` 增加“连接服务器”受控模式，远程模式不打开本地权威 `SqliteUnitOfWorkFactory` 进行聊天写入。PC 为远程缓存使用独立本地 `Vault` 和设备授权令牌的受保护存储（Windows 平台可用系统保护；不可用时不默存裸令牌）；绝不从服务器下发服务器主密钥。先完整快照后增量拉取，游标随缓存事务提交；指数退避、有界队列、显式最近同步时间/错误状态。
- 新增 `tests/unit/test_sync_client.py`、`tests/integration/test_remote_cache.py`、`tests/ui/test_remote_mode.py`：断网离线只读和草稿、掉线恢复、多页同步中断不丢不重、注销/撤销清理缓存、服务器密钥绝不进入 PC。界面不得把“暂存草稿”误说成“消息已发送”。

**Task 4.3：本地已有数据的单向迁移与回滚。**
- 待工作区现有备份/恢复改动定稿后，复用 `application/services/backup_service.py`、`restore_service.py` 的校验/暂存设计写一次性 `server import`；导入前 VPS 自动加密备份，源 PC 保持原状。服务端导入须校验 schema/版本/密钥包装及 blob 覆盖；导入成功后 PC 才切到远程模式，绝不让两份旧库并行可写。导入后新增的 ID 冲突必须中止并报告，绝不按时间戳“最后写入者胜”。
- 新增 `tests/integration/test_server_import.py`：空库导入、坏档/错误密码/版本不兼容回滚、WAL 未检查点、重复导入幂等/拒绝、分支记忆与会话覆盖对照、服务端恢复快照；手工走完整迁移演练。

### Phase 5 — 部署、灾备、验收

**Task 5.1：最小权限部署。**
- 新建 `deploy/` 的 systemd unit、反代配置示例及 `docs/development/server-deployment.md`：专用非 root 用户、单实例独占资料目录、loopback 监听、HTTPS 反代、必要的安全响应头/请求大小限制、仅开放 443/SSH、受控日志和磁盘容量告警；关闭公开调试文档、默认不给 shell/文件工具。配置单元测试/静态检查和 Linux 临时机部署测试；不要把任何私钥/凭据放仓库。

**Task 5.2：加密备份与恢复演练。**
- 使用 SQLite 官方一致性备份接口与现有加密档案流程（不复制活跃 `.db`/`-wal`），定期离机备份数据库、附件 blob、配置、Vault 包装及密文凭据；明确备份密码与服务器主密码的保管、轮换和丢失恢复流程。加入备份/恢复脚本与 `tests/integration/test_server_backup_restore.py`，至少一次在全新资料目录手工恢复并核对会话、记忆、设备撤销和同步游标。恢复后需要强制重建客户端快照与重新评估设备授权。

**Task 5.3：发布闸门。**
- 在 Linux CI 和 Windows 桌面 CI 分开执行目标回归：`uv run pytest tests/unit/test_memory_service.py tests/integration/test_web_api.py tests/integration/test_web_security.py tests/integration/test_web_runtime.py tests/integration/test_memory_pi.py`，加上本计划新增测试；`uv run ruff check src tests`、`uv run mypy src`、`cd web && npm run typecheck && npm test && npm run test:e2e`。完整 Windows 现有 LAN/Qt 回归、Linux 真实 Pi 一轮调用、浏览器完整链路、重启锁定→SSH 解锁→恢复服务、PC 离线→再连→游标续传、备份恢复均通过后才对个人 VPS 放量。

## 验收标准

1. VPS 解锁后，PC 彻底关机，浏览器仍可新建会话、连续对话、查看/管理记忆；运行时内存/响应不暴露模型凭据。
2. VPS 重启后默认锁定，任何旧设备都读不到数据或触发运行；用户 SSH 交互解锁后恢复服务，已授权设备可按策略继续使用。无法 SSH 解锁是明确的可用性限制，不暗藏自动解锁后门。
3. 浏览器与 PC 不共享导航状态；刷新、断网及进程崩溃不会重复发送消息/越过权限。未知状态保留为“待确认”，不能自动重试可能已执行的副作用。
4. PC 同步的全局、会话覆盖和分支记忆/删除与服务器一致；服务端冲突显式处理；离线只读/草稿行为清晰，缓存加密且无服务器主密钥。
5. 网页公网入口具备 HTTPS、持久设备撤销、限速、CSRF/Origin 保护和默认拒绝高危工具；备份能在新环境恢复。旧桌面 LAN 模式不退化。

## 关键决策与风险（ADR 待实现前落档）

- **服务端主库而非双向双主：** 放弃离线写入，换取可解释的同步、低运维成本。以后要支持离线发消息需另做冲突/执行语义。
- **手动解锁与 SSH 本机控制面：** 重启自动可用性让位于磁盘凭据安全；运行期 VPS 被入侵仍可能暴露密钥，不能宣称防在线主机入侵。
- **单运行/单进程：** 现有 `SessionController` 强状态，先通过显式会话作用域与序列化避免串会话；性能不足再设计每会话 Actor/运行池，不预建微服务。
- **鉴权/命令/同步分别持久化：** 内存 AuthStore、receipt、EventBroker 不是跨重启的事实来源；更改这三层有向后兼容风险，须以旧 LAN 合同回归为门槛。
- **迁移依赖尚未合并的备份/恢复改动：** 必须在冻结数据格式与跨平台恢复验证之后执行，不直接挪动或 rsync 活库。

## 参考与实施注意

- 官方 SQLite WAL 文档明确不应将 WAL 数据库部署在网络文件系统；在线备份 API 提供活库一致性快照。服务端数据库留在 VPS 本地块设备，PC 通过 API 同步。
- OWASP Session Management / Authentication 指南可用于校验 cookie、会话撤销和认证限速；以仓库现有更严格的边界为准。
- 现有文档：`web/API-CONTRACT.md`、`docs/development/memory.md`、`docs/architecture/adr-0002-vault-encryption.md`、`docs/plans/2026-10-10-safe-backup-restore.md`。其中最后两份及相关源码当前可能包含工作区未提交内容，实施时重新核对。
