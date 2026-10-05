# UI 线程非前端业务阻塞普查

日期：2026-10-05（Asia/Shanghai）

## 结论与范围

**存在明确的非前端业务阻塞，不只是动画或绘制性能问题。** 当前是“部分链路已后台化，其他入口仍同步调用同一批服务”，而不是统一的异步业务边界。

检查基于当前工作区，包含已有未提交改动。覆盖 GUI 装配、界面事件入口、会话协调、工具网关、历史/附件/压缩/设置服务及其存储实现；未修改产品代码。未连接真实模型，未读取真实资料库。没有进行真实用户会话的端到端性能录制，因此以下实测不是用户当前资料库的耗时预测。

执行边界：
- [app.py:3252](../../src/limbowave/app.py#L3252) 使用 qasync 的 QEventLoop，Qt 和 asyncio 共用 GUI 线程。
- Signal 槽函数、QTimer 回调，以及这个循环里的普通 async 函数，其同步部分仍在 UI 线程。create_task、singleShot 或添加 async 关键字本身不构成后台执行。
- [app.py:868–872](../../src/limbowave/app.py#L868) 已给持久化会话协调器装配 ConversationProcess，但只有显式调用该 worker 的操作受益。

## 隔离实测

使用本机 Python/PySide6/qasync、离屏 QApplication、5 ms Qt 定时器及临时合成资料库。表中数值为单次样本，不是 p95/p99；系统调度和 Windows 定时器精度会影响结果。

| 探针 | 业务耗时 | Qt 最大心跳间隔 |
| --- | ---: | ---: |
| 搜索 1,000 条约 1 KiB 消息，查询无命中 | 32.7 ms | 34.9 ms |
| 搜索 10,000 条约 1 KiB 消息，查询无命中 | 330.7 ms | 331.6 ms |
| 同一 10,000 条搜索，线程后台执行对照 | 347.7 ms | 20.2 ms |
| 非终端工具 list_directory，遇到 400 ms 写锁 | 366.3 ms | 368.0 ms |
| 同一工具和写锁，线程后台执行对照 | 364.8 ms | 10.1 ms |
| 导入 3840×2160 合成噪声 JPEG，规范化为优化 PNG | 3211.9 ms | 3213.9 ms |
| 仅包含极小配置文件的备份，默认 Argon2id 参数 | 61.4 ms | 62.5 ms |

工具探针提取并编译了 app.py 中原样的 _dispatch_tool 函数体，使用真实 ToolGateway、PermissionService 和 SQLite。不是完整 IPC 端到端测试。其他探针直接调用 UI 入口实际使用的业务服务，不包含额外控件构建成本。写锁总计持有 400 ms，计时开始前已有约 60 ms 定时器预热。

原始材料：
- 探针源码：`.var/ui-thread-audit-2026-10-05/probe.py`（仅本地）
- 结构化实测结果：`.var/ui-thread-audit-2026-10-05/probe-results.json`（仅本地）
- 测试输出：`.var/ui-thread-audit-2026-10-05/pytest.log`（仅本地）

## 按修复优先级排列的发现

### 1. P1：非终端工具整个执行与审计链在 GUI 线程

入口：[app.py:811–818](../../src/limbowave/app.py#L811)。只有 run_command 使用 asyncio.to_thread，其他工具直接 tool_gateway.invoke。IPC 在同一事件循环 await 这个派发器：[ipc_server.py:190](../../src/limbowave/infrastructure/tools/ipc_server.py#L190)。

影响：
- list_directory、stat_file、search_text、read_document、create_file、modify_file 的同步文件访问。
- search_text 先递归遍历并排序整个目录，再逐文件整块读取；max_results 不能限制前面的全目录枚举：[tool_gateway.py:200–225](../../src/limbowave/application/services/tool_gateway.py#L200)。
- 普通工具执行前权限审计还会同步读库、加密、写入并提交：[permission_service.py:152–175](../../src/limbowave/application/services/permission_service.py#L152)。工具调用即使很轻，也可能因数据库写锁卡住主线程。
- 扩展的前置授权回调同样直接调用 permissions.authorize / memory_service.record_decision：[app.py:536–612](../../src/limbowave/app.py#L536)。仅迁移 invoke 还不完整。

实测已经用简单列目录复现 368 ms 心跳间隔。SQLite 连接未配置自定义超时，默认锁等待还能更长。**生成过程中出现整窗停顿、停止按钮也不响应，首先排查这里。**

建议：将同步文件工具和权限读写放入受控 worker；用户确认弹窗保留在 GUI。记忆授权状态和运行上下文不能直接跨线程无保护共享，应传不可变上下文快照并在返回时校验 run/conversation/branch 是否仍有效。递归扫描另加文件数、字节数和取消边界。

注意：当前 GUI 没有装配 NetworkTools 的 HTTP fetcher，不将尚未接线的同步 HTTP 请求当成已发生的网络卡顿。

### 2. P1：历史搜索每次输入都全库读取、解密和匹配

入口：[app.py:1758–1766](../../src/limbowave/app.py#L1758)，搜索浮窗 textChanged 直接接刷新函数：[app.py:1781–1796](../../src/limbowave/app.py#L1781)。没有搜索防抖、取消或后台查询。

业务：[history_service.py:183–218](../../src/limbowave/application/services/history_service.py#L183) 遍历所有会话、分支和消息；加密字段在 SQLite 仓库转换领域对象时解密。

实测 1 万条消息约 332 ms 停顿，而且每次按键都可能再次触发。结果控件构建尚未计入。

建议：复用异步读取边界，增加约 150–250 ms 防抖、查询代次校验及结果分页；避免迟到结果覆盖新输入。更大资料库应考虑专门的搜索方案，但不能为了速度悄悄把加密正文写入明文全文索引。

### 3. P1：附件导入、发送组装和缩略图解析仍同步

入口与业务：
- [app.py:1234–1264](../../src/limbowave/app.py#L1234)：拖入/选择文件、粘贴图片，直接 index_path/import_file/import_bytes。
- [image_service.py:121–146](../../src/limbowave/application/services/image_service.py#L121)：整文件读取、格式解析、blob 写入、哈希与数据库提交。
- [app.py:1218–1225](../../src/limbowave/app.py#L1218)：导入后立即重新读解密后的原图并同步解码生成缩略图。
- [app.py:1140](../../src/limbowave/app.py#L1140)、[app.py:2413](../../src/limbowave/app.py#L2413)：发送/编辑发送之前，先同步 attachments.build，再创建异步发送任务。
- [attachment_service.py:53–61](../../src/limbowave/application/services/attachment_service.py#L53)：每个附件都枚举图片记录，图片原图读取并 Base64 编码。
- [app.py:1194–1216](../../src/limbowave/app.py#L1194)：已发送附件解析器缓存未命中时同步读库/读图。历史加载预热过的附件可命中缓存，不能因此认为实时新增附件也已后台化。
- [app.py:1305–1329](../../src/limbowave/app.py#L1305)：文件夹枚举后串行导入最多 50 个文件。

建议：后台完成读取、校验、加密、元数据和 QImage 解码，GUI 只消费结果并创建 QPixmap/控件；按 ID 查询而非每个附件 list_all；缩略图不要反复解码原图。发送在后台准备载荷期间应有忙态和上下文校验。

### 4. P1：手动分叉、失败重试、开始编辑仍绕过已存在的进程边界

- [run_coordinator.py:672–689](../../src/limbowave/application/services/run_coordinator.py#L672)：retry_user_message 在 async 方法里直接查消息、遍历分支并恢复附件。
- [run_coordinator.py:726–762](../../src/limbowave/application/services/run_coordinator.py#L726)：fork_message 同步读取当前分支、会话运行及运行时镜像，构建恢复快照；之后 [769–772](../../src/limbowave/application/services/run_coordinator.py#L769) 又直接写分支/记忆并提交。
- 手动分叉发出的 BRANCHED 事件不携带预构建 history，GUI 会落入 [app.py:3080](../../src/limbowave/app.py#L3080) 的 _show_branch，再同步查完整分支和历史载荷：[app.py:2370–2375](../../src/limbowave/app.py#L2370)。
- [app.py:2381](../../src/limbowave/app.py#L2381)：进入编辑为了找一条消息，先读当前分支全部消息。

建议：扩展 ConversationProcess 操作集合，后台准备 retry/fork 及历史 DTO；单条编辑使用按 ID 读取或已加载内容。不要改动必须留在 GUI 循环的实时内核/控件对象。

特别区分：**regenerate 本身已有 worker 路径，不能把它与手动 fork 一概而论。**

### 5. P1：外观导入有秒级阻塞，预览还隐式重读完整历史

- [appearance_editor.py:696–708](../../src/limbowave/ui/appearance_editor.py#L696)：选择背景后同步 import_background。
- [appearance_theme_service.py:172–188](../../src/limbowave/application/services/appearance_theme_service.py#L172)：EXIF 转正、RGBA 转换、整图字节拼接、SHA-256、PNG optimize 保存全在调用线程。
- 4K 合成 JPEG 实测停顿 **3.21 秒**。
- [app.py:1961](../../src/limbowave/app.py#L1961) 将 appearance_changed 接到 _apply_appearance；其默认 refresh_history=True，在 [989–993](../../src/limbowave/app.py#L989) 同步读取当前分支全部消息并重建历史载荷。调外观不应再次做数据库业务。预览有定时合并，但它没有迁移执行线程。
- [appearance_editor.py:758–767](../../src/limbowave/ui/appearance_editor.py#L758)：字体导入亦同步读文件、哈希和复制；字体注册本身应按 Qt 的线程要求处理。

建议：图像规范化/保存后台化；外观预览只更新已有视图样式，不重新查询会话。背景模糊生成已在 worker 中，不要误把所有背景处理都当成同一条路径。

### 6. P2：导出面板准备及正式导出都同步

[app.py:2138–2151](../../src/limbowave/app.py#L2138) 同步遍历全部会话、读取分支；[app.py:2175](../../src/limbowave/app.py#L2175) 在确认按钮回调里执行完整 export_selection。

全会话列表统计、历史/工具步骤和附件读取、格式转换与文件写入都会挤占 GUI。多会话、多分支、带图导出时风险更高。

建议：后台准备选择列表和导出产物，GUI 保留选择/确认/进度。防止重复提交，关闭应用时等待已提交写入完成。

### 7. P2：备份恢复及“启用系统保护恢复”同步执行加密和 I/O

- [app.py:2213](../../src/limbowave/app.py#L2213)：备份确认回调直接 service.create。
- [app.py:2256](../../src/limbowave/app.py#L2256)、[2268](../../src/limbowave/app.py#L2268)：恢复直接 inspect / restore_to。
- [backup_service.py:74–106](../../src/limbowave/application/services/backup_service.py#L74)：整库/所有 blob 读取、哈希和加密归档。
- [settings_dialog.py:546–548](../../src/limbowave/ui/settings_dialog.py#L546)：enable_recovery 同步验证密码、派生密钥、DPAPI 包装及写盘。

极小配置备份实测已产生约 62.5 ms 心跳间隔；实际耗时取决于资料库、附件数量和 KDF，不应把这个小样本外推成真实备份耗时。

建议：独立后台任务及明确进度/不可重入状态。保持现有密码学参数和安全确认，不通过降低 KDF 成本掩盖 UI 卡顿。

### 8. P2：压缩、记忆/权限面板还有同步数据库工作

- [app.py:1485](../../src/limbowave/app.py#L1485)：压缩按钮或自动预览直接 create_version，读取完整分支并提交版本。
- [compression_service.py:219–223](../../src/limbowave/application/services/compression_service.py#L219)：async generate 内直接 get/build_prompt，再次读取完整分支并组装提示词。生成网络等待是异步的，不代表前后存储也是异步。
- [settings_panel.py:427–434](../../src/limbowave/ui/settings_panel.py#L427)：打开设置时，即使目标不是记忆页，也立即 reload 记忆和多组配置。
- [session_memory_panel.py:178–192](../../src/limbowave/ui/session_memory_panel.py#L178)：直接读取记忆、设置与策略。
- [permissions_dialog.py:112–120](../../src/limbowave/ui/permissions_dialog.py#L112)：list_audit 后才取最后 50 条，不是数据库层限量查询。

建议：后台准备压缩输入/版本、结果持久化和面板 DTO；只刷新当前页需要的数据。审计日志在查询层分页。小数据时未必明显，但不应依赖“小库”保证响应性。

### 9. P2：每轮发送的模型目录“指纹未变”也不是纯内存路径

[app.py:2603–2605](../../src/limbowave/app.py#L2603) 先 load 配置、resolve_catalog、refresh_catalog，再比较指纹。

底层会读取 config.json；每条目录项解析凭据时 SecretStore.get 都会读取 secrets.json；EnvironmentBuilder._write_models_json 还会读取已有 models.json 来比较内容。参见 [secret_store.py:38–60](../../src/limbowave/infrastructure/crypto/secret_store.py#L38) 和 [environment_builder.py:172–220](../../src/limbowave/infrastructure/pi_runtime/environment_builder.py#L172)。

因此 [app.py:2620](../../src/limbowave/app.py#L2620) “只做一次内存比较”的注释不符合实际。通常文件小，风险低于前几项；站点/模型多、盘慢或杀毒介入时会扩大。

建议：配置版本/dirty 标记及可失效快照；配置或密钥变更后后台重建目录。不能缓存后漏掉密钥撤销、能力声明变更等热更新。

### 10. P2：其他同步入口应一并归口，不宜逐按钮补丁

- [app.py:1344–1367](../../src/limbowave/app.py#L1344)：引用其他会话片段，打开时同步统计所有会话，切换选项直接读取完整会话。
- [app.py:2035](../../src/limbowave/app.py#L2035)：删除会话在 async 外壳中同步执行级联删除；重命名、删分支、权限切换同类。
- 外观资源清理、偏好/站点/逻辑模型保存、记忆增删改、授权撤销等仍同步写盘/提交。
- 大多数单次写入可能很短，但会与后台会话进程竞争 SQLite 写锁；“连接各自独立”并不等于“写锁不会阻塞 GUI”。

## 已后台化/不应误报的路径

- 启动密码校验、迁移、内核装配等多处通过 _run_startup_task 执行；数据重置密码验证使用 to_thread。
- 普通会话列表、分支计数、历史页面数据读取使用 HistoryReader；会话切换准备使用 to_thread。
- 持久化会话的发送落库、记忆提示准备、重生成准备、部分编辑分支操作和结束落库通过 ConversationProcess 执行。
- run_command、模型发现/探测使用 to_thread。
- 请求日志分页/详情有独立读取执行器；背景模糊计算有执行器；Pi 的部分大帧解析、恢复文件准备已移出事件循环。
- 背景渲染 Future 的 result() 在完成回调中调用，并不是等待未完成任务的主线程阻塞。

这些后台线程仍可能因大量纯 Python 工作争用 GIL，不能等价于“零掉帧”。当前数据已说明迁移边界能明显降低直接停顿，必要时再针对 CPU 重任务使用进程。

## 回归验证

已执行以下现有测试，**26 passed in 14.62s**：
- tests/ui/test_regenerate_responsiveness.py
- tests/ui/test_session_switch_during_generation.py
- tests/ui/test_startup_responsiveness.py
- tests/unit/test_history_reader.py
- tests/unit/test_conversation_process.py

它们覆盖已有后台化路径，不覆盖全部此次发现；通过不能证明全应用没有主线程阻塞。新增探针保存在 .var，未修改现有测试或产品代码。

## 推荐实施顺序与验收

1. 先处理非终端工具/授权、搜索、附件及手动 fork/retry，覆盖最常见聊天期间卡顿。
2. 外观预览停止重查数据库，背景资源导入后台化；随后迁移导出、备份恢复、压缩输入构建。
3. 统一 UI→业务任务边界；工作线程独立创建/关闭 UoW，不跨线程携带 SQLite 连接或 QWidget/QPixmap。读任务考虑限流/取消，写任务串行化并处理关停等待。
4. 缓存配置/目录和轻量 DTO，采用 generation/run/location 校验，防止会话切换后的迟到结果污染界面。
5. 扩展响应性回归：注入 300–500 ms 文件/数据库延迟时，Qt 心跳和停止/导航仍工作；加入大历史搜索、带图发送、导出、手动分叉和背景导入用例。CI 可先采用最大心跳间隔 <100–150 ms 的稳健门槛，本机交互目标再向 16–33 ms 靠拢。
