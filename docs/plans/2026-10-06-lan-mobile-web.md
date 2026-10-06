# 局域网服务与移动端 Web Implementation Plan

**Goal:** 让同一局域网内的手机通过浏览器使用 LimboWave，共享电脑上的会话与模型能力，不需要安装移动端 App。
**Architecture:** 首版在已解锁的桌面进程内增加可开关的 Web 服务，抽出 Qt 无关的共享运行时门面；桌面与 Web 共用会话、权限、运行协调器和加密存储。浏览历史各端独立，执行命令集中仲裁，浏览器不直接连接 Pi 或内部工具 IPC。
**Tech Stack:** 现有 Python / asyncio / qasync / SQLite / Pi；拟新增 FastAPI + Uvicorn、React + TypeScript + Vite；REST 命令 + SSE 事件流。

> 状态：2026-10-06 方案草案，待确认，不是已经实现的功能。依照 brainstorming、writing-plans、architecture-designer 与 ui-ux-pro-max 工作流编制。执行时按 executing-plans 工作流分阶段验证；本轮不改业务代码、不安装依赖、不启动监听、不提交 Git。

---

## 1. 默认理解与范围

本计划默认需求是“电脑开着 LimboWave，手机连同一 Wi-Fi 即可聊天和查历史”，不是远程桌面投屏，也不是先把整个桌面软件重写成网站。

尚未由用户确认的三个默认值：

1. **桌面附带服务优先**：首版需要电脑端运行并解锁资料库；退出电脑端后网页不可用。真正不启动 GUI 的后台服务作为后续阶段。
2. **单用户、多设备**：配对的手机属于同一资料库所有者，默认能查看该资料库全部会话，不宣称多账号隔离。
3. **一个生成通道**：首版不支持不同设备同时在不同会话里生成；允许同时浏览，冲突操作明确返回“正在忙”，不静默排队。

如果首要目标其实是 NAS / 无显示器主机长期运行，应把第 9 节的无头阶段前置，不能把“桌面最小化运行”当成无头服务交付。

### 首版 MVP 必须具备

- 电脑端开启 / 关闭局域网访问，展示实际可访问地址、连接二维码和已连接设备。
- 配对、认证、登出和电脑端撤销设备；未认证不能读取任何会话内容。
- 手机端会话列表、分页历史、新会话、发送文本、停止当前生成。
- 显示流式正文、思考折叠区、工具执行状态和错误；沿用已有消息与分支语义。
- 选择已经配置的逻辑模型；不在手机上配置 API Key 或编辑端点。
- 网络切换、刷新、手机切后台后恢复当前状态，不重复发送、不丢最终结果。
- 320–430 CSS px 手机宽度可用，横屏和平板不溢出。

### 后置功能

- P1：图片 / 文件上传、搜索、会话重命名、重生成 / 编辑后分叉、分支选择、导出。
- P2：纯后台启动、服务自启动、HTTPS 条件满足后的 PWA 外壳缓存。
- 暂不做：公网穿透、多人账号、远程主密码解锁、手机修改密钥与资料库、远程任意终端、手机审批高风险工具、全量设置页、离线发送队列、推送通知、多会话并行执行。

## 2. 仓库现状与改造依据

以下为本次实际阅读的代码，而非仅依据 README：

| 位置 | 现状 | 对本方案的影响 |
| --- | --- | --- |
| G:/LimboWave/src/limbowave/app.py | Qt 与 asyncio 共用事件循环；运行时、权限弹窗、模型切换、历史预览等装配仍集中在这里 | 不能让 HTTP handler 调桌面控件或直接复用其闭包，需先抽出共享入口 |
| G:/LimboWave/src/limbowave/application/services/session_controller.py | 会话操作薄门面，已有 send / abort / subscribe 等 | 可复用，但当前活动会话是运行时全局状态，不是每个浏览器独立状态 |
| G:/LimboWave/src/limbowave/application/services/run_coordinator.py | 管理消息落库、运行、重试、分支和活动内核 | 保持唯一业务权威，不复制聊天引擎 |
| G:/LimboWave/src/limbowave/application/events.py | ChatEvent 支持正文、思考、工具及运行终态事件 | 可以转成 Web 事件，但尚无可直接承诺给浏览器的版本、序号和恢复协议 |
| G:/LimboWave/src/limbowave/application/services/history_service.py | 只读历史服务；当前部分方法扫描完整历史 | 不能仅把全量结果切片就声称完成大历史性能优化，需要分页读取路径 |
| G:/LimboWave/src/limbowave/infrastructure/conversation_process.py | 已有隔离存储工作进程 | 保留；“单一业务运行时”不等于移除这个存储子进程 |
| G:/LimboWave/src/limbowave/infrastructure/tools/ipc_server.py | 专用回环 TCP / JSONL、明确禁止绑定 0.0.0.0 | 继续只用于内部工具通信，不能充当 Web API |
| G:/LimboWave/src/limbowave/composition.py | 根据权威配置与 VaultKey 创建内核 | 复用现有配置和凭据注入机制，不给手机下发供应商凭据 |
| G:/LimboWave/tests/unit/test_architecture_boundaries.py | 守卫 domain / application 不导入 Qt | 扩展守卫，新增共享业务模块也不能依赖 Web 框架 |
| G:/LimboWave/scripts/packager.py | 已处理 PyInstaller 资源打包 | 需要加入前端构建产物和 Web 运行依赖 |

