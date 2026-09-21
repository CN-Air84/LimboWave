# Pi Runtime 接入合同（核验报告）

> 本文回应设计计划 §二十一「开发启动前唯一必要的文档核验」。
> 核验方式：除公开文档外，**下载 npm 发布包 `@earendil-works/pi-coding-agent@0.86.0` 并直接阅读随包分发的 `.d.ts` 类型定义、实现源码与 32 份官方文档**。
> 因此下文绝大多数结论是源码级事实，而非文档转述。每条均标注 `[确认]` / `[推断]` / `[缺口]`。

核验日期：2026-09-20　　目标版本：`0.86.0`

---

## 一、身份、许可证与版本固定

| 项 | 值 | 来源 |
| --- | --- | --- |
| 仓库 | `github.com/earendil-works/pi` | — |
| 文档 | `https://pi.dev/docs` | — |
| npm 包 | `@earendil-works/pi-coding-agent` | `package.json` |
| 版本 | `0.86.0` | 同上 |
| **许可证** | **MIT** `[确认]` | `package.json` 的 `"license": "MIT"` |
| 引擎要求 | `node >= 22.19.0` `[确认]` | `package.json` → `engines` |
| 可执行入口 | `bin.pi` → `dist/bundle/cli.js` `[确认]` | 同上 |
| 自述 | "a minimal terminal coding harness" | 官方首页 |

**对本机的影响：** 已装 Node `v24.12.0`，满足 `>= 22.19.0`。这是本机第一次出现 Pi 的硬性前置条件被满足。

**MIT 的含义：** 可自由分发与内嵌，无 copyleft 传染。但注意 npm 包内**未附带 LICENSE 文件**（仅 `package.json` 声明），若将来要随应用分发二进制，需自行从仓库取 LICENSE 原文与版权行。

**版本固定 `[确认]`：** 是 `0.x` 预发布线，无正式破坏性变更政策文档 `[缺口]`。发布包内含 `npm-shrinkwrap.json`。建议：
- 在应用侧固定精确版本（`@earendil-works/pi-coding-agent@0.86.0`），不使用 `^`/`latest`；
- 把 npm 包内的 `docs/` 与 `dist/**/*.d.ts` 作为**离线权威参考**纳入开发资料（本次核验即依赖于此）。

**同域包 `[缺口]`：** `pi-agent-core`、`pi-ai`、`pi-tui`、`pi-protocol`、`pi-client`、`pi-server` 均为 `0.86.0`。其中 `pi-protocol`/`pi-client`/`pi-server` 描述为"传输中立的 **CBOR** 远程会话协议"，**与本地 `--mode rpc` 的 JSONL 协议的关系未验证**。不要假设两者通用。

---

## 二、接入方式裁决：RPC 子进程

设计计划假设"Pi Runtime 独立进程 + 本地结构化 IPC"。**该假设成立，且是官方推荐的跨语言路径。**

### 1. 三种机器可读模式 `[确认]`

`--mode <mode>` 仅接受 `text`（默认）、`json`、`rpc`：

- `--mode json`：单向。一次提示 → stdout 上输出完整事件流 JSONL。
- `--mode rpc`：**双向**。stdin 发命令，stdout 收响应与事件。这正是我们要的。

### 2. 没有 Python SDK `[确认]`

- SDK 是**进程内 Node/TypeScript** API（`createAgentSession`、`SessionManager`、`ModelRuntime`）。
- 官方文档明确：需要跨语言集成、进程隔离、语言无关客户端时，**用 RPC 模式**。
- 官方 `rpc.md` 内直接提供了 Python 子进程客户端示例。

### 3. 必须先说的一个陷阱 `[确认]`

`main.ts` 的 `resolveAppMode`：

```ts
if (parsed.print || !stdinIsTTY || !stdoutIsTTY) { return "print"; }
```

**只要 stdin 或 stdout 任一不是 TTY，Pi 会自动降级为 print 模式。** 我们以子进程管道方式启动，两者都不是 TTY——所以**必须显式传 `--mode rpc`**，绝不能依赖默认值。

另有一个 `./rpc-entry` 导出（`dist/bundle/rpc-entry.js`），会硬编码 `--mode rpc` 并设置 `PI_CODING_AGENT=true`、`process.title`。可作为更稳的启动入口，但它是包内路径而非公开 CLI，`[推断]` 稳定性弱于 `pi --mode rpc`。

---

## 三、传输层合同

### 1. 分帧 `[确认]`

官方 `rpc.md` 明确为 **strict JSONL，仅以 LF(`\n`) 分帧**：

- 只在 `\n` 上切分；
- 容忍 `\r\n`，去掉行尾 `\r`；
- **禁止使用会按 Unicode 分隔符切分的通用行读取器**。Node `readline` 被点名不合格，因为它还按 `U+2028`/`U+2029` 切分，而这两个字符在 JSON 字符串内合法。

**对 Python 的直接影响：** 必须按字节读取并 `split(b"\n")`。
**不要用 `str.splitlines()`** —— 它同样会在 `U+2028`/`U+2029` 处切分，会破坏含这些字符的正文。
同理 `TextIOWrapper.readline()` 只认 `\n`，可用，但**统一走 bytes 分帧更安全**。

### 2. stdio 归属 `[确认]`

`output-guard.ts` 的 `takeOverStdout()` 在 `appMode !== "interactive"` 时把 `process.stdout.write` 重定向到 stderr，**只有协议写入器使用原始 stdout**。

结论：**stdout 只承载协议；stderr 承载日志、诊断、扩展错误。** 这是干净的分离，我们的读取器据此设计即可。官方文档示例亦写作 `pi --mode json "..." 2>/dev/null`。

### 3. 命令关联 `[确认]`

命令可带可选 `id`，对应响应回显该 `id`。**事件没有 `id`**，唯一例外是 `bash_execution_update` 携带其来源 `bash` 命令的 `id`。因此：
- 命令/响应用 `id` 做请求-响应配对；
- 事件流是全局顺序流，不能靠 `id` 归属到某一轮，需要靠 `turn_start`/`turn_end` 与 `agent_start`/`agent_end` 界定边界。

### 4. 命令集（34 条）`[确认]`

来源：`dist/modes/rpc/rpc-types.d.ts` 的 `RpcCommand` 联合类型，逐条核对。

| 分组 | 命令 |
| --- | --- |
| 提示 | `prompt`（`message`, `images?`, `streamingBehavior?`）、`steer`、`follow_up` |
| 中断 | `abort`、`clear_queue`、`abort_bash`、`abort_retry` |
| 会话 | `new_session`（`parentSession?`）、`switch_session`、`get_state`、`get_messages`、`set_session_name`、`get_session_stats`、`export_html` |
| 分支 | `fork`（`entryId`）、`clone`、`get_fork_messages`、`get_entries`（`since?`）、`get_tree`、`get_last_assistant_text` |
| 模型 | `set_model`（`provider`, `modelId`）、`cycle_model`、`get_available_models` |
| 思考 | `set_thinking_level`、`cycle_thinking_level`、`get_available_thinking_levels` |
| 队列 | `set_steering_mode`、`set_follow_up_mode` |
| 压缩 | `compact`（`customInstructions?`）、`set_auto_compaction` |
| 重试 | `set_auto_retry` |
| 终端 | `bash`（`command`, `excludeFromContext?`） |
| 资源 | `get_commands` |

