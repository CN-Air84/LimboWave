# 运行诊断日志

`limbowave.infrastructure.diagnostics` 是独立的通用调试日志核心，区别于
`RequestLogService` 的请求/传输快照。导入没有副作用，不创建线程、目录或全局 handler；
核心不依赖 Qt，不联网、不调用 AI，也不引入额外依赖。

**已接入 GUI 主程序。** 启动时创建唯一日志写者，接收运行生命周期、后台异常、Pi 诊断与
Python/线程/asyncio/warnings/Qt 消息；业务资源退出后恢复钩子并关闭写者。
`secret/vault` CLI 的标准输入/输出协议保持原样，没有全局 stdout/stderr 重定向。

## 在程序里使用

- 打开「设置」，点击顶部的 **诊断日志**；也可随时按 **Ctrl+Shift+L**，登录页面和资料库
  未解锁时也能打开。非模态工具窗重复打开会复用，关闭它不退出应用。
- **收集等级（本次运行）**控制哪些事件进入日志，默认 INFO；改成 DEBUG 后可看到 RPC
  命令元数据等细节。**筛选最低等级**只过滤已经收集的记录，不能找回此前未收集的 DEBUG。
- 窗口顶部显示实际文件路径，可选择复制。默认位置由 `AppPaths.log_root` / platformdirs
  决定，活动文件为 `diagnostics.jsonl`，备份为 `.jsonl.1` 到 `.jsonl.4`。
- 收集等级只影响本次运行，不写入偏好。启动时也可指定：

```text
uv run python -m limbowave --log-level DEBUG
uv run python -m limbowave --log-level INFO --log-console
uv run python -m limbowave --log-dir "D:\LimboWave-logs"
```

`--log-level` 接受五级名称及 CRIT，大小写不敏感；`--log-dir` 指向日志目录。
这些是 GUI 参数，不能加在 `secret/vault` 子命令之后。

已有其他进程占用主日志目标时，新进程自动改写同目录下带进程号的独立文件（例如
`diagnostics-1234.jsonl`），诊断窗口会显示实际路径。目录不可用，或主目标与进程专属
目标都不可用时，才降级为**仅内存记录**，主窗口状态栏、诊断窗口和 stderr 都会提示；
不会换临时目录持续创建新文件。仅内存窗口退出即丢失，修复目录权限/关闭占用进程后需
重启恢复文件记录。启动后发生的文件故障仍按 5 秒退避恢复。

## 最小使用方式

```python
from limbowave.bootstrap import create_context
from limbowave.infrastructure.diagnostics import LogConfig, LogManager, log_context

config = LogConfig(directory=create_context().paths.log_root)
with LogManager(config) as diagnostics:
    log = diagnostics.get_logger("limbowave.example", component="example")
    log.debug("仅在 DEBUG 开启时记录：%s", "detail")
    log.info("初始化完成")
    with log_context(conversation_id="c1", run_id="r1"):
        log.bind(operation="probe").warning("耗时偏高", extra={"elapsed_ms": 820})
        try:
            raise ValueError("示例错误")
        except ValueError:
            log.exception("探测失败")
    log.crit("示例严重事件")  # 与 critical 同级
```

五级为 `DEBUG / INFO / WARNING / ERROR / CRITICAL`，`crit` 是别名。
`LogLevel.parse()` 接受枚举、这些名称（大小写不敏感）及 10/20/30/40/50。
第三方标准 logging 的自定义数字等级归入最近的下档，最高封顶 CRITICAL。

- `get_logger(name, **fields)` 返回兼容 logging 的适配器，支持 `%` 懒格式化、
  `exception()`、`exc_info`、`stack_info`、`stacklevel`、`extra` 与 `bind()`。
- `set_level("debug")` 动态修改独立管理器阈值及它自己创建的 logger；GUI 集成使用
  `DiagnosticRuntime.set_level()` 同步修改 `limbowave` 标准 logging 命名空间的阈值。
