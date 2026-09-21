# LimboWave / 灵波

Windows 优先、基于 PySide6 原生界面的桌面 Harness：日常聊天与 Agent 能力共用同一会话。

- 实施计划：[`docs/design_plan/2026-09-20-pi-chat-agent-harness.md`](docs/design_plan/2026-09-20-pi-chat-agent-harness.md)
- 环境事实与决策：[`docs/development/environment.md`](docs/development/environment.md)
- 架构裁决（ADR）：[`docs/architecture/adr-0001-agent-kernel.md`](docs/architecture/adr-0001-agent-kernel.md)
- Pi 接入合同（核验）：[`docs/architecture/pi-runtime-contract.md`](docs/architecture/pi-runtime-contract.md)

当前进度：**Phase 0 完成，内核已接入应用**。工程骨架 + Pi 合同核验 + 六项内核去留闸门实机验证全过 +
`AgentKernel` / `PiKernelAdapter` / `PolicyEnforcementExtension` / `SessionController` / 聊天视图 已落地，
GUI 与内核端到端联通（含优雅降级与权限默认拒绝）。

## 运行应用

应用启动需要指定模型。Phase 1 的站点管理尚未落地，**临时**用环境变量接缝：

```powershell
$env:LIMBOWAVE_PROVIDER = "mock"      # 或你的 provider 名（对应 ~/.pi/agent/models.json）
$env:LIMBOWAVE_MODEL = "mock-model"   # 模型 id（不含 provider 前缀）
.\scripts\run.ps1
```

未设置时应用以"无内核"模式启动：输入区禁用并提示配置，窗口与事件循环正常。

> 内核路线（ADR-0001）：**应用兼容内核层 + Pi 执行引擎**。应用定义稳定的 `AgentKernel`
> 接口，Pi 降级为其第一个可替换实现，不直接依赖 Pi 的 RPC 命令。

## 快速开始

依赖由 [uv](https://docs.astral.sh/uv/) 管理，Python 3.12（uv 托管，不依赖系统 Python）。

```powershell
.\scripts\bootstrap.ps1     # 建立 .venv、按 uv.lock 装依赖、环境自检
.\scripts\run.ps1           # 启动应用
.\scripts\verify.ps1        # 提交前基线：静态检查 + 全部测试
```

其余命令见 [`docs/development/environment.md`](docs/development/environment.md#四固定命令)。

> 本机未安装 PowerShell 7，脚本按 Windows PowerShell 5.1 兼容编写。
> 所有 `.ps1` 为 UTF-8 BOM + CRLF —— 修改脚本后请执行
> `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/normalize-ps1.ps1`。

## 工程结构

```text
src/limbowave/
├─ app.py             GUI 进程入口（Qt 与 asyncio 共用事件循环）
├─ bootstrap.py       运行环境与路径解析，不建立业务状态
├─ domain/            会话、分支、消息、模型与端点、权限、上下文
├─ application/
│   ├─ kernel.py      AgentKernel 抽象接口 + KernelCapabilities + 归一事件（不依赖 Pi）
│   └─ services/
│       └─ session_controller.py  会话控制器：内核 → 聊天级事件，Qt 无关
├─ infrastructure/
│   ├─ pi_rpc.py      Pi RPC 子进程客户端（async，字节分帧，合同 §三）
│   ├─ pi_adapter.py  PiKernelAdapter：AgentKernel 的 Pi 实现 + 硬化启动基线
│   └─ extensions/
│       └─ policy_enforcement.ts  PolicyEnforcementExtension（权限网关 + 观测通道）
├─ tools/             内置文件/目录/终端/联网工具及权限网关
└─ ui/
    ├─ main_window.py  主窗口（只发信号，不持有业务状态）
    └─ chat_view.py    聊天视图：消息流 + 输入 + 状态栏

tools/pi-verify/      Phase 0 内核去留闸门的实机验证设施（mock provider + 探针）
scripts/              固定命令与维护脚本
tests/                unit / integration / ui / fixtures
docs/                 设计计划、架构裁决（ADR）、Pi 合同与开发文档
```

> `runtime/pi-bridge/` 原预留位置已由 `infrastructure/pi_rpc.py` + `pi_adapter.py` +
> `extensions/policy_enforcement.ts` 承担，三者即「Pi Runtime 桥接」的实际落地。

### 架构约束

GUI 只负责展示状态与发出命令，不持有权威业务状态。这一条由测试守着
（`tests/ui/test_main_window.py::test_window_holds_no_public_state`）：主窗口的公开属性
只允许是信号，往窗口上挂会话或消息集合会直接让测试失败。