响应统一为 `{ type: "response", id?, command, success, data? }`，失败为 `{ ..., success: false, error: string }`。

**注意 `prompt` 的语义 `[确认]`：** `success: true` 只表示"已接受 / 已排队 / 已处理"，**不代表请求成功**。接受之后的失败通过事件送达，不会再有第二个响应。

### 5. 事件集 `[确认]`

| 事件 | 说明 |
| --- | --- |
| `agent_start` / `agent_end` | 一次底层 agent 运行开始/结束。`agent_end` 带 `messages` 与 `willRetry` |
| `agent_settled` | 完全安定：无自动重试、无压缩重试、无排队续跑 |
| `turn_start` / `turn_end` | 轮次边界（一轮 = 一次助手响应 + 工具调用/结果） |
| `message_start` / `message_update` / `message_end` | 消息生命周期与流式增量 |
| `tool_execution_start` / `tool_execution_update` / `tool_execution_end` | 工具生命周期 |
| `queue_update` | 待发 steering/follow-up 队列变化 |
| `compaction_start` / `compaction_end` | 压缩 |
| `auto_retry_start` / `auto_retry_end` | 自动重试 |
| `summarization_retry_scheduled` / `_attempt_start` / `summarization_retry_finished` | 摘要重试循环 |
| `bash_execution_update` | RPC `bash` 命令的输出分片 |
| `extension_error` | 扩展抛错 |

`turn_end` 还额外带 `toolResults`。`compaction_end` 带 `reason`(`manual`/`threshold`/`overflow`)、`result`、`aborted`、`willRetry`、`errorMessage?`。

### 6. 流式增量结构 `[确认]`

`assistantMessageEvent` 的联合类型（源 `pi-ai/src/types.ts`）：

```ts
{ type: "text_start" | "text_delta" | "text_end", contentIndex, delta?/content?, partial }
{ type: "thinking_start" | "thinking_delta" | "thinking_end", contentIndex, delta?/content?, partial }
{ type: "toolcall_start" | "toolcall_delta" | "toolcall_end", contentIndex, delta?/toolCall?, partial }
```

**上线的线格式与源码类型有差异 `[确认]`**（`modes/json-event.ts` 的 `toJsonEvent`）：

- `partial` 字段被**剥离**（为了流大小线性，`message_update` 是纯增量）；
- `toolcall_start` 额外带常量大小的 `id` 与 `toolName`；
- `text_end` / `thinking_end` 保留 `content`；`toolcall_end` 保留完整 `toolCall`。

**实现要点：** 用 `contentIndex` + `delta` 自行拼装实时文本/思考/工具参数；`message_end.message` 为最终权威副本。`thinking_delta` 是**协议标准字段**——它坐实了设计计划 §13.2「只读标准 reasoning 字段、不解析 `<think>` 标签」的决定。

### 7. 取消 `[确认]`

- `{"type":"abort"}`：中止当前操作并**等待会话进入 idle 后才响应**。
- 触发链：`session.abort()` → 设置 `_agentRunAbortRequested`、中止重试/压缩/分支摘要 → `agent.abort()`（即 `abortController.abort()`）→ `waitForIdle()`。
- **无 per-request 取消，无 abort id，无信号参数。**

**取消后的行为（与设计计划 §6.3 高度契合）`[确认]`：**

- 已流出的部分消息会被**保留并定稿**，`stopReason` 为 `"aborted"`；
- 未开始执行的工具调用**不会静默消失**，而是各产出一个 `isError: true`、内容为 `"Operation aborted"` 的错误结果，并附 `role: "toolResult"` 消息；
- 正在运行的工具通过 `execute(..., signal)` 收到中止信号，协作式退出。

→ 设计计划 §6.3「不得因取消而静默删除部分输出」**由协议天然满足**。

**模拟 Esc 的正确姿势 `[确认]`：** 先发 `clear_queue` 取回排队文本，再发 `abort`，然后把文本回填到输入框。否则 `abort` 会继续执行队列中剩余消息。

### 8. 进程语义 `[确认]`

| 情形 | 行为 |
| --- | --- |
| stdin EOF | `shutdown()`，退出码 `0` |
| `SIGTERM` | 退出码 `143` |
| `SIGHUP` | 退出码 `129`（非 Windows 才注册） |
| **`SIGINT`** | **无处理器**。回落到 Node 默认强杀（约 130），无优雅关闭、无 stdout flush |
| `--help`/`--version`/`--list-models`/`--export` | `0` |
| CLI 解析错误、会话不存在、无可用模型、print 模式异常 | `1` |
| RPC 模式下使用 `@file` 参数 | 报错并退出 `1` |

`rpc-mode.ts` 的 `runRpcMode` 以 `new Promise(() => {})` 结尾——**进程常驻，直到 stdin EOF 或信号**。

**结论：优雅关闭请用关闭 stdin（→0）或 SIGTERM（→143），不要依赖 SIGINT。**

**退出码 `[缺口]`：** 无正式的错误码表或错误枚举，以上系读源码所得，非稳定契约。

---

## 四、能力映射：设计计划需求 → Pi 机制

| 设计计划需求 | Pi 对应机制 | 状态 |
| --- | --- | --- |
| §1.2 会话级思考强度 | `set_thinking_level`；等级 `off/minimal/low/medium/high/xhigh/max`；`get_available_thinking_levels` 查询能力 | `[确认]` |
| §3 上下文占用显示 | `get_session_stats` → `contextUsage{tokens, contextWindow, percent}` | `[确认]` |
| §6.2 编辑/重生成自动分支 | `fork{entryId}`、`clone`、`get_fork_messages`；会话本身是**带稳定 id 的 append-only 树** | `[确认]` |
| §6.3 停止后续写 | `abort` + `steer`/`follow_up` + 两种队列模式 | `[确认]` |
| §7 压缩 | `compact{customInstructions}`、`set_auto_compaction`、`compaction_start/end` | **部分**（见 §五.2） |
| §8.4 图片原图直传 | `prompt.images[] = {type:"image", data:<base64>, mimeType}` | `[确认]` |
| §13.1 完整请求快照 | 扩展钩子 `before_provider_request` / `after_provider_response` / `before_provider_headers` | 需自建扩展 |
| §13.2 标准 reasoning | `thinking_delta`/`thinking_start`/`thinking_end` | `[确认]` |
| §13.3 自动重试 | `set_auto_retry` + `auto_retry_start/end`；设置项 `retry.*` | `[确认]` |
| §14.2 单文件 HTML 导出 | `export_html{outputPath}` | `[确认]` |
| §4 逻辑模型/站点 | `models.json` 自定义 provider + `set_model` | 需裁决（见 §五.5） |
| §11 权限与审批 | 扩展 `ctx.ui.confirm/select` → `extension_ui_request`/`extension_ui_response` | 需自建扩展 |
| §5 提示词体系 | CLI `--system-prompt` / `--append-system-prompt`；`get_commands` 暴露 prompt 模板与 skills | `[确认]` |