当前工作区存在导出相关未提交改动，包括 G:/LimboWave/src/limbowave/app.py；实施时须增量整合，不覆盖这些工作。本计划只新增自身文件。

## 3. 方案对比与架构决策（草案 ADR）

### ADR-LAN-01：部署形态

| 方案 | 优点 | 代价 / 风险 | 决策 |
| --- | --- | --- | --- |
| A. 桌面进程附带 Web 服务 | 复用解锁状态、权限窗口、唯一运行时；最快形成闭环 | 依赖桌面存活；需验证 qasync 与 ASGI 生命周期 | **首版推荐** |
| B. 独立后台服务，桌面也作为客户端 | 可无头运行，长期结构清晰 | 要前置解锁、安全授权、进程所有权和桌面协议改造 | 后续演进 |
| C. 新写一套独立 Web 聊天应用 | 前端开发表面上直接 | 复制路由、权限、加密和运行状态，产生两个权威 | 不采用 |

### ADR-LAN-02：接口与前端

- 采用 FastAPI + Pydantic DTO；与现有 Python/Pydantic 栈一致。相比纯 Starlette，增加少量框架开销，换取结构化校验和明确合同。
- REST 负责命令 / 查询，SSE 负责单向事件。相比 WebSocket，更符合聊天的“请求提交 + 服务端持续输出”，重连路径也更直接。后续只有确有双向实时需求时再引入 WebSocket。
- React + TypeScript + Vite 作为独立前端；不用 Next.js / SSR，也不额外部署 Node 服务。相比原生 JS，多一个构建链，但流式状态、历史分页和移动端交互更易测试维护。
- 正式运行由同一个 Python 服务提供静态资源与 API，同源部署；Node 仅用于开发 / 构建，以及项目本已有的 Pi 运行需求，二者不可混淆。

### ADR-LAN-03：权限与状态

- 电脑端和手机端均经同一个 RuntimeFacade 提交有副作用的运行命令。
- UI 的“正在看哪条会话”不是“内核正在执行哪条会话”；Web 查询历史绝不隐式切换内核。
- 手机认证只是进入资料库的权限，不等于授予主机文件与终端权限。
- 默认只读历史 + 聊天；如允许手机发起 Agent 任务，必须由电脑端单独开启，敏感操作仍在电脑端确认。

### 目标结构

~~~text
桌面 Qt UI ─────────────┐
                        ▼
                    RuntimeFacade ─── SessionController / RunCoordinator
                        ▲                         │
手机浏览器 ──HTTPS── Web API                      ├── Pi / AgentKernel
               │        │                        ├── ToolGateway / 桌面审批
               │        └── EventBroker           └── 加密 SQLite / Blob / 存储进程
               └── React 静态资源

内部 ToolIpcServer：保持 127.0.0.1，独立认证，绝不暴露到 LAN。
~~~

## 4. 关键行为合同

### 4.1 运行时与生命周期

- 新增 Qt 无关 RuntimeFacade，聚合历史查询、运行状态、命令仲裁和事件订阅；网络层只依赖它的公开接口，不访问协调器私有字段。
- Uvicorn 以受控协程挂到现有事件循环；不要在 Qt 运行中调用会另起事件循环的 uvicorn.run / asyncio.run。先验证锁定版本下的信号处理、异常传播、暂停和停止机制，不依赖未经验证的私有 API。
- Web 的启动 / 停止不得重新创建已有 SessionController、VaultKey 或 Pi 内核。
- 关闭 Web：停止接收新命令、撤销全部 Web 会话、关闭事件流并清空配对窗口；已开始生成默认继续在电脑端执行。
- 退出程序：先停止接收请求，按原有路径中断 / 收尾运行，等待存储任务结束，再关闭数据库和运行时。不能为了快速退出跳过落库。
- 资料库未解锁 / 已失效、重置进行中、运行时未就绪时拒绝对应操作；不能回退成绕过加密的临时 Web 会话。若当前没有完整锁库生命周期，需增加明确失效钩子，不假定已存在。

