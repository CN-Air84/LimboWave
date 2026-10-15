# 项目地图与维护改进路线

> 2026-10-14，基于当前工作树的结构巡检与渐进式改进。开始时已有一批未提交修复；本轮保留它们，不做全量格式化，不自动提交，也不把既有修改计为本轮成果。

## 1. 项目轮廓

这是共用同一业务内核的桌面 + 独立服务端 + Web 客户端项目，不宜拆成互相复制规则的三个应用。

```text
桌面：__main__ → app.py / composition.py → ui（Qt 展示与交互）
服务端：server.__main__ → lifecycle / composition → web（HTTP、鉴权、SSE）
网页：web/src/App.tsx → state/controller.ts → api（命令、回执、事件）
                        ↓
application：SessionController / RunCoordinator / 各业务服务 / 仓库端口
                        ↓
domain：会话、运行、路由、文件、快照、权限等规则与不可变数据

组合根向 application 注入 infrastructure 的实现：
  SQLite 工作单元、加密存储、配置仓库、Pi 适配器、存储子进程
```

这里的箭头表达调用与组合关系；**依赖规则**是 domain 不依赖外层，application 依赖 domain 和自身端口，基础设施实现这些端口。组合根可以了解具体实现。

### 常用入口

| 任务 | 先看哪里 | 重点验证 |
| --- | --- | --- |
| 会话发送、重试、结束、恢复 | `application/services/session_controller.py`、`run_coordinator.py`、`runtime_recovery_coordinator.py` | 单元 + runtime/恢复集成测试；不要自动重发中断输入 |
| 历史与持久化 | `application/repositories.py`、history 系列服务、`infrastructure/database/` | 内存/SQLite 语义一致、事务边界、分页 |
| 文件、图片、剪贴板、导出 | `application/blob_storage.py`、对应服务、`domain/files.py` | 存储替换、真实加密读写、稳定行号、导出内容和计数 |
| Pi 接入与跨进程调用 | `composition.py`、`infrastructure/pi_adapter.py`、`pi_rpc.py`、`conversation_process.py` | 协议契约、子进程退出、结果落库 |
| 桌面交互 | `app.py`、`ui/main_window.py`、`ui/chat_view.py` | Qt UI 回归、跨会话切换、后台任务生命周期 |
| 独立服务与 Web API | `server/lifecycle.py`、`server/composition.py`、`web/app.py`、`runtime_facade.py` | locked/unlocked 生命周期、命令回执、SSE、权限边界 |
| 网页行为 | `web/src/state/controller.ts`、`web/src/state/chat.ts`、`web/src/api/` | 类型检查、Vitest、真实协议 E2E；不自动重试变更命令 |
| 开发与交付 | `pyproject.toml`、`scripts/`、`.github/workflows/linux.yml`、`web/package.json` | Windows/Linux 质量门是否一致、冻结打包入口 |

测试按 `tests/unit`、`tests/integration`、`tests/ui` 分层，网页侧独立维护类型检查、组件/状态与端到端测试。

## 2. 本轮已经改进

### 2.1 存储依赖不再“名义注入、实际绑死”

5 个附件相关服务现在依赖应用层端口：文件/导出只需 get；图片/剪贴板需要读写删；回收只需 prune。
去掉 FileService 的 object 参数和具体类断言。生产加密实现不变；新增测试替身无需密钥和磁盘，即可覆盖真实服务行为。

### 2.2 共享规则不再藏在其他服务的私有函数中

行偏移算法移动至 `domain.files.compute_line_offsets`，文件和剪贴板共享公开入口；重新索引不再对同一内容重复扫描两遍。
增加空文件、CR/LF/CRLF、混合换行、末尾换行、中文字节偏移以及非行界控制字符测试。

### 2.3 导出渲染与计数使用同一份结果

删除“读取图片用于渲染、再用 exists 重新统计”的重复路径。损坏或缺失图片继续跳过，但不会错误计入成功数量。
只读替身可以导出图片；每张图只调用一次 get，用户与助手消息不会重复嵌入同一轮附件。

### 2.4 分层从口头约定变成自动检查

架构测试检查全部 domain/application 模块，识别相对、包级、嵌套与 TYPE_CHECKING 导入。
反向依赖只能使用按文件、具体模块和理由登记的存量例外；无用例外也会使测试失败。
本轮移除了 5 个服务的基础设施直接依赖，仍保留 5 个有不同生命周期/格式责任的存量例外文件，没有假装边界已经完全清理。

决策与失败模式见 [ADR-0003](../architecture/adr-0003-application-storage-boundaries.md)。