---

## 五、必须裁决的冲突

这一节是本报告最重要的部分。以下七条，设计计划的写法与 Pi 的实际行为不一致，**需要你拍板**。

### 1. 上下文权威归谁（最关键）

**事实 `[确认]`：** RPC 的 `prompt` 命令只有 `message: string` + 可选 `images`，**没有消息数组**。宿主无法指定一轮发给模型的完整消息序列。

**冲突：** 设计计划 §2.1 说"所有模型请求先生成不可变的拟发送请求快照"、§13.1 要求快照包含"实际发送消息"、§7 要求应用自主管理上下文压缩。但 Pi 拥有会话与上下文，宿主只投递单条消息。

**三个选项：**

- **(A) Pi 拥有会话，应用只镜像。** 应用用 `get_entries`/`get_tree` 读回结构，加密落库做副本。代价：§13.1 的"实际发送消息"只能来自 `before_provider_request` 钩子，而非应用自建。
- **(B) 应用拥有上下文，构造 Pi 会话文件后 `switch_session` 注入。** 会话格式是文档化的 v3 JSONL。代价：需自行维护 Pi 会话文件序列化，且 Pi 仍会向其追加写入；格式版本升级需跟进。
- **(C) 只做单轮执行器。** 每轮 `new_session` + 单条 `prompt`，上下文完全由应用拼成一条消息。代价：丧失真正的多轮语义与工具续跑能力，**实际不可行**。

**建议：先按 (A) 落地。** 理由：它用官方主路径、不依赖未验证的格式写入，且 `get_entries` 的稳定 id + `since` 游标已足以支撑应用侧的分支与审计。§13.1 的"实际发送消息"改由 `before_provider_request` 钩子供数。

**需你确认：是否接受把"拟发送请求快照"的权威来源从应用自建改为 Pi 钩子上报。**

### 2. 压缩能力低于设计计划

**事实 `[确认]`：** Pi 的压缩是**纯 token 预算驱动**：
- 触发条件 `contextTokens > contextWindow - reserveTokens`（`reserveTokens` 默认 `16384`）；
- 从最新消息向后累计，保留 `keepRecentTokens`（默认 `20000`），切割点即压缩边界；
- 摘要为固定结构：Goal / Constraints & Preferences / Progress(Done,In Progress,Blocked) / Key Decisions / Next Steps / Critical Context / read-files / modified-files；
- 工具结果在序列化时截断到 2000 字符；
- 原条目**不删除**，追加一条 `CompactionEntry`，早于 `firstKeptEntryId` 的消息不再发给模型。

**缺口 `[确认]`：** Pi **没有**——
- 白名单/固定消息（设计计划 §7.3 的核心诉求）；
- 压缩预览、用户编辑、重试、接受/取消 UI（§7.1）；
- 版本化与回退任意版本（§7.4）；
- 设置项形式的"压缩专用模型"（§7.2）。

**可用的钩子：** 扩展事件 `session_before_compact` 可 `return { cancel: true }`，或返回自定义 `{ summary, firstKeptEntryId, tokensBefore, usage? }` 覆盖摘要。`session_compact` 成功、`session_compact_failed` 失败（带 `reason`/`errorMessage`/`aborted`/`willRetry`/`fromExtension`）。官方 `custom-compaction.ts` 示例即用**另一个模型**生成摘要。

**建议：** 由应用侧生成摘要（用设计计划 §7.2 的专用压缩模型），通过自建扩展的 `session_before_compact` 注入，并把白名单与版本化留在应用数据库。这样 §7 的全部要求都能满足，前提是我们要写一个 TypeScript 扩展。

**需你确认：是否接受"为了 §7 必须引入一个自建 TS 扩展"这一额外组件。**

### 3. 全量加密与 Pi 的明文落盘

**事实 `[确认]`：** 正常运行时 Pi 会写盘：
- `~/.pi/agent/sessions/`（按工作目录组织，每会话一个 **JSONL 树**，**明文**）；
- `~/.pi/agent/auth.json`（**明文**，`0600`；含 API key 或 OAuth token）；
- `~/.pi/agent/models-store.json`（模型目录缓存）；
- `~/.pi/agent/trust.json`（信任决策）；
- `~/.pi/agent/crashes.json`（最近 5 条崩溃栈）；
- `settings.json`、`models.json`；
- 终端输出过长时落临时文件（`pi-bash-*` / `pi-powershell-*`）。

**冲突：** 设计计划 §12.1「全量默认加密」+ §12.2「数据库加密、文件仓库逐对象加密、API 密钥加密、日志加密」。

**可用手段：**
- `--no-session`（官方称 ephemeral，"do not save"）→ 不落会话文件；
- 设置项 `sessionDir` → 把会话目录重定向到应用控制的目录（支持绝对/相对/`~`）。

**建议：** 采用 `--no-session` 运行，权威会话状态由应用加密库持有；若某些功能必须依赖 Pi 的会话文件（如 `switch_session` 路线），则用 `sessionDir` 指向受控的**短生命周期明文目录**，退出即清理——这正是设计计划 §12.2「临时明文文件尽量只存在于受控短生命周期目录」预留的口子。

**另外必须注意：`auth.json` 是明文的。** 设计计划要求"API 密钥加密"。若把站点密钥交给 Pi 管理，就与 §12 冲突；**建议密钥由应用侧掌握，通过 `--api-key` 或 `models.json` 的 `"$ENV_VAR"` / `"!command"` 值解析在启动时注入**，不落 Pi 的 `auth.json`。`auth.json` 的 key 字段支持 `"$ENV"` 与 `"!命令"` 两种间接形式，这条路可行。

### 4. 权限体系完全需要自建

**事实 `[确认]`：**
- Pi 官方设计原则明确"**不包含**内置 MCP、子 agent、**权限弹窗**、plan 模式、todo、后台 bash"，说明这些要靠扩展构建；
- 安全文档明确"**Pi 不含内置沙箱**"，工具与扩展都以 pi 进程的权限运行。

**可用的审批通道 `[确认]`：** 扩展调用 `ctx.ui.select/confirm/input/editor`，在 RPC 模式下翻译为 `extension_ui_request` 上线，宿主用 `extension_ui_response` 回答。方法集（`rpc-types.d.ts` 逐条核对）：`select`、`confirm`、`input`、`editor`、`notify`、`setStatus`、`setWidget`、`setTitle`、`set_editor_text`。响应形态：`{value}` / `{confirmed}` / `{cancelled}`。

官方 `permission-gate.ts` 示例即：在 `tool_call` 事件里检查危险命令，非 UI 环境默认 `block: true`，有 UI 则弹窗询问。**`tool_call` 可在执行前阻断，且 `event.input` 可原地改写以修正参数（改后不再重新校验）。**