- 使用固定模块名作为 logger 名，不要为每个 run 创建新名字；动态 ID 放上下文。
- 字段优先级：`log_context` < logger 绑定 < 单次调用的 `extra`。
- `log_context` 使用 ContextVar，嵌套退出自动还原，asyncio 任务隔离；新线程不自动
  继承，需显式重新绑定或使用 `contextvars.copy_context()`。
- 上下文在调用时快照，事后修改原始字典不会改变已提交记录。
- 来源含文件/行号/函数、UTC 时间、线程、进程与每次启动唯一的 `session_id`；
  `sequence` 仅在一个管理器实例内递增，跨启动用 `(session_id, sequence)` 标识。

### 标准 logging 的显式接线

```python
import logging

with diagnostics.attach(logging.getLogger("some_library"), level="debug"):
    # 此 logger 及正常向它传播的子 logger 会被接收。
    # 仍须满足其自身阈值与 diagnostics 的阈值。
    ...
```

也可手动 `logger.addHandler(diagnostics.handler)`，结束时自行移除。
`attach()` 保留原 handler，退出时撤销自身添加，并恢复它显式修改的等级。
它不会禁用传播、清除第三方 handler 或改变 root。**不要把同一个 handler 同时装在
父子两级传播链上，否则标准 logging 会重复投递。** 其他 handler 的输出不受本组件脱敏保护。

## 默认资源预算

| 项目 | 默认 | 作用 |
| --- | --- | --- |
| 收集阈值 | INFO | 关闭无用 DEBUG 负担 |
| 待写队列 | 2048 条 | 防止磁盘阻塞造成无界内存增长 |
| 近期内存窗口 | 1000 条 | 供分析面板读取，不扫描磁盘 |
| 批量大小 | 64 条 | 一批合并写入，轮转处拆分 |
| 最长批量等待 | 0.5 秒 | 低流量也能定时输出 |
| 单条 JSONL | 16 KiB（含换行） | 防止异常/大对象灌爆日志 |
| 单文件 | 5 MiB | 按 UTF-8 字节数而非字符数轮转 |
| 备份 | 4 个 | 同一配置生成的活动文件 + 备份最多约 25 MiB |
| 写盘速率 | 64 KiB/s | 令牌桶，突发容量为速率与单条上限的较大值 |
| 文件故障重试 | 5 秒 | 退避期间丢弃磁盘副本，不忙循环重试 |
| 控制台 | 关闭 | 避免终端渲染开销 |

设置 `disk_bytes_per_second=0` 可关闭速率限制，适合短时完整诊断；默认不是无损审计器。
速率限制是**写入预算**，文件轮转是**保留空间预算**，两者不是一回事。
长期持续打满 64 KiB/s 仍可产生约 5.3 GiB/天写入，需要更低预算可继续调小该配置。
单条记录超过预算时会截断详情，保留合法 JSON 并置 `truncated=true`。

生产线程不做日志文件 I/O，但仍需完成 logging 消息格式化、有界字段快照、脱敏和编码。
未知上下文对象只记录类型，不调用其 repr，不扫描属性；循环/深层/超量结构会截断。
异常保存有限调用栈与因果链，不抓局部变量、不读取源码，也不持有 traceback 引用。
调用者自己的 `__str__`、f-string 或 `stack_info=True` 所产生的成本不在组件控制范围内。

后台仅一个写者，空闲时等待条件变量，不定时空写；合并写入后交由 OS 页缓存，不逐条
`fsync`。窗口/队列上限按记录数计算，每条也有字节上限；包含 Python 对象开销，不应
把两者相乘当作精确 RSS 上限。控制台输出也在写者线程，阻塞的终端不会阻塞生产线程，
但会占住写者，导致有界队列按策略丢弃。

## 过载、故障与可见性

- 队列满：优先淘汰最低等级最早的记录，给更高等级让位；同级或更低级的新记录丢弃。
  **全是 ERROR/CRITICAL 时仍可能丢高等级，不保证关键事件无损。** 同级洪峰拒绝为 O(1)。
