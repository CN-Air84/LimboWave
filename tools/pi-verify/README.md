# Pi 集成验证设施（Phase 0）

用于在设计计划 Phase 0 阶段，对 Pi 做**不依赖真实密钥**的实机验证。
产出用于填写 [合同文档 §十一「内核去留闸门」](../../docs/architecture/pi-runtime-contract.md#十一phase-0-内核去留闸门)
的 `P0-GATE-01` ～ `P0-GATE-06`。

## 为什么需要 mock provider

Pi 需要真实模型端点才能跑通多轮。若依赖真实 API，验证会受制于密钥、费用、网络与模型不确定性，
而且**上下文到底有没有累积，只能听模型自己说**——这不是证据。

本设施用本地 mock 实现 OpenAI Chat Completions 兼容接口，并**记录每一次请求的完整请求体**。
于是"第二轮有没有带上第一轮"变成一条可以直接读的日志事实。

## 组成

| 文件 | 作用 |
| --- | --- |
| `mock_provider.py` | 本地 OpenAI 兼容 mock provider：记录请求体到 JSONL，支持工具调用注入（PUT `/control/toolcall`）与失败注入（PUT `/control/fail`） |
| `rpc_client.py` | Pi RPC 模式的最小 Python 客户端；也可单独运行做连通自检 |
| `gate_01_no_session.py` | P0-GATE-01：`--no-session` 会话语义与分支命令 |
| `gate_02_final_request.py` | P0-GATE-02：`before_provider_request` 捕获/改写最终请求 |
| `gate_03_compaction.py` | P0-GATE-03：`session_before_compact` 接管压缩（手动 + 阈值） |
| `gate_04_tool_block.py` | P0-GATE-04：RPC `bash` 命令路径的副作用阻断测试 |
| `gate_04b_model_tool_block.py` | P0-GATE-04：模型发起的工具调用阻断测试 |
| `gate_05_telemetry_retry.py` | P0-GATE-05：遥测与内部重试关闭 |
| `gate_06_crash_recovery.py` | P0-GATE-06：崩溃后据镜像物化恢复 |
| `extensions/` | 各闸门探针用的 TypeScript 扩展（经 `-e` 加载） |

## 闸门结论

六项闸门已全部实机验证，结论记录在
[`docs/architecture/pi-runtime-contract.md` 第十一节](../../docs/architecture/pi-runtime-contract.md#十一phase-0-内核去留闸门)：

| 闸门 | 结论 |
| --- | --- |
| P0-GATE-01 | 通过 |
| P0-GATE-02 | 通过 |
| P0-GATE-03 | 通过 |
| P0-GATE-04 | 可绕过（模型调用可全拦；`user_bash` 是独立通道需另钩） |
| P0-GATE-05 | 通过 |
| P0-GATE-06 | 通过 |

## 前置条件

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.86.0
```

并配置 `~/.pi/agent/models.json` 指向 mock（`<port>` 与 `mock_provider.py --port` 一致）：

```json
{
  "providers": {
    "mock": {
      "baseUrl": "http://127.0.0.1:8787/v1",
      "api": "openai-completions",
      "apiKey": "mock-key",
      "models": [
        { "id": "mock-model", "name": "Mock Model", "contextWindow": 128000, "maxTokens": 4096, "input": ["text"] }
      ]
    }
  }
}
```

建议同时配置 `~/.pi/agent/settings.json`，与 ADR-0001 第七节的启动基线一致：

```json
{ "enableInstallTelemetry": false, "defaultProjectTrust": "never" }
```

## 运行

```bash
.venv/Scripts/python.exe tools/pi-verify/rpc_client.py        # 连通自检
.venv/Scripts/python.exe tools/pi-verify/gate_01_no_session.py  # GATE-01 探针（自动起停 mock）
```

## 写新探针时的注意事项

这些是实际踩过的坑，不是理论提醒。

1. **分帧必须按字节切 `b"\n"`。** 不要用 `str.splitlines()` —— 它会在 `U+2028`/`U+2029`
   处切分，而这两个字符在 JSON 字符串内合法，会造成误切。合同文档 §三 有完整说明。
2. **不要调用 npm 的 `.cmd` 垫片。** Windows 的 `CreateProcess` 不用 PATHEXT 补全，
   `subprocess.run(["npm", ...])` 会直接 `FileNotFoundError`。同理不走 `pi.cmd`，
   而是由 `resolve_pi_command()` 定位 `node` + `cli.js`。
3. **`fork` 需要用户消息的 entryId**，不是 `get_tree` 的 `leafId`。
   传错会得到 `Invalid entry ID for forking`。用 `get_fork_messages` 取合法值。
4. **判定闸门时必须区分「Pi 不可用」与「探针写错了」。** 上面第 3 条就曾让 GATE-01
   被误判为「选项 ③」。探针失败时先怀疑探针。
5. **别让 mock 之外的流量出网。** `default_env()` 已设 `PI_SKIP_VERSION_CHECK=1` 与
   `PI_OFFLINE=1`；若某探针需要关闭它们，请在探针里显式说明原因。
6. **看紧磁盘。** 会话语义相关探针应显式传 `--session-dir` 指向临时目录，
   并在末尾校验该目录为空，否则可能漏掉落盘行为。

## 备注

六个闸门已全部实现并有结论（见上表）。本设施继续作为后续改动的回归验证工具：
改动 `infrastructure/pi_*` 或 `extensions/policy_enforcement.ts` 时，应重跑对应闸门探针。