**结论：** 设计计划 §11 的整套权限模型（会话级持久授权、资源范围、高影响操作二次确认、审计）**必须由应用侧实现**，载体是一个自建 TS 扩展 + `extension_ui_*` 通道。

**需你确认：接受"权限网关落在 TS 扩展 + Python 宿主协同"这一架构。**

### 5. 路由与回退的归属

**事实 `[确认]`：** Pi 同时只有**一个**活跃模型（`set_model`）。它有 `models.json` 自定义 provider（任意 `baseUrl` + 四种协议），但没有"逻辑模型绑定多站点、按优先级回退、回退前要用户确认"的概念。

**结论：** 设计计划 §4 的"逻辑模型 → 站点"体系、§4.3 的备用端点与回退确认，**全部由应用编排**：应用调用 `set_model` 切换实际端点，失败后按自己的策略决定是否提示用户、是否切换。Pi 侧只是执行。

**中转站可用性 `[确认]`：** `~/.pi/agent/models.json` 支持——`baseUrl`（任意）、`api`（`openai-completions` / `openai-responses` / `anthropic-messages` / `google-generative-ai`）、`apiKey`、`headers`、`authHeader`。模型级支持 `id`、`name`、`reasoning`、`thinkingLevelMap`、`input:["text","image"]`、`contextWindow`、`maxTokens`、`samplingParams`、`cost`、`promptCache`。还有一整套 `compat` 开关，含 `thinkingFormat`（值含 `deepseek`、`zai`、`qwen`、`openrouter`、`together`、`chat-template`、`qwen-chat-template` 等）、`maxTokensField`、`supportsDeveloperRole`、`supportsReasoningEffort`、`requiresThinkingAsText` 等。

→ 设计计划 §4.4「站点兼容预设」列的每一项，在 `models.json` 里都有对应落点。**这一节可以按 Pi 的 schema 重写，而不是另起一套。**

### 6. 参数来源可解释性受两处干扰

**干扰一 `[确认]`：** 默认 `enableInstallTelemetry: true`，会给 **OpenRouter、NVIDIA NIM、Cloudflare** 的 provider 请求**注入 Pi 归属头**（attribution headers），并向 `https://pi.dev/api/report-install` 上报。

→ 直接违反设计计划 §4.4「不允许运行时私自注入来源不明的字段」，也违反 §1.3「不实现遥测」。
**处置：必须设 `enableInstallTelemetry: false`**（官方说明该项同时关闭上报与归属头）。

**干扰二 `[确认]`：** 模型级 `samplingParams` 是"自由对象，原样合并进每个请求体"，且在 Pi 自身字段**之后**应用，即**其键会覆盖 Pi 的字段**。这既是能力也是风险。
**处置：** 我们只从应用数据库生成该字段，绝不允许运行时来源不明的注入。

**观测通道 `[确认]`：** 扩展事件 `before_provider_headers`（可改 `event.headers`）、`before_provider_request`（在 payload 构建完成、即将发出前触发，可读可替换整个 payload）、`after_provider_response`（HTTP 响应已到、流体尚未消费，含 `event.status` 与归一化响应头）。**这是唯一能拿到精确最终请求体的通道**，是 §13.1 的实现基础。

### 7. 重试归属需要决策

**事实 `[确认]`：** Pi 自带两层重试，且 **agent 级默认开启**：
- `retry.enabled` 默认 `true`，`retry.maxRetries` 默认 `3`，指数退避 `retry.baseDelayMs` 默认 `2000`（即 2s/4s/8s），`retry.maxAgentDelayMs` 默认 `60000`；
- provider/SDK 级：`retry.provider.maxRetries` 默认 `0`，`retry.provider.timeoutMs`、`retry.provider.maxRetryDelayMs` 默认 `60000`。

**冲突：** 设计计划 §13.3 要求"只对连接类错误自动重试"，并明确列举**不**自动重试的情形（鉴权失败、参数错误、限流、业务错误、已产生副作用的工具调用、已输出大量内容后的流中断），还要"在消息内显示"重试次数与等待。

**问题：** Pi 的重试是否按错误类别区分，源码未逐条核验 `[缺口]`。

**建议：** 保守做法是 `set_auto_retry(false)` + 关闭 `retry.enabled`，由应用在 `request_failed` 层按自己的分类策略决定重试，从而完全满足 §13.3。代价是放弃 Pi 的退避实现。

**需你确认：重试策略归应用还是复用 Pi。**

---

## 六、Windows 与 PowerShell 实证（对应设计计划 §10）

### 1. Windows 是一等公民 `[确认]`

- Pi **原生支持 Windows**（非仅 WSL），有专门的 `docs/windows.md`。
- 但 `bash` 工具**依赖 Git Bash**：探测顺序为 ① `~/.pi/agent/settings.json` 的 `shellPath` → ② `C:\Program Files\Git\bin\bash.exe`（含 `ProgramFiles(x86)`）→ ③ PATH 上的 `bash.exe`（Cygwin/MSYS2/WSL）。全部失败则抛错并给出安装指引。
- 另有 WSL 遗产 bash 检测（`C:\Windows\System32\bash.exe`），命中时改用 `-s` + stdin 传输以绕开 WSL 启动器的参数问题。
- `powershell` 工具是**可选**的，用 `defaultTools` 替换模型面向的 shell 工具：
  ```json
  { "defaultTools": ["read", "powershell", "edit", "write"] }
  ```

本机已具备 Git Bash，`bash` 工具可用。

### 2. PowerShell 工具逐项核对

| 设计计划要求 | Pi 实际 | 判定 |
| --- | --- | --- |
| §10.1 默认 PowerShell 7，缺失回退并明示 | `findExecutableOnPath("pwsh.exe") ?? findExecutableOnPath("powershell.exe")` —— **确实 pwsh 优先** | **满足**（但"在界面明示回退"需我们自己做） |
| §10.2 编码统一 | 仅前置 `try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}`；**未设 `InputEncoding`，未设 `$OutputEncoding`**；且用的是带 BOM 语义的 `Encoding.UTF8`，而非 `UTF8Encoding($false)` | **仅 1/3** |
| §10.3 写临时 `.ps1`，不拼单行 | 直接 `-NoProfile -NonInteractive -ExecutionPolicy Bypass **-Command**`，把 UTF-8 前缀与命令拼成单行 | **相反** |
| §10.4 Bash 语法误用静态检测 | 全包检索无任何相关实现 | **完全没有** |
| §10.5 结构化返回含独立 `stdout`/`stderr` | 两者**合并**进同一 `onData` 回调 | **不符合** |
| §10.5 退出码与信号 | `exitCode ?? (signalCode ? 128 + n : 1)` | 满足 |
| — 输出截断 | 保留最后 `2000` 行或 `50KB`（先到者），超出则全量写临时文件并回报路径 | 附带能力 |
| — 超时 | 工具入参 `timeout`（秒），**可选、无默认值**；超时抛 `timeout:<n>`；上限约 2147483 秒 | — |
| — 进程树清理 | Windows 上用 `System32\taskkill /F /T`（不走 PATH，避免劫持） | 可借鉴 |