- 磁盘限流：一批内先给高等级分配预算，最终仍按原始序号写盘；不同批次之间不保证
  高等级保留配额，CRITICAL 同样受限。
- 文件错误：运行中的错误不抛给业务线程；记录留在近期窗口，磁盘副本丢弃并计数。
  退避后有新记录时尝试恢复，不重放旧记录，避免重复写入和无限缓存。
- 控制台错误独立计数，不妨碍同一批的文件保存。
- 字符串格式化失败产生安全占位消息及 `format_errors`，不回显未脱敏的原始对象。
- 独立 `LogManager` 启动时目录/锁失败仍会抛出；`DiagnosticRuntime` 接线层捕获这些失败，
  改用 `file_enabled=False` 的仅内存管理器，明确提示后继续启动。

`status()` 返回不可变状态快照（其中字典为副本）：

- `state`：new / running / closing / closed / failed；`sequence`：近期窗口版本。
- `queued`：累计成功入队次数（含后来被挤出的）；`pending`：队列 + 当前后台批次。
- `written / bytes_written / write_batches`：本实例已完成文件写入的条数/字节数/写调用数。
- `queue_dropped / rate_limited / file_dropped`：三种磁盘记录损失计数，互不重复。
- `dropped_by_level`：按五级累计上述损失。
- `rejected`：启动前或关闭后投递；`format_errors`：格式化/序列化故障。
- `console_errors`：控制台失败的批次数。
- `file_skipped`：仅内存模式中已处理但未启用文件保存的记录数，与文件故障/限流分开计数。
- `last_file_error / last_console_error`：脱敏后的最近失败信息；成功恢复时清空。

近期窗口包含通过收集阈值的记录，**包括未落盘和因过载被丢弃的记录**。它不是文件的
镜像，更不是完整历史。应一起查看状态计数，不能只凭窗口中“看到日志”判断落盘成功。

## 生命周期与文件边界

- `start()` 显式启动，可重复调用；`close(timeout=5)` 停止收取、排空并释放目标锁。
- `flush(timeout=5)` 等待调用前已入队记录处理完毕。返回 True 表示处理完成，**不代表
  没有丢弃、未发生 I/O 故障或已物理持久化**。检查 `status()` 判断完整性。
- `close()` 超时返回 False，状态保持 closing，线程和锁仍然有效；稍后可再次等待。
  上下文管理器退出超时时抛 TimeoutError，不谎报关闭成功。
- 关闭后的管理器不可重启，创建新实例。未显式关闭时，daemon 线程不保证保存最后一批；
  进程崩溃/断电还可能丢失 OS 缓存。不要把它当审计、财务或事务提交记录。
- 对 `<directory>/<name>` 持有 OS 文件锁；独立 `LogManager` 的同名同目录第二个写者抛
  `LogTargetInUseError`。GUI 的 `DiagnosticRuntime` 捕获冲突并改用 `<name>-<pid>`，
  让并行应用实例分别落盘；实例/线程不可跨 fork 复用。
- 命名空间为 `diagnostics.lock`、`diagnostics.jsonl`、`diagnostics.jsonl.1` … `.4`；
  `.1` 最新，最大编号最旧。锁文件保留，不靠删除锁文件判断进程存活。
- 不清空目录，不触碰其他名称文件。保留参数调小前既有更大文件或超出新编号的旧备份
  不保证立即符合新空间预算；部署/迁移应单独安排清理，避免意外删历史。
- 异常退出留下半行时，下次写者补换行隔离损坏行，不把后续完整记录粘到坏行。

## 本地分析 API

```python
from datetime import UTC, datetime
from limbowave.infrastructure.diagnostics import LogAnalyzer, LogQuery, LogReader

query = LogQuery(
    min_level="warning", logger="limbowave.pi", text="timeout",
    since=datetime(2026, 9, 29, tzinfo=UTC), context={"run_id": "r1"},
)
recent = diagnostics.recent(query, limit=100, after_sequence=0)  # 最近匹配项，按时间正序
reader = LogReader(config)
summary = LogAnalyzer.summarize(reader.iter_entries(query), max_groups=200)
print(summary.by_level, summary.repeated_problems)
print(reader.stats)  # 坏行、超长行、不可读文件等
```

