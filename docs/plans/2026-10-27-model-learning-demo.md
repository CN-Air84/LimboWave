# 模型三层关系：引导练习 Demo

日期：2026-10-27。状态：独立原生沙盒，等待用户验收；不修改正式 OOBE。

## 目标与顺序
不是术语卡片或开放式“自己试试”。按“解释一个概念 → 指定一个动作 → 针对错误反馈 → 动手成功 → 再进入下一步”练习。正式集成时放在添加并明确选择模型后、语音与联网等功能介绍前。

## 练习设计
1. 站点端点：辨认“地址、协议、密钥属于哪里”，正确选择后解释其余两层不负责认证。
2. 实际模型：同一ID在两个站点也是两个条目；一站可有多个ID，身份是站点+ID。
3. 逻辑模型：逻辑 ID 沿用实际 ID xyz-chat，显示名称为 XYZ Chat；保留型号，而非以用途命名。立即用真实 RoutingService 展示未绑定无法路由，不把命名当模型部署。
4. 绑定：把两个站点的两个实际模型逐个绑定，不选端点裸节点；显示实际模型身份与排序。
5. 首选：亲手调整顺序，再预测去向，点击模拟路由看 endpoint+model_id；不联网。
6. 能力：同名条目的工具支持可以不同；去实际模型能力处判断，而不是靠逻辑名称猜。
7. 故障：首选失败不静默跨站。明确选择另一已绑定站点，再主动模拟重试；无自动外发。
8. 带走：展示完整图和具体设置入口、首次引导自动完成的绑定/默认步骤。复盘不能冒充已配置真实用户模型。

## 安全与执行
- 仅新增 tools/demos/model_learning_demo.py、其测试、启动脚本和说明。
- 假站点使用 .invalid，无API key输入，不读用户资料；纯内存 AppConfiguration + RoutingService，无持久化/网络/模型请求。
- 错误不扣分、无红色惩罚；给原因和可执行提示，支持“带我看一步”、返回、重练、退出。
- 禁止只点继续通过，动作+验证必须都完成；辅助演示不是替用户交卷。
- 沿用正式主题和原生Qt，不新增依赖，不触碰并行搜索文件，不做首创宣传。
- 测试覆盖全通关、错误/提示、未绑定、顺序、能力不同、失败无切换和用户明确切换。深浅色/小窗截图QA后打开demo。

## Demo verification
- 7 guided lessons and final review implemented in an isolated native Qt window; real model schemas and RoutingService validate purely in memory.
- Full progression, wrong-answer recovery, hint without auto-answer, explicit binding/order/routing checks, ability differences, manual retry, back/relearn and reset tested.
- Combined demo regression: **38 passed** (21 model-learning cases plus 17 existing voice/network demo cases), 21.85 seconds. Ruff passed; dark/light/minimum-window screenshots reviewed.
- Model practice does not read user configuration, resolve credentials, make network/model requests, save progress or modify production onboarding/search files. Awaiting user acceptance before integration.

命名教学修正：区分逻辑 ID 与显示名称。练习采用 `xyz-chat` → `XYZ Chat`，保留型号，仅改善显示可读性。不要用“每天聊聊”等用途名称替代型号；此教学限制不改变真实设置允许的命名规则。

Latest naming correction verified: **26 model-learning tests passed**, including separate logical ID/display-name fields (`xyz-chat` / `XYZ Chat`) and identity-preserving lesson validation. Existing routing, real config naming rules, and production onboarding are unchanged.
