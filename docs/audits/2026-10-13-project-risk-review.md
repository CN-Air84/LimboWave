# 项目可用性与交付风险巡检（已核对）

> **核对时间：2026-10-13。**  
> 下面先给出逐项核对结论；原始巡检内容保留在后文，便于追溯。

## 核对结论

| # | 风险 | 结论 | 依据 / 现状 |
|---|---|---|---|
| 1 | Pi 事件队列无上限 | **已修复** | `PiKernelAdapter` 队列改为上限 1024，满时丢弃最旧事件；新增 `tests/unit/test_pi_adapter_queue.py`。 |
| 2 | 停止超时后永久 busy | **已修复** | `RunCoordinator.abort()` 捕获 `TimeoutError`，调用 `mark_interrupted()` 并清 busy；新增回归测试。 |
| 3 | 配置 / 密钥直接覆盖原文件 | **已修复** | 新增 `atomic_write_text()`，`JsonConfigRepository` 与 `SecretStore` 均改为同目录临时文件 + `os.replace()`。 |
| 4 | 备份先拍数据库、后拷附件导致不一致 | **不成立（按当前写入顺序）** | 附件导入是“先写 blob，后写 DB”；删除是“先删 DB，后删 blob”。因此“先快照 DB，再收集 blob”不会产生 DB 引用缺失，最多多收未被引用的 blob。若未来写入顺序改变，需要重新评估。 |
| 5 | 服务端未挂存储进程 | **已修复** | `build_server_runtime()` 已创建并挂接 `ConversationProcess`，`ServerRuntime.close()` 会关闭它；新增测试。 |
| 6 | 回执与用户消息分两次提交 | **仍存在** | `PersistentCommandStore.register()` 与 `_persist_run_start()` 仍是两个独立事务。重启后 pending 回执会被标为 failed，但无法保证回执和消息的原子性。 |
| 7 | 同步快照重建不了客户端 | **大部分已修复** | 新增 `ChangeLogStore.snapshot()`，返回 conversations / branches / messages / runs / memories 与游标；已有集成测试验证可重建。但增量变更仍只含元数据，客户端尚未实现按实体取回并应用，增量部分未完全闭环。 |
| 8 | 空闲两小时会永久撤销设备 | **已修复** | `PersistentAuthStore` 不再把 idle 过期写入 `revoked_at`；只有 absolute expiry 或显式 revoke 才撤销。 |
| 9 | 工具策略对象未接入运行时 | **已修复** | `ServerToolPolicy` 决定 `allowed_tools`，并传入 `EnvironmentBuilder.build()`，Pi `settings.json` 只注册 `add_session_memory`。 |
| 10 | 一个全局版本号会被别的操作顶掉 | **已修复** | `switch_conversation()` 改为导航专用装饰器，不再 bump 全局 revision；新增 `test_navigation_does_not_bump_execution_revision`。 |
| 流水线 | 缺 Web 测试 / Pi 版本 / Windows CI | **部分修复** | Linux CI 已加入 Web typecheck + 单测，Pi 固定为 0.86.0，`test_composition` 改用 `resolve_pi_argv()` 判断；Windows CI 仍未补。 |

## 仍然需要处理的项

1. **回执与消息原子性**：需要把回执注册并入用户消息落库的同一事务，或引入可恢复的两阶段标记。
2. **增量同步实体取回**：客户端已能保存快照和变更元数据，但还没有按 `entity_type + entity_id` 拉取最新实体并应用到本地缓存。
3. **Windows CI**：仍缺独立的 Windows 桌面回归任务。

---

# 项目可用性与交付风险巡检

日期：2026-10-13。范围：当前工作树的桌面运行时、资料库写入、以及正在进行的服务端 / 网页同步。只读核对，没有改这些路径上的代码，也没有跑测试。

计划里已经写明的约束不再重复：单会话、手动解锁、不做离线写入、首版不做附件同步。下面是对照调用处之后，仍会卡住日常使用或让网页交付在测试变绿后对不上验收的问题。

## 当前桌面

### 1. 流式事件进了队列，没有人取