`logger` 按模块前缀匹配（`pi` 匹配 `pi.rpc`，不匹配 `pixel`），文本搜索不分大小写；
`levels=frozenset(...)` 可进一步选精确等级，空集合不匹配任何项。时间范围含端点，必须
带时区。上下文是顶层字段精确比较；文本、异常和上下文 JSON 也会参与关键词搜索。

Reader 按备份到活动文件流式读取，每行有大小上限；损坏/未知版本/半行跳过并计数。
`iter_entries(limit=N)` 返回从最旧开始的前 N 条匹配项，不是最新 N 条。
一次 reader 只运行一个迭代器，`stats` 在迭代开始时重置、迭代中累积；并发扫描请新建 reader。
在线写入/轮转时不承诺一致性快照，正式完整分析应在写者关闭后读取；GUI 不同步扫描文件。

Analyzer 提供五级、来源、异常类型分布和精确重复警告/错误。分组数有上限，来源/异常溢出
归入 `[other groups]`，额外问题组不保留，并设置 `groups_truncated`。它辅助定位，不推断
未经证实的根因；统计范围仅是传入的记录。

## 可嵌入 Qt 面板

```python
from limbowave.ui.diagnostics_panel import DiagnosticsPanel

panel = DiagnosticsPanel(diagnostics, parent=some_widget, display_limit=500)
layout.addWidget(panel)
```

面板具有最低等级、来源和关键词筛选、暂停/继续、只读表格、完整结构化详情、重复问题与
异常统计、文件故障/丢弃计数。500ms 刷新，无新记录不重建表格；隐藏或暂停时停止轮询，
搜索有 150ms 防抖，最多展示指定行数。暂停时筛选仅作用于冻结的内存窗口。
面板不调用 start/close，不安装 handler；由宿主管理器统一管理生命周期。GUI 通过
`DiagnosticsWindow` 懒创建面板，并传入 `DiagnosticRuntime.set_level` 处理收集等级变更。
没有后台历史扫描、日志上传或隐式导出。

## 脱敏边界

复用 `domain/redaction.py` 的凭据字段/请求头名称与文本模式，补充常见赋值与 URL userinfo
处理。文件、内存窗口、控制台共享同一份脱敏记录；文件读取时再做一次防御性脱敏。
未知格式、业务隐私、聊天内容以及任意格式的自由文本不可能自动识别干净：不要把完整
请求体、主密码、原始密钥、图片或模型流式 token 当调试字段写入。明文日志属于本地诊断
数据，不属于 vault 加密资料库，故障反馈前仍应人工检查。

## 接入边界与继续扩展

- `app.main()` 持有 `DiagnosticRuntime` 与 Qt 桥接，统一覆盖正常退出、smoke、启动异常和
  数据重置退出。`_wire()` 中途失败会回收已打开的数据库与工具 IPC；退出时先停止业务
  生产者，最后关闭日志。日志写者关闭超时会明确提示，不谎报完成。
- `DiagnosticRuntime` 每进程只允许安装一次，可幂等 start。它接入 `limbowave` logger
  命名空间，不清空 root、不接管所有第三方 HTTP 调试输出。各业务模块只需使用
  `logging.getLogger(__name__)`，运行元数据通过 `extra` 传递；不需要依赖 Qt 或日志存储层。
- `RunCoordinator` 在统一事件出口记录开始/错误/重试/分支/工具阶段，最终提交后记录
  `run.finalized`；携带 conversation_id / branch_id / run_id，但不记录正文、思考、附件
  或逐 token 更新。跨阶段日志通过这些 ID 关联，而不是把整个事件载荷写入。