### 4.2 多端竞争与幂等

- 每个浏览器维护自己的查看会话和输入草稿；服务端维护唯一当前运行。电脑查看历史的既有体验不退化。
- 发送命令必须显式带 conversation_id、branch_id、expected_revision、client_command_id 和 server_epoch。首轮新会话使用服务端分配的草稿目标，或由 new-session 命令返回目标句柄；不能靠客户端猜 ID。
- 对“验证目标 → 恢复目标上下文 → 选择模型 → 启动运行”加统一命令锁，所有桌面与 Web 相关写入口必须经过它；只锁 Web handler 不足以避免竞争。
- 命令锁只覆盖状态转换，不覆盖整段生成。忙时仅允许匹配 run_id 的停止命令、只读查询和事件订阅，其他生成 / 切换操作返回 409。
- 手机 A 和电脑同时提交时只有一个成功；失败一端保留草稿并展示冲突原因。不偷偷切到另一会话，不自动排队重发。
- 同一命令 ID 和同一载荷返回同一回执；同 ID 不同载荷返回 409。服务端先登记 pending，再触发执行，失败也保留可查询回执，不能因 HTTP 断开丢掉去重信息。
- 首版去重保证限定在当前 server_epoch / 有效会话期内：回执保留到认证失效；容量达到上限时拒绝新命令，不提前淘汰仍有效记录。重启会换 epoch 并使认证失效；重新配对后必须先读状态，**禁止自动跨 epoch 重放旧命令**。
- 不承诺上游模型调用跨进程崩溃的“恰好一次”；恢复时依据已落库 run 状态显示中断 / 结果，让用户明确决定是否重试。

### 4.3 流式事件、历史和恢复

拟定事件包字段：protocol_version、server_epoch、seq、kind、conversation_id、branch_id、run_id、message_id、payload；不适用的 ID 可为空，不能省略作用域判断。

- 复用应用事件语义，但经白名单 DTO 转换；不把 ChatEvent.data、异常对象或历史仓库对象原样序列化。
- 正文 / 思考 / 工具 / 运行状态与会话列表变化都进入同一有序事件通道。每个客户端按 seq 去重，终态后忽略过期 delta。
- SSE 使用认证 Cookie，支持 Last-Event-ID。新连接先获取一致的运行快照与游标，再补游标后的事件；必须测试快照与订阅之间的竞态，不允许漏掉首个 delta。
- 环形缓冲同时限定事件数和字节数，初始目标 2,048 条 / 8 MiB，先到者触发淘汰；每连接另有有界队列。慢客户端断开并要求 resync，不反向阻塞生成或无限占内存。
- 游标过期 / epoch 改变时发送明确 resync_required，客户端以历史 + 当前未落库流式快照重建界面；不能只读数据库导致正在生成的文本消失。
- 浏览器切后台后连接可中断；回前台重新取状态，不承诺持续保活。断开不等于用户取消生成。
- 历史基于稳定游标分页，默认每页 50 条、上限 100 条；沿用分支祖先路径语义。大历史不得在请求内同步解密全部消息，采用现有后台读取边界并新增受限读取路径。
- token 更新合并到 30–60 ms UI 批次；用户向上读历史时不强制滚底，显示“有新内容”。

### 4.4 拟定 API（实现前锁定 DTO）

| 接口 | 用途 / 约束 |
| --- | --- |
| GET /healthz | 只返回最小存活信息，不公开会话、模型、路径或锁库详情 |
| POST /api/v1/pair | 一次性配对票据交换；限频，必须处于电脑端开启的配对窗口 |
| GET /api/v1/pair/status | 只查询当前配对申请是否获电脑端确认，不泄露其他申请 |
| POST /api/v1/logout | 撤销当前会话 |
| GET /api/v1/session | 当前设备权限、epoch、受控能力标志；不返回认证秘密 |
| GET /api/v1/state | 活动 run、运行快照与事件游标；认证后可见 |
| GET /api/v1/models | 已配置且可用的逻辑模型、公开能力；无端点 URL / Key |
| GET /api/v1/conversations | 分页会话摘要 |
| GET /api/v1/conversations/{id}/messages | 校验 branch 归属后分页历史 |
| POST /api/v1/commands | new-session / send / abort / select-model 的受限联合 DTO |
| GET /api/v1/commands/{client_command_id} | 超时后查询命令是否已接受；不直接重发消息 |
| GET /api/v1/events | SSE；认证、游标重放、撤销即断开 |