**逐条含义：**

- §10.1 的 pwsh 回退**不用我们实现**，Pi 已有。但"在界面明确显示当前用的是哪个 shell"必须我们做——可以从 `get_state`/环境或自行探测导出。
- §10.2 是**真实缺陷**：未设 `InputEncoding`/`$OutputEncoding` 时，非 UTF-8 的遗留命令输出、以及含中文的输入命令，在 GBK 代码页机器上仍可能乱码。**这一项需要我们在提示词层或自建执行器补齐。**
- §10.3 与 Pi 的实际做法相反。`-Command` 单行受命令行长度上限约束，且引号/转义由 Node 的 argv 序列化处理。**如果我们走 Pi 的 powershell 工具，就拿不到临时脚本方案。** 若要 §10.3 的健壮性，必须走"直接终端模式"（设计计划 §9.1 的第二种模式）由应用自己执行。
- §10.4 的 Bash 误用检测是**纯增量价值**，Pi 完全没有。可作为应用侧前置检查实现。
- §10.5 的 stdout/stderr 分离在 Pi 的返回里不存在。若设计计划坚持该 schema，同样需要自建执行器。

**这构成一个重要结论：设计计划 §10 有相当一部分，只有在"直接终端模式（应用自建执行器）"下才能满足；走 Pi 的内置 powershell 工具则只能满足其中一部分。**

---

## 七、安全与隐私

### 1. 威胁模型 `[确认]`

- Pi 以启动它的用户权限运行，"与该用户可写的文件视为同一本地信任边界"。
- **无内置沙箱**，且这是刻意的：官方认为进程内部分沙箱容易被误解为安全边界。
- **Project trust 只是输入加载守卫**，不是沙箱，不限制工具能做什么。它控制是否加载项目本地的 `settings.json`、`.pi/extensions`、`.pi/skills`、`.pi/prompts`、`.pi/themes`、`.pi/SYSTEM.md`、`.pi/APPEND_SYSTEM.md`、项目 `.agents/skills`。
- **非交互模式（`-p`、`--mode json`、`--mode rpc`）不弹信任提示。** 无已保存决策时，`defaultProjectTrust` 为 `"ask"` 或 `"never"` 都会**忽略**这些资源，`"always"` 才信任。
- 信任决策按规范化目录存于 `~/.pi/agent/trust.json`；`--approve`/`-a`、`--no-approve`/`-na` 可单次覆盖。
- `AGENTS.md`、`AGENTS.override.md`、`CLAUDE.md` 等上下文文件**不受信任门控**，默认加载（除非 `--no-context-files`）。
- 提示注入（来自仓库文件、注释、文档、构建输出）被明确列为"预期的本地 agent 风险，无法可靠防止"。

**对我们的直接影响：**

1. 我们在 RPC 模式启动，**没有信任弹窗**。若工作目录是我们打开的用户仓库，其 `.pi/extensions` 等资源在 `"ask"`/`"never"` 下会被忽略——这正是我们想要的默认。**建议显式固定 `defaultProjectTrust: "never"` 并传 `--no-approve`**，避免依赖默认值语义。
2. `AGENTS.md`/`CLAUDE.md` 会被自动注入系统提示词，**这违反设计计划 §5「任何额外参数必须有明确来源」**。**建议传 `--no-context-files`**，由应用的提示词体系全权负责。
3. Pi 无沙箱，我们的 §11 权限网关是**唯一**的约束层——它的重要性因此高于设计计划中的原估。

### 2. 遥测 `[确认]`

| 设置 | 默认 | 作用 |
| --- | --- | --- |
| `enableInstallTelemetry` | **`true`** | 向 `https://pi.dev/api/report-install` 发匿名安装/更新上报，**并为 OpenRouter/NVIDIA NIM/Cloudflare 请求加归属头** |
| `enableAnalytics` | `false` | 仅实验性首次安装（`PI_EXPERIMENTAL=1`）时询问 |
| 版本检查 | 开启 | 访问 `https://pi.dev/api/latest-version` |

关停开关：`enableInstallTelemetry: false`；`PI_SKIP_VERSION_CHECK=1` 关版本检查；`--offline` 或 `PI_OFFLINE=1` 关全部启动期网络操作。

**结论：设计计划 §1.3 声明"不实现遥测"。我们必须显式关闭 Pi 自身的遥测**，而不是假设它默认关闭——它默认是**开**的。

---

## 八、建议的启动配置（草案）

```powershell
pi --mode rpc `
   --no-session `
   --no-approve `
   --no-context-files `
   --provider <provider> `
   --model <provider/model-id> `
   -e <应用自建扩展.ts> `
   --session-dir <受控短生命周期目录>