- Pi 普通 stderr 脱敏、限长后记录；`[LIMBOWAVE]` 帧仍完整交给原请求观测通道，仅对白名单
  事件记录类型/状态等元数据摘要。RPC stdout 不复制到调试日志，RPC 命令也只记录类型与 ID。
  既有 stderr 调试缓存限制为最多 256 Ki 字符、1024 帧，不截断交付给观测回调的原始帧。
- Python 主线程/后台线程/unraisable 异常、warnings 与 asyncio 异常有显式桥接。不会把
  task/future/object 的 repr 或整个 asyncio 错误上下文写入文件。Qt Fatal 尽力刷新最多
  250ms，但不改变 Qt 的终止行为，也不保证崩溃时磁盘物理持久化。
- 原有 Python 异常/warnings 与 Qt 消息处理器会继续收到回调，退出恢复先前钩子。控制台
  开关只控制本诊断器的输出副本；旧处理器、CLI 标准流的输出仍遵守原来的行为和脱敏约定。
- 新模块按需要增加低频生命周期或故障日志，不机械地对每个函数埋点；处理任意输入前仍应
  检查敏感字段。`log_context` 可用于局部 asyncio 流程，跨线程必须显式传递。

## 验证

```text
uv run python -m pytest tests/unit/test_diagnostics.py tests/unit/test_diagnostics_analysis.py tests/unit/test_diagnostics_edges.py
uv run --env-file .var/offscreen.env python -m pytest tests/ui/test_diagnostics_panel.py
uv run ruff check src/limbowave/infrastructure/diagnostics src/limbowave/ui/diagnostics_panel.py tests/unit/test_diagnostics*.py tests/ui/test_diagnostics_panel.py
uv run mypy src/limbowave/infrastructure/diagnostics src/limbowave/ui/diagnostics_panel.py
```

覆盖线程并发、级别/上下文、脱敏、坏对象、字节上限、轮转、队列优先级、限流、磁盘失败/
退避恢复、关闭超时、文件互斥、坏行回读及面板过滤/冻结/隐藏行为。

### 本机验证记录（2026-09-29）

Windows / Python 3.12 环境：新增核心与分析器测试 80 项、面板测试 5 项全部通过；
连同原请求日志面板的 UI 回归共 9 项通过。新增源码通过 ruff 与 strict mypy。
完整单元回归第一次遇到已有 DNS 环境问题：
`test_tool_gateway.py::test_read_url_truncates_output` 因 `example.com` 解析到
`198.18.0.10` 被网络防护拒绝。排除该项后为 **1033 passed / 1 skipped / 1 deselected**；
跳过项是当前环境不支持创建符号链接，并未修改这些已有测试或网络防护。

可复现压力探针：

```text
uv run python scripts/diagnostics_benchmark.py --records 20000
```

脚本只使用临时目录及合成日志，退出自动清理；默认洪峰与无损写入分开测量。
以下只是这台机器的一次观测，不是跨机器性能承诺：

| 场景 | 观测 |
| --- | --- |
| 关闭 DEBUG，调用 2 万次 | 约 4.3ms，无文件写调用 |
| 5000 条 INFO，关闭限流且放大队列 | 约 0.395s 全部处理，79 次合并写，无丢失 |
| 默认配置，2 万条 INFO 洪峰 | 约 1.56s，实际日志载荷写入 166708 字节，388 条落盘 |
| 上述默认洪峰损失 | 队列拒绝 3036 条、磁盘限流 16576 条，均按 INFO 计数 |
| 单独启用 tracemalloc，1 万条 INFO | 追踪到的峰值约 683 KiB；非 RSS，不用于吞吐对比 |

INFO 测试的生产端约 1.29 万调用/秒，前 1000 次调用的 p95 约 126 微秒，包含快照与
脱敏成本。磁盘字节统计是日志载荷，不包括文件系统元数据和半行修复分隔符；实际硬盘
物理写入还取决于 OS 缓存。洪峰保护是有损的，不能拿减少后的写盘量冒充完整保存。