## 3. 剩余维护热点：按优先级渐进式处理

### P1：桌面组合根承载过多业务交互

读取时 `app.py::_wire_impl` 单个函数约 3,597 行，包含设置、历史、事件分发、恢复、权限等大量闭包。
问题不只是文件长，而是闭包共享状态与关闭顺序使功能无法独立装配和验证。

**下一步**：先提取设置/配置接线与历史导航为独立对象，显式传入依赖和可释放的订阅；每次只移动一个职责，保留 startup、shutdown、恢复与跨会话切换测试。
不要先造一个万能 ApplicationManager 再把同一批状态搬进去，也不要一次性重写退出流程。

### P1：存储子进程协议依赖协调器私有实现

`infrastructure/conversation_process.py::_execute` 用私有方法名白名单、位置参数与 getattr 调用 RunCoordinator，并写 `_active_conversation_id` / `_active_branch_id`。
其中 `_persist_run_start` 还约定从 `args[4]` 取上下文。这些隐式协议无法由现有 Any/字符串类型检查兜底，重命名或调整参数容易只在子进程里出错。

**下一步**：给存储操作定义具名请求/结果与显式调度函数；让主进程/子进程共同使用同一份契约，再从 RunCoordinator 提取纯存储操作。先覆盖序列化、错误返回和提交一致性，不引入第二套运行状态机。

### P2：RunCoordinator 的职责仍然拥挤

运行协调器同时处理命令范围、活动目标、内核事件、重试和最终落库。已有 recovery、send_queue、history 等拆分可沿用，不应重建一套平行抽象。

**下一步**：优先跟随上述存储契约拆分，保留“一个权威运行状态、一个提交边界”；最后才评估事件归一化/重试策略是否需要独立对象。

### P2：开发质量门存在平台差异

Windows 的 `scripts/lint.ps1` 执行仓库级 ruff check、format --check 和 mypy；Linux 脚本/CI 只检查 src/tests 的 ruff check 与 mypy，没有对应格式门。
网页检查又在单独的 npm 脚本中。相同修改可能本地过、另一平台不过，或只在某个平台遗漏文件。

**下一步**：统一检查范围与锁文件策略后，让 Windows/Linux/CI 调用同一质量入口；先单独清理已有静态问题，再启用严格门，不能通过大面积忽略规则来“变绿”。
本轮不混入全库格式化，也不声称补齐 Windows CI 或真机覆盖。

### P2：无界面服务仍随基础依赖安装 Qt

虽然 `server/composition.py` 不导入桌面入口，但 `pyproject.toml` 把 PySide6/qasync 作为无条件基础依赖。
这会让仅部署服务端也承担桌面依赖的安装与版本维护成本。

**下一步**：评估 desktop 可选依赖组与服务端安装入口，先验证打包、默认开发环境和无 Qt 服务端启动，再改变默认安装方式。

### P2：网页控制器与桌面状态机需要继续保持分工

`web/src/state/controller.ts` 集中管理鉴权、连接、选择目标、分页、草稿及待确认回执；当前工作树已有该文件修复。
拆分时必须保留 generation/selection 过期响应屏障与“结果不明确不自动重发”的约束，不能仅按文件长度拆开。
本轮没有修改网页逻辑；后续适合将连接/重连与回执确认分别提取并注入测试时钟。

## 4. 验证记录

- 修改前附件服务与原架构检查：**173 passed**。
- 新回归在旧实现确认失败：文件读取替换 3 项、导出 2 项；新增方向检查报出预期 5 个服务。
- 修改后局部服务、行索引、架构检查：**309 passed**。
- 仓库级 Ruff lint：通过。
- 全部 Python 源文件严格 mypy：**216 source files，零错误**。
- 附件/分层与服务端跨边界定向 Python 回归：**660 passed, 1 skipped**；服务端组合、记忆运行时、IPC、Web memory 与解锁事务追加回归：**128 passed**；新增服务端记忆授权测试：**3 passed**。
- Web typecheck 通过，Vitest：**67 passed**；Playwright 真实链路：**9 passed**；生产构建通过。
- 完整 pytest 未作为全量绿灯声明：默认 Windows 临时目录权限不可用；改用工作区临时目录后，Qt offscreen 的 `test_checkbox_style.py` 仍受 PySide6 缺失字体目录影响失败，另有部分悬停/像素断言存在平台敏感性。上述定向回归仍通过；真实 Linux VPS、跨进程 Unix socket、真实 provider、手机键盘与真机交互仍未验证。

本轮没有修改生产数据、加密格式或数据库版本；新增的服务端组合与回环工具 IPC 改动已由对应回归覆盖。