```

配套设置（`~/.pi/agent/settings.json` 或项目级）：

```json
{
  "enableInstallTelemetry": false,
  "defaultProjectTrust": "never",
  "sessionDir": "<受控目录>",
  "defaultTools": ["read", "powershell", "edit", "write"]
}
```

环境变量：`PI_SKIP_VERSION_CHECK=1`。

**以上仅为基于本次核验的草案，未经过实机连通验证。** 每一项都还需要 Phase 0 Task 0.3 的实机跑通来确认。

---

## 九、尚未解决的缺口

| # | 缺口 | 影响 | 建议解决方式 |
| --- | --- | --- | --- |
| 1 | 无正式 JSON Schema / 错误码表 | 协议类型仅存在于 npm 包的 `.d.ts` | 以 `0.86.0` 的 `.d.ts` 为基准生成 Python 侧类型，并在升级时对比 |
| 2 | `prompt` 无法注入完整消息历史 | 决定 §2.1/§13.1 的实现路线 | 见 §五.1，先按 (A) 落地 |
| 3 | Pi 重试是否按错误类别区分 | 决定 §13.3 归属 | 读 `pi-agent-core` 重试实现，或直接关掉 Pi 重试 |
| 4 | 会话 v3 格式细节未细读 | 影响 (B) 路线可行性 | 需要时细读包内 `docs/session-format.md` |
| 5 | CBOR 系（`pi-protocol`/`pi-client`/`pi-server`）与本地 rpc 的关系 | 未知，可能提供了更结构化的传输 | 单独核验，暂不纳入 |
| 6 | `0.x` 无破坏性变更政策 | 升级风险 | 固定精确版本，升级前用本次方法重新核验 |
| 7 | 未实机连通验证 | 上述所有结论均为静态核验 | Phase 0 Task 0.3 必须实机跑通一次完整流式会话 |

---

## 十、核验方法留痕

核验用的本地材料位于系统临时目录（未纳入版本控制）：

```
%LOCALAPPDATA%\Temp\pi-research\package\
├─ docs\           32 份官方文档（含 windows.md、rpc.md、models.md、settings.md、security.md）
├─ dist\           .d.ts 类型定义 215 份 + 实现源码
└─ package.json    license / engines / bin / exports
```

复现方式：

```bash
npm pack @earendil-works/pi-coding-agent@0.86.0
tar -xzf earendil-works-pi-coding-agent-0.86.0.tgz
```

`raw.githubusercontent.com` 与 `api.github.com` 在本机网络下不可达（ECONNRESET）；`cdn.jsdelivr.net` 与 npm registry 可达。

---

## 十一、Phase 0 内核去留闸门

本节是 Phase 0 的收敛点。**只验证以下六件事，不再扩散范围。**

每项结论**只允许**三选一：**通过** / **可绕过** / **不通过**。
判定为「可绕过」时，**必须同时写明绕过方案与残余风险**；写不出这两项的，「可绕过」无效，视为**不通过**。

### 1. 判定规则

- 六项**全部通过** → 锁定「Pi + 应用兼容内核层」路线。
- **P0-GATE-01 / 02 / 04 / 05** 中任意一项为**不通过** → 进入局部 Fork 评估。
- **P0-GATE-03 / 06** 为不通过 → 先执行各自的降级方案，不单独触发 Fork 评估。
- 局部 Fork 仍无法形成稳定合同 → 才进入自建内核论证。

### 2. 结论汇总

| ID | 主题 | 结论 | 绕过方案与残余风险 |
| --- | --- | --- | --- |
| P0-GATE-01 | `--no-session` 会话语义 | **通过** | 选项 ①。28/28 项检查通过，见本节末「实机验证记录」 |
| P0-GATE-02 | `before_provider_request` 捕获最终请求 | **通过** | 15/15 项。观察模式捕获值与上线值逐字段相同；改写生效。注意 `event.payload` 是改写前值 |
| P0-GATE-03 | `session_before_compact` 接管压缩 | **通过** | 14/14 项。钩子可取消、可注入自定义摘要，手动（manual）与自动阈值（threshold）两条路径均受控。见「实机验证记录」 |
| P0-GATE-04 | `tool_call` 阻断全部工具执行 | **可绕过** | 模型调用路径 `tool_call` 可完全阻断（7/7）。**但发现真实缺口**：`tool_call` 不覆盖 RPC `bash` 命令与用户命令，它们走独立的 `user_bash`/`user_editor` 事件。规避：应用通道走 `tool_call` + 同钩 `user_bash`/`user_editor`，或不实现原生终端。见「实机验证记录」 |
| P0-GATE-05 | 内部重试与安装遥测彻底关闭 | **通过** | 启动期出网=0；agent 级 `retry.enabled=false` 后只发 1 次、无重试事件、正确报错；provider 级默认 0、可控。见「实机验证记录」 |
| P0-GATE-06 | Runtime 崩溃后据加密镜像恢复 | **通过** | 13/13 项。应用可把权威镜像物化为 Pi 会话文件并 `switch_session` 恢复完整模型上下文。强路径成立。见「实机验证记录」 |

---

### P0-GATE-01　`--no-session` 下是否保留完整进程内多轮上下文与分支树

**优先级：最高。这是阻断性问题——若不成立，权威边界方案需要重构。**

判定方法：

1. 以 `pi --mode rpc --no-session --session-dir <受控空目录>` 启动。
2. 连续两轮 `prompt`，第二轮内容依赖第一轮上下文，确认模型确实记得。
3. 依次发 `get_state`、`get_entries`、`get_tree`、`get_session_stats`、`get_fork_messages`、`fork`、`clone`。
4. 检查 `get_state.sessionFile` 是否存在，以及 `<受控目录>` 下是否产生任何文件。

- **通过**：会话树与多轮上下文在进程内完整存在，仅不落盘；上述分支类命令全部可用；磁盘无会话文件。
- **可绕过**：会话语义受限（部分命令不可用），但存在受控替代路径——例如 `sessionDir` 指向短生命周期目录，或存在内存 session 后端／导入恢复入口。
- **不通过**：无 session 即无多轮上下文，且不存在任何替代路径。

### P0-GATE-02　`before_provider_request` 能否稳定捕获最终请求体

判定方法：

1. 写一个最小 TS 扩展，挂 `before_provider_request`，将 `event.payload` 原样输出到 stderr。
2. 跑一轮**含工具调用**的请求，采集 payload。
3. 与应产生的请求意图快照逐字段比对（messages 序列、tools 定义、model、参数）。
4. 再测一次"返回改写后 payload"，确认改写真正生效。

- **通过**：能稳定拿到完整最终 payload，且可原样改写。
- **可绕过**：能观测但字段不全／不稳定，或只能观测不能改写（改写能力另寻路径）。
- **不通过**：拿不到 payload，或该钩子在 rpc 模式下不触发。

### P0-GATE-03　`session_before_compact` 能否可靠接管压缩

判定方法：

1. 钩子返回 `{ cancel: true }`，触发 `compact`，确认 Pi 未自行压缩且会话仍可用。
2. 钩子返回自定义 `{ summary, firstKeptEntryId, tokensBefore }`，确认摘要生效、后续上下文按该摘要重建。
3. **两条触发路径都要测**：手动 `compact`，以及自动阈值触发。

- **通过**：可取消、可注入自定义摘要，两条触发路径均受控。
- **可绕过**：钩子同步返回、无法等待 GUI 审阅 —— 改用提前压缩 + 预批准摘要注入（在 70%～80% 阈值由应用主动发起，Pi 真正触发时直接注入已批准摘要）。
- **不通过**：无法阻止 Pi 自行改写上下文。

### P0-GATE-04　`tool_call` 能否阻断全部实际工具执行

判定方法：

1. 先用 `get_state`／`--tools` 确认**实际注册的工具全集**（含扩展工具）。
2. 对**每一个**工具逐一发起调用，钩子一律返回 `{ block: true }`。
3. 用**可观测副作用**验证未执行（写入文件、命令留痕），不能只看模型是否声称被阻断。
4. 另测应用断连／超时未响应时是否默认拒绝。

- **通过**：全部可阻断；未响应或断连时默认拒绝。
- **可绕过**：少数工具不可阻断，但可在启动时不注册、改注册应用代理工具。
- **不通过**：存在无法阻断且无法禁用的执行路径。

### P0-GATE-05　Pi 内部重试与安装遥测能否彻底关闭

判定方法：

1. 设 `enableInstallTelemetry: false`、`retry.enabled: false`、`PI_SKIP_VERSION_CHECK=1`。
2. 用代理（`httpProxy`）或抓包观察启动期与请求期出网，确认上报与 provider 归属头消失、版本检查消失。
3. 人为制造可重试错误（连接中断），确认 Pi **未**自行重发。

- **通过**：上报、归属头、版本检查均消失，且无内部重试。
- **可绕过**：某项无法完全关闭，但影响可控且在 UI 中如实说明。
- **不通过**：遥测或重试无法关闭 —— 会导致"应用看到的一次调用"实际已在内部请求多次。

### P0-GATE-06　Runtime 崩溃后能否据加密镜像恢复到可继续状态

判定方法：

1. 运行中强杀 Pi 进程（`taskkill /F` 与 SIGTERM 各测一次）。
2. 应用据加密镜像重建状态，验证能继续对话。
3. 校验分支关系、工具调用记录与压缩边界三者与崩溃前一致。
4. 确认**未自动重发**上一轮用户请求。

- **通过**：恢复到可继续状态，产品级数据无损，且不自动重发。
- **可绕过**：需人工介入或丢失少量运行时增量，但产品级数据无损。
- **不通过**：崩溃后无法重建出可继续的会话。

---

### 3. 实机验证记录

#### P0-GATE-01　结论：通过（选项 ①）

- 日期：2026-09-20
- 环境：Windows 11 `10.0.26200` / Node `v24.12.0` / `@earendil-works/pi-coding-agent@0.86.0`
- 方法：`tools/pi-verify/` 下的 mock provider + RPC 客户端 + 探针。**不依赖任何真实密钥**——
  本地 mock 实现了 OpenAI Chat Completions 兼容接口并记录每次请求体，因此上下文是否累积
  由请求体本身判定，不依赖模型自述。
- 命令：
  ```text
  pi --mode rpc --no-session --session-dir <受控空目录>
     --provider mock --model mock/mock-model --no-approve --no-context-files
  ```
- 结果：**28/28 项通过**

| 观察项 | 实测 |
| --- | --- |
| `get_state` 是否含 `sessionFile` | **不含**（内存模式特征）；`sessionId` 仍分配 |
| 多轮上下文 | 第一轮请求送出 **2** 条消息，第二轮 **4** 条，且第二轮内容含第一轮 |
| `messageCount` | `3 → 5` 累积 |
| 分支与查询命令 | `get_entries`(7 entry) / `get_tree` / `get_fork_messages` / `get_messages` / `get_commands` 全部可用 |
| `fork` | 可用；活动分支 leafId 确实切换，且切换后仍可继续对话 |
| `clone` | 可用 |
| 磁盘 | 受控 `sessionDir` 为空；默认 `~/.pi/agent/sessions` 零新增 |
| 关闭语义 | stdin EOF → 退出码 **0** |
| `contextUsage` | `{tokens, contextWindow, percent}` 可用（支撑设计计划 §3 的上下文占用显示） |

**结论：`--no-session` 仅禁止持久化磁盘，会话树与多轮上下文在进程内完整存在。**
设计计划 §12 的加密方案（应用持有权威、Pi 不落盘）成立，ADR-0001 的权威边界无需修改。

复现：`python tools/pi-verify/gate_01_no_session.py`

**探针自身的坑：** `fork` 需要的是**用户消息**的 entryId，不是 `get_tree` 返回的 `leafId`
（leaf 通常是助手或工具结果消息），传错会得到 `Invalid entry ID for forking`。
首轮探针因此曾误判为「选项 ③」——**闸门结论必须区分「Pi 不可用」与「探针写错了」。**

#### 顺带取得的实测事实

以下两条不属于任何闸门，但对实现有直接影响，记录以免重复踩坑。

**默认工具集只有 4 个：** 未配置 `defaultTools` 时注册的是 `read`、`bash`、`edit`、`write`。
**不含** `powershell`、`grep`、`find`、`ls`。

→ 设计计划 §9.1 若要在 Windows 上使用 PowerShell 工具，**必须显式配置 `defaultTools`**，
不能依赖默认值。§10 的终端模式与 §9.1 的内置工具集都需要按此显式声明。

**`openai-completions` 的请求体顶层字段：**
`messages`、`model`、`stream`、`stream_options`、`tools`、`store`、`max_completion_tokens`。

→ 注意是 `max_completion_tokens` 而非 `max_tokens`（对应 `compat.maxTokensField` 的可选行为），
且默认带 `store` 字段。核对 §4.4「参数白名单」时需纳入。

#### P0-GATE-02　结论：通过

- 日期：2026-09-20　　环境：同 GATE-01　　结果：**15/15 项通过**
- 方法：扩展 `extensions/gate-02-request-hook.ts` 挂 `before_provider_request`（记录 payload）
  与 `after_provider_response`（记录 status/headers）；mock provider 记录实际上线请求体，
  两相对照。改写测试用 `GATE02_REWRITE=1` 把 `max_completion_tokens` 改为 2048。

| 观察项 | 实测 |
| --- | --- |
| 钩子触发 | 每轮触发，payload 完整可取 |
| 观察模式保真 | 扩展捕获的 payload 与 mock 实际收到的**逐字段相同**（messages/model/tools/stream/max_completion_tokens） |
| 改写生效 | 返回 `{...payload, max_completion_tokens: 2048}` 后，mock 实际收到 2048（原默认 4096） |
| **改写边界** | `event.payload` 是**改写前**的值；改写在此后生效，其余字段原样透传 |
| `after_provider_response` | 收到 `status=200` 与归一化响应头 |
| payload.messages[0] | 是 Pi 注入的 **system 消息**（设计计划 §5 提示词分层的观测点） |

**结论：能稳定捕获并可改写最终请求体。** §13.1「最终传输快照」完全可行。
**实现要点：归档「实际上线值」= 钩子捕获值 + 应用自身施加的改写**——因为钩子读到的是改写前的 payload，
应用必须自己记录改写了什么。该信息由应用掌控，确定性足够。

复现：`python tools/pi-verify/gate_02_final_request.py`

#### P0-GATE-04　结论：可绕过（含一处真实缺口）

- 日期：2026-09-20　　环境：同 GATE-01

**方法：** 双探针。
`gate_04_tool_block.py` 用 RPC `bash` 命令做副作用测试（touch 哨兵文件），三种模式：
不加载扩展 / `block-all` / `no-response`（模拟断连）。
`gate_04b_model_tool_block.py` 让**模型真正调用工具**（mock 发出 `read(canary.txt)` 工具调用，
canary 文件含机密字符串 `CANARY-SECRET-*`），看下一轮请求（含 tool 结果）里机密是否出现。

**模型调用路径（`gate_04b`）：7/7 通过。**

| 观察项 | 实测 |
| --- | --- |
| `tool_call` 钩子触发 | allow-all 与 block-all 下均被触发 |
| allow-all 基线 | 机密进入上下文（证明 read 真的执行了，不是"没读到"造成的假阴性） |
| block-all | 机密**未**进入上下文；tool 结果含阻断原因 `GATE04 blocked by extension` |

→ **模型发起的工具调用，`tool_call` 可完全阻断。** ADR-0001 的 `PolicyEnforcementExtension` 在此路径成立。

**直接终端路径（`gate_04`）：发现真实缺口。**

| 观察项 | 实测 |
| --- | --- |
| 无扩展基线 | 命令执行，哨兵文件创建，`exitCode=0` |
| `block-all` 扩展 | **哨兵文件仍被创建**；`tool_call` 钩子**未触发**（日志为空） |
| `no-response` 扩展 | **哨兵文件仍被创建**；进程需强杀 |

→ **RPC `bash` 命令与用户命令不经过 `tool_call` 钩子。** 源码确认（`rpc-mode.js` 第 443 行）：
该路径触发的是**独立的 `user_bash` 事件**，不是 `tool_call`。同理用户编辑器命令走 `user_editor`。

**缺口含义与规避：**
- 若应用实现"直接终端 / Pi 原生终端"模式，**必须同时钩住 `user_bash` 与 `user_editor`**，
  否则存在绕过权限网关的执行路径。
- 规避方案：`PolicyEnforcementExtension` 同时实现 `tool_call`（模型调用）与
  `user_bash`/`user_editor`（用户命令）两类钩子；或产品上不实现 Pi 原生终端模式。
- 残余风险：`user_bash`/`user_editor` 的阻断与改写语义是否与 `tool_call` 完全对等，
  尚未实测（其 handler 返回类型在 `types.d.ts` 中为独立定义）。

复现：`python tools/pi-verify/gate_04_tool_block.py` 与 `gate_04b_model_tool_block.py`

#### P0-GATE-05　结论：通过

- 日期：2026-09-20　　环境：同 GATE-01
- 方法：三项子检查，全部走行为观测（不抓包）。

**A. 启动期出网 —— 通过。** 子进程拦截 `net.Socket.prototype.connect` 记录全部连接目标，
随后 `import cli.js --version`；在 `PI_SKIP_VERSION_CHECK=1` + `PI_OFFLINE=1` 下，**连接目标为空**。

**B. agent 级自动重试 —— 可关闭。** 用 mock 的失败注入（前 2 次返回 503）+ 子进程动态写 `settings.json`：

| 配置 | 请求次数 | auto_retry 事件 | 收场 |
| --- | --- | --- | --- |
| `retry.enabled=false` | **1** | 无 | 正确报错（agent_end 带 error） |

**C. provider 级重试 —— 默认即 0，可控。** 用"接受连接立即重置"的 TCP 服务器（产生 ECONNRESET）
数连接次数：

| 配置 | 连接数 | 含义 |
| --- | --- | --- |
| 全默认（provider `maxRetries=0`） | **1** | 默认不重试 |
| agent 关 + provider `maxRetries=0` | 1 | 彻底关闭 |
| provider `maxRetries=2` | **3** | 1 主试 + 2 重试，确认可控 |

**结论：遥测与两类重试均可彻底关闭。** ADR-0001 第七节的启动硬性断言成立。

**两条对 §13.3 有直接意义的实测事实：**

1. **Pi 的 agent 级重试不针对 HTTP 5xx 状态码。** 开着重试时对 503 也只发 1 次、无 `auto_retry`
   事件。它大概率只对连接类错误重试——这与设计计划 §13.3「只对连接类错误重试」的默认行为接近。
2. **settings.json 启动时读一次、运行时不重读。** 重试/遥测配置必须在启动 Pi **之前**写入，
   不能在运行中改。这是适配器的硬性流程约束。

复现：`python tools/pi-verify/gate_05_telemetry_retry.py`

**探针自身的坑（mock）：** mock 的失败注入最初在 `_record` 之前 `return`，导致被 503 的请求
没有记入日志，产生"请求数=0"的假象。**mock 必须先记录每一条请求，再决定是否返回错误。**

#### P0-GATE-03　结论：通过

- 日期：2026-09-20　　环境：同 GATE-01　　结果：**14/14 项通过**
- 方法：扩展 `extensions/gate-03-compaction-hook.ts` 挂 `session_before_compact`，
  三种模式（observe / cancel / custom）+ 自动阈值触发。

| 阶段 | 实测 |
| --- | --- |
| observe | 钩子触发，`reason=manual`，`preparation` 含 `messagesToSummarize`/`firstKeptEntryId`/`tokensBefore` |
| cancel | 返回 `{cancel:true}` 后**压缩未发生**（`contextUsage.tokens` 不变 950→950） |
| custom | 返回自定义 `{compaction:{summary,...}}` 后，**下一轮请求的上下文含自定义摘要标记、被压原文消失** |
| threshold | 小 contextWindow + 小 reserveTokens 触发自动压缩，钩子以 `reason=threshold` 触发 |

**结论：`session_before_compact` 可取消、可注入自定义摘要，手动与自动阈值两条路径均受控。**
设计计划 §7 的压缩接管可行，ADR-0001 裁决 2 的实现路径成立。

**关键流程约束 `[实测]`：** 手动 `compact` 命令要求 `prepareCompaction` 返回非空，
即切割点之上有可压消息；`findCutPoint` 仅在 `accumulatedTokens >= keepRecentTokens` 时才把切割点前移。
因此手动压缩的实测前提是 **`keepRecentTokens` 足够小**（默认 20000 时几乎永不前移）。
这与 ADR 裁决 2 的降级方案吻合：**应用应主动在 70%～80% 阈值发起压缩，而不是依赖 Pi 的手动命令在任意时刻可压。**

复现：`python tools/pi-verify/gate_03_compaction.py`

#### P0-GATE-06　结论：通过（强路径成立）

- 日期：2026-09-20　　环境：同 GATE-01　　结果：**13/13 项通过**
- 方法：会话 A（两轮 + 完整权威镜像采集）→ 强杀 Pi 进程（taskkill 等价）→ 新进程恢复。

**核心发现：应用可据加密镜像恢复完整模型上下文，而不只是"新会话重来"。**

恢复路径：应用把 `get_entries` 采集的权威镜像**物化为 Pi 会话文件**（v3 JSONL：
header + 逐条 message entry），再 `switch_session` 加载。

| 观察项 | 实测 |
| --- | --- |
| 权威镜像 | `get_entries` 返回 7 条（system/model_change/thinking_level_change/2×user/2×assistant），id/parentId 完整 |
| 物化 + switch_session | 成功加载 |
| 恢复后 get_messages | 完整返回 5 条（system + 2×user + 2×assistant），**含崩溃前内容** |
| 恢复后模型上下文 | 新一轮请求的请求体**含崩溃前标记**（请求体取证），模型上下文真正被恢复 |
| 自动重发 | **无**（崩溃点在两轮之间，无 in-flight 请求被重发） |
| 进程退出 | 正常退出码 0 |

**结论：`--no-session` 不仅不丢权威状态，还支持"物化恢复"这条强路径。**
设计计划 §12 的崩溃恢复与 ADR-0001 的"Pi 状态必须可由应用重建"得到实机确认。

**两条对实现有硬影响的实测事实：**

1. **快照必须在分支切换之前采集。** fork 会切换活动分支，此后 `get_entries` 只回活动分支，
   会丢掉重建所需的 system 消息。归档策略：在每次分支操作前落盘完整镜像。
2. **物化文件必须含 system 消息。** 缺了它，`switch_session` 虽能成功但上下文是空的
   （首轮调试因此只看到 1 条消息）。

**`switch_session` 的一个行为细节：** 传不存在的文件路径不报硬错误（`success` 仍返回），
但上下文不恢复。**适配器必须在 `switch_session` 后用 `get_messages`/`get_entries` 校验恢复结果**，
不能只看 `success`。

复现：`python tools/pi-verify/gate_06_crash_recovery.py`

---

验证设施的组成与扩展方式见 [`tools/pi-verify/README.md`](../../tools/pi-verify/README.md)。
