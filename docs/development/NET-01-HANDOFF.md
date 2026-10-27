# NET-01 READY 交接（2026-10-27）

## 状态

NET-01 实现完成，代码未提交；等待用户配置真实搜索 Key、调整本机 fake-IP DNS 后实网联调。
新增 OOBE 联网介绍仅在 demo 中，待用户和其他新板块统一验收，**不等于批准正式实装**。
正式 `src/limbowave/ui/oobe_copy.py`、`oobe_demo.py`、`oobe_live.py` 未修改。

## 默认值和能力

- 配置入口：设置 → 联网搜索。默认 provider=disabled，max_results=5（可选1–10），credential_ref=None。
- Google/Bing 经第三方 SerpAPI，或 Tavily；需要对应服务 Key，不接受 Google/Azure 原生 Key。
- 无静默服务回退、无自动购买、无收费自动重试；未实现模型原生 grounding。
- 搜索逐次授权；通配域名授权不能静默授权搜索词外发。Web 来源不能执行网络工具。
- read_url 无需搜索 Key，仍需公网访问授权；只读 HTML/文本，不登录、不执行脚本、拒绝内网。
- 有来源、查阅时间、截断标记，区分搜索摘要与读过正文；失败不编造，外部内容不是指令。
- 密钥只进入现有加密 vault，按请求解析，不进入模型、普通配置或原始错误输出。

## 实现文件

新增生产模块（均在 G:/LimboWave）：
- src/limbowave/domain/network.py
- src/limbowave/application/services/web_content.py
- src/limbowave/application/services/network_settings_service.py
- src/limbowave/infrastructure/web_access.py
- src/limbowave/infrastructure/web_search.py
- src/limbowave/infrastructure/network_backend.py
- src/limbowave/ui/network_settings_page.py

共享文件局部编辑：
- src/limbowave/app.py：settings/credentials 构造提前到网关前；注入 create_network_tools，早于 IPC 启动。
- application/services/tool_gateway.py：保留其隐私/历史/提醒等在途改动；只扩展网络依赖、错误及证据预算。
- application/services/settings_service.py + domain/configuration.py：network 配置及配置重建保留字段。
- domain/permissions.py：web_search 的逐次确认规则，不改其他能力。
- infrastructure/extensions/policy_enforcement.ts：仅 read_url/web_search 的模型说明。
- ui/settings_panel.py：新增 lazy 联网搜索页；tests/ui/test_settings_panel.py 只更新分类列表预期。
- README.md、docs/development/network-access.md、docs/plans/2026-10-27-net-01.md：配置、边界、验证。

独立测试：tests/unit/test_web_access.py、test_web_search.py、test_network_settings.py、
test_network_gateway.py；tests/ui/test_network_settings_ui.py。

## OOBE 联合验收

已在共享 tools/demos/voice_oobe_demo.py 加入 NetworkTryout、build_network、模拟状态和导航，
同时追加对应交互检查。随后 HIST 会话在共享文件中清理历史页；NET 收到协调后不再写该共享文件。
最终读取/截图确认当前共享稿为语音 + 联网，不替其他模块决定 OOBE 范围。

建议统一入口：
- tools/demos/oobe_joint_demo.py：继承共享 VoiceOobeDemo，不复制语音或其他页面；仅为联网页增加小窗口滚动和标题高度保护。
- tools/demos/start_oobe_joint_demo.ps1：无控制台启动入口。
- tools/demos/test_network_oobe_joint.py：独立4项合集检查。
- tools/demos/README-joint-oobe.md：评审说明。

全部纯模拟，无网络/麦克风/历史数据库/凭据读写，不保存真实配置。模拟授权不进入正式权限。
已查看深色、浅色和最小窗口截图；最小窗口原有裁切由独立合集入口解决，不抢写共享演示稿。
截图：.tmp/oobe-joint-net01/network-joint-dark.png、network-joint-article-dark.png、network-joint-minimum-light.png。

## 验证记录

NET-01 针对性测试：134 passed（包括现有 tool_gateway 回归与新 UI 测试）。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_web_access.py tests/unit/test_web_search.py tests/unit/test_network_settings.py tests/unit/test_network_gateway.py tests/unit/test_tool_gateway.py tests/ui/test_network_settings_ui.py -q --disable-warnings --tb=short
```

独立合集测试：4 passed。共享 demo 在 HIST 清理前为25 passed；清理后为13项。
最终当前版本合跑：**151 passed in 29.86s**（NET-01 134 + 共享 demo 13 + 独立合集4），见 .tmp/net01-final-focused.log。
Ruff 对本次生产模块、相关测试与独立合集入口通过；strict mypy 对13个受影响生产文件通过（follow-imports=silent）。

广回归命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit tests/integration --ignore=tests/unit/test_server_package.py -q --disable-warnings --tb=short
```

结果：2999 passed，14 failed，8 skipped（484秒）。日志：.tmp/net01-regressions.log。
未排除前，全量收集因既有缺失 scripts.package_server 失败。14项失败位于：
- preferences_service.py 导入Qt的层边界测试（非NET修改）。
- 会话切换 runtime 读取计数。
- companion_window.py:476 硬编码字号（非NET修改）。
- request_route_guard_e2e 图片透传2项。
- retry_context_e2e 上下文8项。
- run_coordinator_e2e 请求 Authorization 大小写断言1项。
未将这些跨模块问题混入 NET 改动，请统筹最终回归；不宣称全量测试全绿。
另：settings rail-hover 两项 UI 回归在 pre-NET 文件快照同样失败，未为本任务修改样式/测试断言。

## 实网前置条件

本机 DNS 将 example.com、serpapi.com、api.tavily.com 解析到198.18.*（fake-IP）。
受控传输正确返回 blocked_address；不能把本次检查当作真实取回成功。
需要真实公网 DNS / 非fake-IP代理配置，且尚未收到真实搜索服务 Key。
未开启环境代理继承，未放宽内网保护；20秒预算、DNS固定、同源跳转和内容大小限制保留。

## 合并注意事项

工作树开始时已有大量其他任务改动，本任务没有重置、整文件回退、提交或改动正式 OOBE。
对 app.py/tool_gateway.py/policy_enforcement.ts 等共享文件应做定点集成，保留其他会话变更。
生产模块和本次测试进程已完成；最终测试完成后无后台服务、无付费请求、无常驻演示进程。
独立合集依赖共享 VoiceOobeDemo 的 network 页面、network_tryout 与 network_continue_button；
若协调方调整接口，请一并更新合集封装，避免重复维护第二份页面。

最终 READY：本任务所有写操作、pytest、截图进程均已结束；无后台常驻进程。正式 OOBE 三文件 git diff 为空。
