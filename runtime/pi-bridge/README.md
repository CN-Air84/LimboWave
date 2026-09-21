# Pi Runtime Bridge

> **本目录已被取代，仅保留作说明。**

设计计划 §二.2 预留的 Pi Runtime 桥接，实际落地在源码包内：

| 组件 | 位置 |
| --- | --- |
| AgentKernel 抽象接口 | [`src/limbowave/application/kernel.py`](../../src/limbowave/application/kernel.py) |
| Pi RPC 子进程客户端 | [`src/limbowave/infrastructure/pi_rpc.py`](../../src/limbowave/infrastructure/pi_rpc.py) |
| PiKernelAdapter | [`src/limbowave/infrastructure/pi_adapter.py`](../../src/limbowave/infrastructure/pi_adapter.py) |
| PolicyEnforcementExtension | [`src/limbowave/infrastructure/extensions/policy_enforcement.ts`](../../src/limbowave/infrastructure/extensions/policy_enforcement.ts) |

Phase 0 的合同核验与六项内核去留闸门均已完成并实机通过，结论见
[`docs/architecture/pi-runtime-contract.md`](../../docs/architecture/pi-runtime-contract.md)。

本目录不再承载实现，可安全删除；暂保留以免破坏既有引用。
