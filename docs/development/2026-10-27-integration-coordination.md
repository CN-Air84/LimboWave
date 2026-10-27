# 本批功能统一收尾记录（2026-10-27）

协调入口：VOICE-01 会话。用户授权跨会话调度和联合核查；未授权提前实装 OOBE。

## 不变边界
- 正式 `oobe_copy.py` / `oobe_demo.py` / `oobe_live.py` 暂不修改。
- HIST-02 明确不进入 OOBE；语音 + 联网只保留独立联合 demo，待用户验收。
- 所有功能目前位于同一共享工作树；不整文件覆盖、回退、自动提交或声称已完成 Git 合并。
- 不用真实密钥，不做收费 API 调用，不放宽内网、授权或隐私保护。

## 状态与当前写集
| 功能 | 已收到状态 | 当前动作 / 写集 |
|---|---|---|
| VOICE-01 | 相关回归 287 通过；真实音频/API 未验收 | 正式语音实现已完成，OOBE demo 等待统一验收 |
| HIST-02 | READY；普通检索轮次关联P1已修复 | 已停写；普通检索分页前完整来源判定，OOBE无历史页 |
| NET-01 | READY；逐跳隐私保护99通过+2架构检查 | 已停写；每跳DNS前检查，保留正常重定向与SSRF保护 |
| CAP-01 | READY；CUSTOM × 配置/授权修复后17项通过 | 已停写，权限闸门不变；联合测试纳入快照交叉用例 |
| PRIV-02 | READY；两项负责P1修复260通过 | 已停写；网络预检+结构化结果指纹，HIST关联另由HIST修复 |
| CMP-01 | READY；限定测试修复4通过 | 已停写；未修改压缩生产逻辑，图片/鉴权断言保持且加强 |
| 周期提醒 | READY；迁移19→20与PRIV数据共存核查通过 | 已停写，UI/系统通知人工验收仍待实机 |

## 已知广回归记录（NET 会话提供，非当前重跑结论）
- 2999 passed / 14 failed / 8 skipped；`test_server_package.py` 因缺少 `scripts.package_server` 收集受阻，原命令显式排除。
- 原有 Qt 跨层导入与 companion 固定字号不混入功能修复。
- runtime 读取计数、图片透传、重试上下文、Authorization 断言正在定向区分真实回归与过时测试假设。
- 当前先运行 `.tmp/coordination-runtime-recheck.log`，待 CAP 修复和 PRIV 结论后统一聚焦回归。

## 完成门槛
1. 所有会话停写或交出明确最小写集；需要修复的问题有归属。
2. CAP 自定义权限描述与真实闸门一致；隐私审计无未处理高风险旁路。
3. 联合聚焦测试通过，较广回归失败逐项可解释；不将条件跳过算通过。
4. 正式 OOBE 三文件保持未改；联合 demo 无 HIST-02 页面。
5. 向用户汇总实现状态、测试、未做人工验收和下一步验收入口，而不是宣称全量全绿。

## 最新定向核查
- 重试与会话隔离：16 passed / 1 failed。8项重试已通过（测试已将CAP临时overlay与用户原文分开），余下runtime读取计数测试仍追踪旧list_for_conversation；HEAD也已使用metadata + list_for_entries。此项作为原有测试漂移记录，不为它回退生产优化。
- CMP证据：图片夹具PNG CRC损坏，Pi原图首次发送即省略；另有CAP overlay使“最后user消息”定位失效。HTTP Authorization实际正确，仅mock头名小写。按上述最小测试范围修复，不放松安全断言。
- PRIV审计发现的P1：①网络参数禁区检查晚于外发；②普通历史搜索丢失用户轮次的禁区关联；③来源正文指纹可被JSON/嵌套工具结果包装绕过。修复与新增回归进行中，未宣称联合完成。
- 已启动57文件联合聚焦测试（.tmp/coordination-focused-final.log），其结果仅覆盖启动时快照；PRIV/HIST修复落地后必须再跑受影响链路。

## 联合核查进度（修复前快照）
- 57文件联合聚焦回归完成：**1389 passed, 2 skipped**，240.08秒。日志 `.tmp/coordination-focused-final.log`；跳过是既有SQLite密文投影专用测试，不计通过。
- 52个已改生产Python模块的只读Ruff与定向mypy均通过；P1修复过程中会继续增加模块，最终需复验。
- CMP限定测试修复已READY：两个授权文件，共4 passed；原图精确字节和Bearer/脱敏断言未削弱，部分加强。
- NET发现同源Location第二跳不回网关，新增同根P1防护：NET独占`application/services/network_privacy.py` + `infrastructure/web_access.py` + 必需适配/测试；PRIV使用`guard_network_request(check_url)`在实际同步invoke作用域安装检查器，传输每跳DNS前调用。ContextVar不可仅在原始executor外安装。
- HIST修复已包含按有效分支反向追溯所属用户轮次并在分页前判定，正在交叉回归。PRIV继续覆盖网络预检和结构化来源指纹。
- 不把当前大回归通过结果当作新P1用例验证；收到各方最终READY后重新联合跑受影响链路。