返回错误统一为 code / message / request_id / retryable；按认证失败、禁止操作、目标不存在、状态冲突、限流、未解锁 / 未就绪分类。错误中不回传堆栈、环境变量、主机私有路径或供应商密钥。

## 5. 局域网与安全设计

### 5.1 监听与配对

- 默认关闭服务。用户从电脑端主动启用后，选择一个明确的网卡地址和端口；建议端口 8765，可修改。不得把 0.0.0.0 展示成手机可访问地址。
- 首版优先绑定选定的私有 IPv4 地址；IPv6、多网卡自动切换、mDNS 后置。需要重新绑定时由用户确认，不悄悄扩大暴露范围。
- 列出候选 LAN 地址和网络适配器，端口占用明确失败，不静默换端口。提示 VPN、访客 Wi-Fi / AP 隔离、主机休眠等常见原因。
- 不自动修改防火墙、不申请公网映射、不做 UPnP；仅提供限制到 Private 网络 / 本地子网的配置说明。
- 二维码使用短期高熵一次性票据，不承载主密码或长期 token；票据放 URL fragment，网页读取后立即清理地址，并通过 POST 交换。
- 提供限时手输码作为备用；短码严格限速与尝试次数，并要求电脑端确认该次申请。确认页面显示匹配校验短语，而不是只相信可伪造的设备名称。
- 配对票据约 2 分钟有效，一次使用；设备会话只在本次服务运行内有效，闲置 / 绝对过期时失效，初值 2 小时 / 12 小时。服务重启、关闭或撤销立即失效。

### 5.2 HTTPS 与浏览器边界

- **正式 LAN 验收采用 HTTPS。** 支持配置证书 / 私钥或指导使用本地 CA；证书须覆盖实际访问的 IP / 主机名，手机须正确安装并信任。不得把“点过证书警告”视为可信 HTTPS。
- TLS 默认在此 Web 服务终止；证书私钥仅本机受限读取。证书不存在 / 无效时给出具体错误，不静默降级明文。
- 开发时允许仅 loopback HTTP；不把它宣传为安全的手机访问路径。若之后决定支持 LAN HTTP，必须单独确认风险与权限收缩，不作为本草案默认交付。
- 认证使用 HttpOnly、Secure、SameSite=Strict 的不透明 Cookie；服务端存储 token 摘要。正式同源提供页面，不把 token 放 localStorage 或 URL 查询串。
- 所有写接口校验 Origin 和 CSRF token；Host 使用实际允许地址名单，防止 DNS rebinding。拒绝不匹配 / null Origin 的浏览器写请求，不启用 CORS 通配。
- 设置 CSP、frame-ancestors 'none'、nosniff、Referrer-Policy: no-referrer；敏感响应 Cache-Control: no-store。生产默认关闭交互式 API 文档。
- 不承诺 HTTP LAN 上的 Service Worker、安装型 PWA、自动剪贴板或相机能力；前端按实际能力降级，首版不依赖这些功能。

### 5.3 内容与工具权限

- API Key、VaultKey、内部 IPC token、环境变量和终端控制接口绝不进入 Web DTO、二维码、访问日志或异常响应。
- 首版默认远程聊天权限不包含工具执行；若电脑开启“允许远程 Agent”，以不可变 run_origin 标记该次运行，并施加远程权限上限。
- 有效工具授权 = 现有策略 ∩ 远程上限；不能因当前会话已有“全信任”预设就绕过远程限制。文件 / 终端 / 网络工具及记忆修改等副作用均需覆盖。
- 手机只显示“等待电脑确认”，不提供铸造 confirmed=true 的接口；电脑不可审批时超时拒绝，不自动同意。
- Markdown 禁止原始 HTML，经安全渲染 / 清洗；脚本 URL、SVG / HTML 上传、事件属性不得执行。外链加安全属性，远程图片默认不自动加载以免泄露阅读行为。
- P1 附件只能通过认证后的 opaque ID 读取，校验对象归属、大小 / MIME / 魔数，禁止任意主机路径和目录遍历。受限文件强制下载，不在页面内执行主动内容。
- 草稿和聊天状态首版只保存在页面内存；不把明文历史存入 localStorage / IndexedDB。退出 / 被撤销时清空页面状态与订阅。
- 限制请求大小、请求速率、配对尝试、活跃会话数、SSE 连接数和队列内存；日志只留脱敏元数据，不留提示词和完整事件体。

## 6. 移动端界面方案

### 信息结构

