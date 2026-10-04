# LimboWave / 灵波

Windows 优先、基于 PySide6 原生界面的桌面 Harness：日常聊天与 Agent 能力共用同一会话。

Linux 初始适配已加入：原生 Bash/Sh、平台路径与打包分支；**尚待 Linux 实机验收**。
启动、依赖与限制见 [Linux 支持说明](docs/development/linux.md)。
Linux 保留主密码解锁和加密备份，不支持 DPAPI/Windows Hello，应用内数据重置明确禁用。

- 实施计划：[`docs/design_plan/2026-09-20-pi-chat-agent-harness.md`](docs/design_plan/2026-09-20-pi-chat-agent-harness.md)
- 环境事实与决策：[`docs/development/environment.md`](docs/development/environment.md)
- 架构裁决（ADR）：[`docs/architecture/adr-0001-agent-kernel.md`](docs/architecture/adr-0001-agent-kernel.md)
- Pi 接入合同（核验）：[`docs/architecture/pi-runtime-contract.md`](docs/architecture/pi-runtime-contract.md)

当前进度：**Phase 1A 完成——应用权威配置闭环**。不再依赖环境变量：站点、逻辑模型、路由与
密钥引用全部由应用权威配置驱动，Pi 的 `models.json` 是运行时**派生**产物。
90 项测试（单元 / UI / 集成）与 ruff、mypy strict 全绿。

## 运行应用

模型由应用权威配置驱动（不再是环境变量）。配置位于资料库根目录：

```text
<data_root>/config.json     普通配置：站点端点 + 逻辑模型 + 默认模型（不含密钥）
<data_root>/vault/          加密密钥库：密钥密文与主密钥
```

- Windows 上 `<data_root>` = `%LOCALAPPDATA%\LimboWave`
- 密钥只在启动子进程时经**一次性环境变量**注入，绝不写入普通配置或 Pi 的 `auth.json`

配置方式与字段说明见 [`docs/development/configuration.md`](docs/development/configuration.md)。
未配置时应用以"无内核"模式启动：输入区禁用并提示，窗口与事件循环正常。

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

## 诊断日志

GUI 启动后自动记录五级诊断日志；在设置页顶部点击 **诊断日志**，或按 **Ctrl+Shift+L**
打开查看器（登录页面也可使用）。窗口显示日志路径、收集/筛选等级及写入故障状态。

```powershell
uv run python -m limbowave --log-level DEBUG
uv run python -m limbowave --log-console --log-dir "D:\LimboWave-logs"
```

后台批量写入、有限文件轮转和限流默认开启；过载可能丢弃记录，并明确计数。主日志被
另一实例占用时改写带进程号的独立文件；目录确实不可用时降级为仅内存记录，不阻止启动。
详细接口、资源预算、隐私边界与测试见
[`docs/development/diagnostics.md`](docs/development/diagnostics.md)。

## 打包发布

`scripts/packager.py` 用 PyInstaller 产出本平台程序（Windows / Linux 分支）：关于页内容可在图形
界面里填写并随产物注入，图标、单文件/标准打包方式、控制台隐藏（默认隐藏）与 UPX
压缩均可选；另有命令行模式供脚本与 CI 使用。

```powershell
uv run python scripts/packager.py             # 图形界面
uv run python scripts/packager.py --cli --help
```

机制、参数与注意事项见 [`docs/development/packaging.md`](docs/development/packaging.md)。

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