## P1修复交接完成，最终联合回归进行中
- PRIV两P1已READY：全部NETWORK参数执行前检查、结构化结果来源/正文指纹检查；与NET每跳URL guard已衔接。限定合集260 passed，含5项真实Pi。
- HIST P1已READY：普通检索分页前追溯有效分支所属用户轮，来源不明或预算不足保守拒绝；最终单元186 passed/3 skipped，交叉202 passed/3 skipped（各为独立运行，不相加）。
- NET逐跳guard已READY：99 passed + 2架构边界检查，首跳/同源后续跳均在DNS前检查；跨源重定向地址返回前也检查。
- 统筹新增`tests/integration/test_network_privacy_gateway.py`，真实Gateway→作用域→PublicHTTPClient与搜索适配器连线测试：**18 passed**。覆盖双仓库、带编码同源/跨源Location、第二跳零DNS/HTTP、正常跳转/证据来源、三搜索提供方在禁区query下零凭据解析、来源指纹正文阻断，以及原始ThreadPoolExecutor线程内安装/复用后不串策略。全为离线假传输，不连接公网。
- 修复后53个已改生产Python文件Ruff/mypy通过；62文件最终联合回归运行中。

## 修复后联合证据
- **62文件：1536 passed, 3 skipped，250.57秒**。`.tmp/coordination-postfix-final.log`。这是全部P1实现停写后运行结果，包含语音、NET、HIST、CAP、PRIV、压缩、提醒、配置、权限、真实Pi及共享demo用例。
- 另新增18项真实适配层连线测试通过（离线HTTP/DNS伪传输），无条件跳过。它们与上述1536为不同测试集，可合计1554通过；不与各会话重复覆盖的历史测试数相加。
- 全部53个变动生产Python模块Ruff、mypy、编译检查通过；正式OOBE三个源文件逐一核对与HEAD一致；git diff --check通过。
- 各功能会话均已idle、释放写集。统筹正在补跑unit+integration较广回归（显式排除缺失包装模块的旧test_server_package.py），以及原有OOBE/权限/记忆/启动UI相关套件。

# 最终交接：本批实现和协调收尾完成，等待人工验收

## 最新运行结果（不相加重复覆盖的测试数）
- 修复后联合聚焦：**1536 passed / 3 skipped**（62文件，250.57秒）。
- 统筹新增网关→真实网络适配器离线连线：**18 passed**，含原始执行器线程作用域与复用隔离。
- 原有OOBE、权限提示、记忆面板、高级栏、设置与启动UI补充：**262 passed**（104.08秒）。
- 最终广回归：**3192 passed / 3 failed / 9 skipped**（unit + integration，452.39秒）。日志 `.tmp/coordination-broad-final.log`。
- 53个变动生产Python模块Ruff/mypy/编译检查通过；变动差异空白检查通过。

## 明确保留的原有检查问题
1. `PreferencesService`导入PySide6，违反现有层边界；HEAD已有，本批未动该文件。
2. `test_session_isolation.py`仍对旧`runtime.list_for_conversation`断言一次；HEAD生产路径已改成`list_metadata_for_conversation + list_for_entries`。心跳与线程检查本次仍通过，失败点仅旧API调用计数。未为迎合旧断言回退优化。
3. `companion_window.py:476`的20px硬编码字号；HEAD已有，本批未动该文件。
4. `tests/unit/test_server_package.py`是已有收集阻断：HEAD只有测试但无`scripts/package_server.py`/`install_server.py`；最终广回归显式`--ignore`，没有隐藏成通过。
5. 9个条件跳过含SQLite专用投影、POSIX与系统符号链接能力，不计通过。

## 本批修复和边界
- CAP CUSTOM搜索状态准确对应既有拒绝闸门，无授权放宽。
- PRIV网络外发前检查、NET每跳DNS前守卫、嵌套工具结果来源指纹、HIST用户轮次关联P1均修复且联合复验。
- 损坏PNG夹具、CAP overlay消息定位、Authorization头名大小写测试假设已修，精确图片/Bearer/脱敏断言保留或加强。重试8项已通过。
- 七项功能均收齐READY，所有参与功能会话已idle；本次未自动提交、打包、部署或执行Git合并。所有增量共存于共享工作树。
- 正式OOBE三文件逐一对照HEAD未变。联合demo只含语音和联网新增板块，HIST不进入OOBE。

## 用户下一步
- 统一验收demo：`tools/demos/start_oobe_joint_demo.ps1`或运行`tools/demos/oobe_joint_demo.py`。未获明确批准前不接入正式OOBE。
- 实机待验：语音麦克风/扬声器与真实提供方、搜索真实Key和公网DNS（fake-IP仍被拦）、安装包升级/Windows提醒气泡/休眠恢复。没有为本次联调使用真实密钥或收费API。
- 不宣称完整仓库全绿或已发布；上述原有基线问题可作为独立维护任务，勿混入已验收功能改动。