1. **连接 / 配对页**：服务名称、证书 / 连接帮助、配对状态；电脑未解锁或配对已过期时给出可执行提示。
2. **聊天页**：顶部会话标题 + 连接状态；中部消息流；底部多行输入、模型入口、发送 / 停止。手机以此页为主，不照搬桌面三栏布局。
3. **会话抽屉**：标题、最近活动、生成中标记、新建会话；搜索在 P1。阅读别的会话时仍能看到“另一会话生成中”的入口。
4. **更多 / 连接信息**：当前模型、资料库连接状态、退出设备。敏感设置留在电脑。

### 布局与交互

- 沿用 LimboWave 现有视觉语言，取色前检查 G:/LimboWave/src/limbowave/domain/appearance.py 和 G:/LimboWave/src/limbowave/ui/theme.py；如文件结构变化，以实际主题入口为准，不复制 Qt 私有样式代码。
- 间距建议 4 / 8 / 12 / 16 / 24 / 32；正文 16 px、辅助信息 13–14 px、标题 18–20 px。字体以系统 UI 字体 + 中文系统回退为主，不为首版增加远程字体依赖。
- 语义颜色使用 background / surface / text / muted / accent / danger / border token，支持明暗主题；正文对比度至少 4.5:1。
- 触控目标至少 44×44 CSS px，不依赖 hover。底部处理 safe-area，采用动态视口高度并实测软键盘，不单靠 100vh。
- 中文输入法合成期间 Enter 不发送；手机默认 Enter 换行、按钮发送，桌面 Web 可提供明确快捷键。
- 长代码块横向滚动不撑宽页面，思考和工具结果折叠，大段内容按需渲染；复制失败提供文本选择回退。
- 历史上拉分页保留滚动锚点；加载、空态、失败、重连、等待电脑确认、正在忙、认证失效都要有独立状态，不靠一个旋转图标代替。
- 模型切换、停止按钮与生成状态来自服务端，不凭前端猜测；断线时禁用发送并保留草稿。
- 键盘可操作、焦点清晰、控件有标签；屏幕阅读器不逐 token 播报，使用节流摘要 / 完成通知，遵从 prefers-reduced-motion。

## 7. 分阶段实施任务

以下路径除“修改”项外均为拟新增；实施前确认实际目录与团队命名。测试先行：每项先写失败测试 → 跑到预期失败 → 最小实现 → 跑绿 → 检查 diff，再进入下一项。不要将整份计划一次性铺开。

### T0 — 固化基线与事件循环验证（第一道闸门）

**文件：**
- 新增 G:/LimboWave/tests/integration/test_web_lifecycle.py。
- 修改 G:/LimboWave/pyproject.toml 和 G:/LimboWave/uv.lock，引入并锁定验证所需的最小 Web 依赖；T3 只补正式配置。
- 新增 G:/LimboWave/docs/architecture/adr-0003-lan-web.md，记录本草案经确认后的决策。

**步骤：**
1. 记录现有工作区改动和测试基线，将既有失败与新增失败分开；配置最小 FastAPI / Uvicorn 测试依赖，不安装到全局 Python。
2. 写最小生命周期测试：在 qasync loop 中启动服务器、请求 health、关闭服务器；同时检查 Qt timer 持续触发。
3. 验证无第二事件循环、无信号处理冲突、端口释放、异常不会静默遗留后台任务。
4. 记录正式 TLS 所需证书配置与一台手机的信任流程；不自动导入系统信任库。

**验收：** 启停与 GUI 响应可行才进入 T1。若不可行，先修订 ADR，不能悄悄另开一个可访问同一运行状态的进程。

### T1 — 共享运行时门面与命令仲裁

**文件：**
- 新增 G:/LimboWave/src/limbowave/application/runtime_facade.py。
- 新增 G:/LimboWave/src/limbowave/application/runtime_commands.py。
- 新增 G:/LimboWave/src/limbowave/runtime_composition.py。
- 修改 G:/LimboWave/src/limbowave/app.py，仅逐步迁移运行装配和有副作用的命令入口。
- 按需修改 G:/LimboWave/src/limbowave/application/services/session_controller.py。
- 新增 G:/LimboWave/tests/unit/test_runtime_facade.py；修改 G:/LimboWave/tests/unit/test_architecture_boundaries.py。

**步骤：**
1. 先测纯查询不切会话、并发发送只接受一次、目标 / revision 不符拒绝、abort 必须匹配 run。
2. 抽出最小门面，保留现有控制器、存储工作进程、权限回调与 kernel 生命周期。
3. 将桌面发送 / 停止 / 新会话 / 实际切换 / 模型切换接入同一仲裁，清查分支 / 重试 / 压缩等会改变运行时的旁路入口。
4. 测桌面原有生成中查历史和重试行为不退化；不为此次计划顺便大规模重写 app.py。

