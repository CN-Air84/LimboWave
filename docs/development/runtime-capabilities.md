# CAP-01：每次请求的运行时能力快照

## 数据链路

`policy_enforcement.ts` 的最后一个 `context` hook → 已认证工具 IPC 的内部
`capability_snapshot` operation → 应用校验当前运行 → `ToolGateway.runtime_capabilities()`。

扩展使用已确认的附件上下文通道取得 `run_id`（即使本轮没有附件），应用使用自己的活动运行与
会话标识；客户端不能选择其他会话。每次请求都重新读取，包括工具续轮和重试，不缓存成功快照。
身份校验在异步读取前后都执行。此 operation 不在模型工具列表中，不发放权限、不执行工具，
不产生工具调用审计；现有权限、来源、隐私和资源范围边界不变。

## 状态与权限

- `active`：服务已装配且当前运行可用，不代表网络连通、调用成功或无须审批。
- `unconfigured`：服务或所需配置/凭据不存在。
- `restricted`：来源、会话档位、活动上下文、隐私规则或实际工具列表不允许使用。
- `unknown`：未绑定活动运行、通道缺失/超时、响应身份或格式错误。不得沿用历史的 active 状态。
- `permission` 独立标记 `allowed`、`requires_confirmation`、`scoped`、`denied` 或 `unknown`。

涵盖搜索、URL 读取、提醒、后台监控、工作区读写、终端、附件、聊天记录、记忆新增/编辑/读取、
便签、时钟和环境数据。记忆编辑与新增单列，因为编辑始终逐次确认；聊天记录权限独立于普通档位。

搜索从当前设置及凭据引用存在性读取，不解密或输出 API Key，也不发起联网测试。
URL 读取、网页搜索与天气/环境数据是不同能力；搜索未配置并不等于设备断网。
CUSTOM 档位下，已配置的 `web_search` 显示 `restricted; permission=denied`，
无论是否存在 NETWORK 授权（包括 `*`）。原因是搜索规则要求逐次确认，而现有 CUSTOM
闸门只接受 ALLOW，没有搜索确认流程；快照不应承诺可确认。未配置搜索仍为
`unconfigured; permission=denied`。此修正不修改权限闸门、不新增审批流程，也不改变
URL 读取、READ_ONLY 或 FULL_ACCESS 的既有行为。
提醒显示 `active(local_only)` 时，仍依赖本地应用运行与资料库可用；只有 `set_reminder` 成功
才能说已安排。周期提醒不是持续联网监控。后台监控目前明确为未配置。

## 注入与失败行为

- 最多等待 IPC 1.5 秒，失败后以 unknown 继续普通对话。
- 与 Pi `getActiveTools()` 及确认界面可用性交叉检查，不把静态注册当作可执行能力。
- 使用 `display: false` 的临时 custom 消息，每次先删除旧同类 overlay；不修改用户原文，
  不写入 Pi 会话树，不进入历史压缩摘要。
- 在隐私投影后注入，内容只含固定能力名/枚举与边界说明，不包含路径、记忆、提醒正文或凭据。
- 提示模型在请求依赖不可用能力时开头说明边界；无关的普通对话不逐项播报。
- 模型遵循提示不等于硬保证；最终安全性仍由权限网关和工具执行结果负责。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_runtime_capabilities.py tests/unit/test_runtime_capability_extension.py tests/unit/test_tool_ipc_server.py tests/integration/test_runtime_capabilities_pi.py -q
```

Python 测试覆盖服务缺失、动态搜索配置/凭据撤销、档位/授权变更、远端来源、提醒上下文和记忆隐私。
Node 测试执行真实扩展代码及 TCP 通道，覆盖超时、错运行、畸形响应、无 UI、工具禁用、隐私顺序、
会话切换与 overlay 去重。真实 Pi + 本地 mock provider 测试确认快照确实到达出站请求，工具续轮
重新读取状态，切换会话后旧状态消失，且会话树没有持久化快照。

## 2026-10-27 CUSTOM 搜索交接补充

- 修复仅限 CAP 派生状态，不改 app.py、domain/permissions.py、policy_enforcement.ts 或 OOBE。
- 新增 CUSTOM × 有/无 NETWORK grant × 已配置/未配置共四种场景；同时验证 URL 状态、
  READ_ONLY/FULL_ACCESS 与切回 CUSTOM 的动态刷新、授权及审计不被快照修改。
- 修复前两项已配置场景失败，修复后能力测试全集及既有通配域名授权搜索闸门测试共 17 项通过。
- 相关两文件 Ruff 通过；tool_gateway.py 定向 mypy（follow-imports=silent）通过。
- 未执行大规模回归；统一集成测试由统筹会话在共享写入稳定后执行。
- 保留限制：CUSTOM 仍不能发起搜索；要改变它需另行明确授权设计审批流程，本次没有放宽。