> 状态（2026-10-13 复查）：已处理（Codex，工作树未提交）

`PiKernelAdapter` 每收到一个内核事件，都放进没有上限的 `asyncio.Queue`，同时再交给订阅回调。全仓库没有调用 `events()` 的地方。`message_update` 的每一个字都留在这个队列里，直到进程退出。

桌面长时间开着会涨内存。以后常驻的服务端进程更明显，短测试看不出来。

位置：`src/limbowave/infrastructure/pi_adapter.py` 的 `_publish` 与 `events`。

### 2. 停止等满 120 秒仍不解除忙碌

> 状态（2026-10-13 复查）：已处理（`RunCoordinator.abort` 超时后强制收尾并清除 `_busy`，仍向上抛出超时）

`abort` 向 Pi 等待 120 秒。超时后桌面把异常显示成「后台操作失败」，网页把命令记成 `command_failed`。`_busy` 只在本轮正常结束、进程退出，或发送前就失败时清掉，超时本身不清。

Pi 卡住、不再发出结束或退出事件时，之后的发送会一直被拒绝，直到重启进程。

位置：`src/limbowave/infrastructure/pi_adapter.py` 的 `abort`；`src/limbowave/application/services/run_coordinator.py` 的 `_busy`；`src/limbowave/app.py` 的停止按钮。

### 3. 配置和密钥直接覆盖原文件

> 状态（2026-10-13 复查）：已处理（`atomic_write_text`，工作树未提交）

`config.json` 与 `secrets.json` 用 `write_text` 打开即截断。Vault 本体是先写入临时文件，再 `os.replace`。

写到空文件时，配置加载把空内容当成还没配置，站点和模型回到默认。密钥文件先被截断，旧密文在新内容写完之前就已经没了；写了一半的 JSON 会让下次读取失败。

位置：`src/limbowave/infrastructure/configuration/json_config_repository.py`；`src/limbowave/infrastructure/crypto/secret_store.py`；对照 `src/limbowave/infrastructure/crypto/vault.py`。

### 4. 备份先拍数据库，后拷附件

> 状态（2026-10-13 复查）：待处理（计划 Phase 5：备份与恢复）

数据库使用 SQLite backup API，能带上仍在 WAL 里的已提交页。附件是之后对 `blobs/` 的另一次目录读取，中间没有停写。

备份过程中如果正在导入附件，归档可以生成，恢复时会因为数据库引用了未收入归档的附件而被拒绝。这是安全网失效，不是静默写坏一份旧库。

位置：`src/limbowave/application/services/backup_service.py` 的 `_collect`。

## 网页交付

这几项都已经有模块、路由或测试。对照调用处之后，边界还没接上。按现在的形状收尾，PC 静默同步和过夜登录会对不上计划里的验收。

### 5. 服务端没挂上存储进程

> 状态（2026-10-13 复查）：已处理（`composition.py` 已挂上 `ConversationProcess`，工作树未提交）

桌面在 `src/limbowave/app.py` 里把 `ConversationProcess` 赋给 `storage_worker`。`src/limbowave/server/composition.py` 没有这一步。`storage_worker` 为空时，落库、切会话和收尾都在 asyncio 循环里同步执行。网页的流式输出会在写库和解密时停住。

### 6. 回执和用户消息分两次提交

> 状态（2026-10-13 复查）：待处理（计划 Task 2.3：回执与消息同一事务）

`PersistentCommandStore.register` 先单独提交 `pending`。用户消息和运行记录在另一条连接里提交。完成状态由 `finish` 再写一次。进程在中间被杀掉后，重启把所有 `pending` 标成 `failed`。

消息已经在库里时，同一个操作编号会卡在失败回执上；换一个编号会再写一条用户消息。计划要求消息和回执在同一事务里提交，当前实现不是。

位置：`src/limbowave/server/command_store.py`；`src/limbowave/application/services/runtime_facade.py`；`src/limbowave/application/services/run_coordinator.py` 的 `_persist_run_start`。

### 7. 同步快照重建不了客户端

> 状态（2026-10-13 复查）：待处理（计划 Phase 4：快照与同步日志）