**验收：** domain / application 不依赖 Qt 或 FastAPI；桌面独立运行仍可用。

### T2 — 有序事件、快照与命令回执

**文件：**
- 新增 G:/LimboWave/src/limbowave/application/event_broker.py。
- 新增 G:/LimboWave/src/limbowave/application/command_receipts.py。
- 修改 G:/LimboWave/src/limbowave/application/events.py（仅必要的通用事件，不混入 HTTP 类型）。
- 新增 G:/LimboWave/tests/unit/test_event_broker.py 和 G:/LimboWave/tests/unit/test_command_receipts.py。

**步骤：**
1. 测 seq 单调、重复去重、跨 scope 隔离、缓冲溢出 resync、慢消费者上限。
2. 测“取快照同时产生事件”不会丢消息；测试当前未落库正文的恢复。
3. 测重复命令、不同载荷复用 ID、断线发生在接受前 / 接受后、epoch 改变后拒绝旧命令。
4. 实现有界订阅与回执生命周期，保持现有 ChatEvent 的消费者兼容。

**验收：** 不依赖浏览器也能证明恢复协议成立，无无限内存缓存。

### T3 — Web 安全边界与生命周期

**文件：**
- 新增 G:/LimboWave/src/limbowave/web/__init__.py。
- 新增 G:/LimboWave/src/limbowave/web/server.py、G:/LimboWave/src/limbowave/web/app.py。
- 新增 G:/LimboWave/src/limbowave/web/auth.py、G:/LimboWave/src/limbowave/web/security.py。
- 新增 G:/LimboWave/src/limbowave/application/services/lan_access_service.py。
- 修改 G:/LimboWave/pyproject.toml 和 G:/LimboWave/uv.lock。
- 新增 G:/LimboWave/tests/unit/test_web_auth.py、G:/LimboWave/tests/integration/test_web_security.py。

**步骤：**
1. 先测未认证访问、票据到期 / 重放 / 猜码、撤销、Origin / Host / CSRF、请求限额。
2. 实现正式 HTTPS 配置检查、loopback 开发模式、认证 Cookie、配对窗口和一次性确认流程。
3. 接入受控启停与锁库 / 重置失效钩子；SSE 认证撤销后立即断开。
4. 测错误与日志不会包含票据、Cookie、密钥、请求正文或内部路径。

**验收：** 没有合法配对不能读数据；缺证书不能静默开启明文 LAN。

### T4 — 聊天 API、SSE 与分页历史

**文件：**
- 新增 G:/LimboWave/src/limbowave/web/schemas.py、G:/LimboWave/src/limbowave/web/routes.py。
- 新增 G:/LimboWave/src/limbowave/web/stream.py、G:/LimboWave/src/limbowave/web/dto.py。
- 按需修改 G:/LimboWave/src/limbowave/application/services/history_service.py、G:/LimboWave/src/limbowave/application/repositories.py、G:/LimboWave/src/limbowave/infrastructure/database/sqlite_repositories.py。
- 新增 G:/LimboWave/tests/integration/test_web_api.py、G:/LimboWave/tests/integration/test_web_stream.py。

**步骤：**
1. 用 fake kernel 测建立会话 → 发送 → delta → 最终落库 → 再次查询完整闭环。
2. 按第 4.4 节实现白名单 DTO 和命令合同；branch / conversation 关系在服务端验证。
3. 实现同源 SSE 和同步快照游标，测试断线重连 / 游标过期。
4. 增加大历史、分支祖先分页和最小数据投影测试；确认不返回 secret 或整个配置对象。

**验收：** 两个认证客户端看到相同最终 run 状态；只读请求不会改变电脑执行会话。

### T5 — 移动端 Web MVP

**文件：**
- 新增 G:/LimboWave/web/package.json、G:/LimboWave/web/package-lock.json、G:/LimboWave/web/tsconfig.json、G:/LimboWave/web/vite.config.ts、G:/LimboWave/web/index.html。
- 新增 G:/LimboWave/web/src/main.tsx、G:/LimboWave/web/src/App.tsx、G:/LimboWave/web/src/styles/tokens.css、G:/LimboWave/web/src/styles/layout.css。
- 新增 G:/LimboWave/web/src/api/client.ts、G:/LimboWave/web/src/api/events.ts、G:/LimboWave/web/src/state/chat.ts。
- 新增 G:/LimboWave/web/src/components/PairingPage.tsx、G:/LimboWave/web/src/components/SessionDrawer.tsx、G:/LimboWave/web/src/components/MessageList.tsx、G:/LimboWave/web/src/components/Composer.tsx、G:/LimboWave/web/src/components/ConnectionBanner.tsx。
- 新增 G:/LimboWave/web/src/state/chat.test.ts、G:/LimboWave/web/src/components/Composer.test.tsx、G:/LimboWave/web/tests/mobile-chat.spec.ts。

**步骤：**
1. 先测事件归并与终态、重连 resync、不自动重发、输入法合成 Enter 不发送。
2. 实现配对、会话抽屉、历史消息、流式正文、思考 / 工具折叠、输入与模型选择。
3. 补齐安全 Markdown 渲染与异常状态；前端只使用已定义 DTO，不引用 Python 内部结构。
4. 用真实后端 + fake kernel 做端到端测试，不只做全接口 mock 的静态页面。
5. 检查手机视口、键盘、横屏、长代码块、滚动锚点、慢网络和页面重新进入。

**验收：** 一部手机完成配对、新聊、继续历史、停止和断线恢复；页面无横向溢出。

### T6 — 电脑端入口与远程权限

**文件：**
- 新增 G:/LimboWave/src/limbowave/ui/lan_access_panel.py。
- 修改 G:/LimboWave/src/limbowave/ui/settings_panel.py、G:/LimboWave/src/limbowave/app.py。
- 按需修改 G:/LimboWave/src/limbowave/application/services/permission_service.py、G:/LimboWave/src/limbowave/application/services/tool_gateway.py。
- 新增 G:/LimboWave/tests/ui/test_lan_access_panel.py 和 G:/LimboWave/tests/integration/test_web_permission_boundary.py。

**步骤：**
1. 测默认关闭、开关失败回滚、候选地址、配对确认、设备撤销和密钥不展示。
2. 提供地址 / 二维码、证书说明、端口冲突 / 防火墙提示，不引入公网映射。
3. 将 run_origin 及远程权限上限从命令绑定到整个运行，含自动重试和后续工具调用；不能在切换 UI 时丢失。
4. 测手机不得绕过“全信任”预设、伪造 confirmed、调用内置 IPC 或审批敏感操作。

**验收：** 明确知道是谁连接、服务是否暴露、如何立即切断；手机连接不扩大主机工具权限。

### T7 — 打包、文档和发布闸门

**文件：**
- 修改 G:/LimboWave/scripts/packager.py、G:/LimboWave/tests/unit/test_packager_core.py、G:/LimboWave/.gitignore。
- 按需修改 G:/LimboWave/scripts/verify.ps1，保持现有 PowerShell 编码与兼容要求。
- 新增 G:/LimboWave/docs/development/lan-web.md。
- 新增 G:/LimboWave/tests/integration/test_web_packaged_assets.py。

**步骤：**
1. 约定前端构建输出到 G:/LimboWave/src/limbowave/web/static；源文件与构建产物不混淆，不提交 node_modules。
2. 打包前显式构建静态资源并校验存在；PyInstaller / wheel 包含它们。生产禁止静默落回 Vite 开发服务器。
3. 验证冻结产物中静态资源解析、MIME、API 路由与页面回退边界；不存在的 API 不能返回 index.html。
4. 写明启动 / 关闭、证书信任、网络排障、单执行通道、安全范围和不支持事项。
5. 跑完整回归并分别做 Android Chrome、iOS Safari 和打包程序实机验收；没有设备时明确标记未验证，不把浏览器模拟当成真机。

**验收：** 用户不启动独立前端进程即可使用；正常桌面启动、退出、资料库和导出功能不回归。

## 8. 测试与验收方式

### 建议命令（对应文件实现后执行）

从 G:/LimboWave 运行；本轮仅文档，不执行以下尚不存在的测试：

~~~powershell
uv run pytest G:/LimboWave/tests/unit/test_runtime_facade.py G:/LimboWave/tests/unit/test_event_broker.py G:/LimboWave/tests/unit/test_command_receipts.py -q
uv run pytest G:/LimboWave/tests/unit/test_web_auth.py G:/LimboWave/tests/integration/test_web_lifecycle.py G:/LimboWave/tests/integration/test_web_security.py -q
uv run pytest G:/LimboWave/tests/integration/test_web_api.py G:/LimboWave/tests/integration/test_web_stream.py G:/LimboWave/tests/integration/test_web_permission_boundary.py -q
npm --prefix G:/LimboWave/web ci
npm --prefix G:/LimboWave/web run typecheck
npm --prefix G:/LimboWave/web run test -- --run
npm --prefix G:/LimboWave/web run build
npm --prefix G:/LimboWave/web run test:e2e
powershell -NoProfile -ExecutionPolicy Bypass -File G:/LimboWave/scripts/verify.ps1
git -C G:/LimboWave diff --check
~~~