`change_log` 触发器在同一事务里记下实体类型、id、`upsert` 或 `delete`，升级时不回填旧数据。`GET /api/v1/sync/snapshot` 只返回 `conversations(limit=100)` 和一个游标，没有分支、消息、运行或记忆。

记忆删除是改写文档：会话级删除把覆盖写成 `null`，分支或全局条目删除也是更新文档。触发器看到的是 `upsert`。只按删除操作应用日志的客户端，不会知道某一条记忆没了。

`tests/integration/test_sync_protocol.py` 只断言快照里出现了一个会话 id。这条测试通过，不代表客户端能够从快照加游标恢复。

位置：`src/limbowave/infrastructure/database/migrations.py` 的 `_migration_016`；`src/limbowave/web/app.py` 的 `sync_snapshot`；`src/limbowave/application/services/memory_service.py` 的 `_set_session_global`。

### 8. 空闲两小时会永久撤销设备

> 状态（2026-10-13 复查）：已处理（空闲不再撤销设备，只在绝对过期时撤销，工作树未提交）

`idle_ttl` 默认 7200 秒。过期时 `UPDATE` 写入 `revoked_at`，不是一次可恢复的会话过期。`list_devices()` 会先做这次清理。锁定服务的 `invalidate()` 明确不撤销已保存的设备授权，空闲却会。

过夜之后，本来要静默同步的电脑或手机需要重新配对。

位置：`src/limbowave/server/auth.py` 的 `_prune_expired`、`valid`、`list_devices`。

### 9. 工具策略对象没有接到运行时

> 状态（2026-10-13 复查）：待处理（计划 Phase 1：工具策略接入运行时）

`ServerToolPolicy` 只作为 `ServerRuntime` 的默认字段存在，`allows()` 没有调用者。`LIMBOWAVE_SERVER` 只被写进环境变量，没有读取处。Pi 的 `settings.json` 仍注册完整的应用工具表。

当前无界面进程没有安装权限处理器，确认返回拒绝，工具 IPC 也没启动，所以现在不会执行命令。`user_bash` 在确认返回真时把执行权交回 Pi，工作目录是资料库根目录。桌面的全权限处理器一旦接到服务端，这条通道会打开。策略文件本身不是那条边界。

位置：`src/limbowave/server/tool_policy.py`；`src/limbowave/server/composition.py`；`src/limbowave/infrastructure/pi_runtime/environment_builder.py`；`src/limbowave/infrastructure/extensions/policy_enforcement.ts` 的 `user_bash`。

### 10. 一个全局版本号会被别的操作顶掉

> 状态（2026-10-13 复查）：待处理（计划开头已决定改用带实体 ID 的版本）

成功的桌面导航会增加 `RunCoordinator._revision`。`runtime_transition` 结束时也会加。网页发送要求 `expected_revision` 等于这个数。桌面切换到另一个会话时，正在看原会话的网页会收到 `revision_mismatch`。

位置：`src/limbowave/application/services/run_coordinator.py` 的 `_arbitrated` 与 `runtime_transition`；`src/limbowave/application/services/runtime_facade.py` 的命令校验。

## 流水线

`.github/workflows` 里只有 `linux.yml`：ruff、mypy、pytest，以及打包后的 `--help`。没有 Windows 任务，不跑网页的 typecheck、单元测试和端到端。Node 24 会装上，Pi 不会。

`docs/architecture/pi-runtime-contract.md` 与适配器注释以 Pi 0.86.0 为合同。`docs/development/linux.md` 的安装命令没有版本号。`tests/integration/test_composition.py` 的跳过条件是系统里有没有 `node`，跳过原因写成需要已安装的 Pi。有 Node、没有对应 Pi 时，这组测试不会跳过，会在定位命令行时失败。

### 流水线状态

已处理：CI 与文档都固定安装 Pi 0.86.0；CI 增加网页 typecheck 与单元测试；	est_composition.py 的跳过条件改为检查 Pi 的 cli.js 能否定位。未处理：Windows 任务、网页端到端测试。