上述 npm 脚本需在 T5 中定义；首次引入测试应先得到相应合同断言失败，不能把缺依赖 / 收集错误当成有效 TDD 失败。实现后专项测试、类型检查、构建预期退出码均为 0；已有全量失败需单列归因。

### 必过场景

- 手机 + 电脑同发：只有一条运行启动，另一端保留草稿并收到 409。
- 浏览历史不影响另一个端正在执行的会话；停止仅作用于指定 run。
- 请求已接受但响应丢失：查询回执恢复，不重复创建用户消息或调用模型。
- 手机离线 30 秒再回来：结果与电脑一致；buffer 过期也可恢复。
- 服务重启：旧票据 / Cookie / epoch 均无效，旧命令不自动重放。
- 撤销设备、资料库失效：持续连接也不能继续读出内容。
- 跨站请求、伪造 Host、XSS Markdown、猜测附件路径、超限请求、内部工具调用均被拒绝。
- 主机退出 / 端口占用 / 证书无效：明确失败、没有残留监听或数据库未收尾。
- 320 / 390 / 430 / 768 CSS px 布局、软键盘遮挡、中文输入法、长文本和长代码块通过。

### 初始性能目标（需记录设备和网络条件）

- 指定测试资料库与普通家庭 Wi-Fi 下，不含模型提供方耗时，发送接受回执 p95 ≤ 300 ms。
- 首屏最近 50 条消息 API p95 ≤ 500 ms；基准集至少含 1 万条历史消息，不以空库代表性能。
- 已加载页面的服务端 delta 到浏览器可见 p95 ≤ 200 ms；UI 不每 token 全量重排。
- 3 个已连接设备、一个生成任务连续运行 30 分钟，事件队列不越过配置上限，无持续单调内存增长。
- Qt timer 响应在基线与开启 Web 的同负载下对比；不得明显拖慢桌面历史加载和输入。

这些是待测目标，不是当前已达成结果。性能不达标时先定位同步解密、长列表渲染、事件扇出和线程边界，不先增加多进程写入或第二套运行时。

## 9. 后续：真正的无头服务

完成 MVP 后再决定是否实施，不计入首版验收：

1. 复用 RuntimeFacade / runtime_composition，增加 serve 命令；G:/LimboWave/src/limbowave/cli.py 和 G:/LimboWave/src/limbowave/__main__.py 不得在此路径导入 Qt。
2. 主密码通过本机不回显终端或受控 stdin 读取，不支持 URL / argv / 普通配置明文；无交互自启动需另行设计系统凭据保护，不能直接存密码解决。
3. 引入资料库实例所有权锁：GUI 与 headless 不能各起一套 Pi / 协调器共同写同一资料库。后续可让桌面连接后台服务，但这是独立迁移。
4. 无本地审批 UI 时，Agent 权限应使用预先配置的最小允许范围，其他操作默认拒绝；不自动把审批搬到手机。
5. 再评估 OS 服务、休眠恢复、证书续期、PWA 外壳缓存等运维能力；不缓存明文聊天，不增加离线自动发送。

## 10. 推进顺序与决策点

**推荐顺序：T0 → T1 → T2 → T3 → T4 → T5 → T6 → T7。** T6 的权限合同在 T1 时就应设计，不等页面做完才补安全边界。

每阶段交付一个可检查闭环：

- 第一阶段：门面可测试，桌面无回归，生命周期可行。
- 第二阶段：安全 API + SSE，fake kernel 多端闭环。
- 第三阶段：手机真实可用，电脑端可管理连接。
- 第四阶段：真机、打包与安全闸门通过，才称首版完成。

最大不确定性是共享运行时从 app.py 的提取、多端状态一致性，以及手机 HTTPS 证书信任；不是静态页面本身。先消除这三类风险，再扩展附件与高级功能。

**待用户确认的核心边界：首版接受“电脑端保持运行并已解锁”，还是必须从第一版就能纯后台独立运行。** 其余内容可按本草案默认值推进。


## 实施后用户确认的边界调整（2026-10-06）

用户在遇到 TLS required 报错后明确要求“允许不配置 HTTPS 证书和私钥，强制手机端核验数据库密码”。实现据此新增明确 opt-in 的 HTTP 模式，未自动降级 HTTPS。配对与资料库密码核验是两个连续门槛；核验前禁止读取历史、订阅事件或执行命令。HTTP 风险与密码加密信封的主动攻击限制见 docs/development/lan-web.md。该调整覆盖本草案要求 LAN 必须 TLS 的部分，不扩大主机工具权限。
