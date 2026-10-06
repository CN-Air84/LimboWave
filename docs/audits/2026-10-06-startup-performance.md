# 启动与交互性能优化验收记录

日期：2026-10-06。范围：当前工作树的桌面启动、可选局域网服务、历史附件查询、手机端首屏与消息渲染。

## 已完成的优化

1. **可选服务按需创建**：正常启动不再导入 FastAPI/Uvicorn，不创建远程事件订阅、LAN 面板或枚举网卡。用户开启服务时，在后台导入 Qt 无关依赖，在原有 Qt/asyncio 线程创建服务。停止服务释放订阅，隐藏面板停止刷新定时器；配对过期、撤销、重置和关闭仍有效。
2. **缩短就绪关键路径**：设置页不再在启动时预热；存储进程和进程池在第一次实际请求时启动，不为未打开的会话空转预热。后续复用同一实例，关闭仍等待已提交的事务完成。
3. **按需加载解析资源**：ASCII 模型名排序/站点标识不加载拼音词典；排序结果采用最多 2048 项的缓存。空白消息不加载 Markdown 解析器，普通 Markdown 不加载语法高亮器。不缓存私密消息正文。
4. **缩小历史读取范围**：附件引用使用当前会话、选定消息的元数据投影，复用已有会话/运行索引，不读取与解密其他会话的请求参数。每批最多 500 个消息 ID；保留继承消息、重试与最新意图清空附件的语义。无需数据库迁移。
5. **手机端减少首屏与流式开销**：聊天列表与密码加密代码按需加载；显式隔离 React/CommonJS 公共运行时，避免分包配置反向拉入可选代码。未变化的消息/Markdown 复用渲染结果，折叠的思考过程不解析，展开时显示最新内容。保留滚动位置、链接过滤与远程图片拦截。

未降低 Argon2 参数、未改变加密/授权规则、未删减动效或修改视觉设计。保留工作区原有修改；期间另有会话流/IPC 性能改动同步进入工作树，这些改动未由本轮覆盖或撤销。

## 桌面复测

同一隔离脚本、每组 3 个新进程，表内为中位数。原始数据保存在 `.var/performance-before.json` 与 `.var/performance-after.json`。

| 指标 | 优化前 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| 新进程导入应用模块 | 916.91 ms | 662.90 ms | -27.7% |
| Qt 主窗口构建 | 175.80 ms | 200.78 ms | +14.2% |
| 解锁后服务装配 `_wire` | 486.55 ms | 93.68 ms | -80.7% |
| 启动收尾（原先等待存储进程预热） | 686.08 ms | 12.11 ms | -98.2% |
| 登录窗口主进程工作集 | 149.59 MiB | 98.05 MiB | -34.5% |
| 装配后主进程工作集，不含设置页 | 175.18 MiB | 102.71 MiB | -41.4% |
| 首次构造设置页 | 399.55 ms | 481.36 ms | +20.5% |
| 构造设置页后的主进程工作集 | 193.38 MiB | 126.37 MiB | -34.7% |
| 装配期间 Qt 心跳最大间隔 | 397.34 ms | 30.61 ms | -92.3% |

**测量边界与取舍：**

- 使用 offscreen Qt、临时数据库、测试专用低开销 KDF、替身 shell 探测/模型配置；没有访问真实资料库或启动真实模型。不是操作系统清缓存后的冷启动，也不包含用户输入密码、生产 KDF、真实壁纸、显示器刷新、模型握手和全部真实数据量。
- 内存是主进程当前工作集，不是进程树总内存，也不是启动峰值；心跳间隔不是显示帧率。工作树中同步出现的聊天 UI 改动与系统负载也会影响小幅时序差异。
- 启动收尾提速主要来自**不再等待未使用进程**，不是让同一进程创建速度提高了 98%。首次打开历史/发送消息仍有一次后台进程初始化成本。
- 设置页仍在 GUI 线程创建并复用；首次打开约 0.48 秒，本次测量比原先预热阶段约 0.40 秒慢。该成本已移出启动路径，但没有被消除。Qt 窗口构建本身本次约慢 25 ms，未宣称所有阶段都变快。
- 启动装配后的控件从 145 个降至 110 个；设置完整打开后仍为 720 个，说明保留了功能，而非删减控件实现提速。

正式可重复脚本 `scripts/performance_benchmark.py` 的另一次 5 进程复测：装配中位数 93.47 ms，装配后工作集 102.66 MiB；正常启动未导入 `fastapi`、`pypinyin`、`markdown_it`。完整样本在 `.var/performance-final.json`。

## 历史附件查询

在**同一个**临时 SQLite 数据库上对比旧查询和新投影，各重复 3 次，校验结果完全一致。数据：50 个会话、2000 条请求意图、每条 4 KiB 合成参数，选中会话的 40 条消息。

- 旧方式：读取并解密全部请求意图，中位数 243.84 ms。
- 新方式：只读选定会话的附件引用，中位数 1.90 ms。
- 这是附件查询子步骤，不是完整历史窗口加载时间；模型回复、Markdown 排版、图片解码等仍有自己的成本。

## 手机端产物

使用同一 Vite 生产构建口径，首屏入口与静态依赖的 JS 合计，不含 CSS/SVG：

| | 优化前 | 优化后 |
| --- | ---: | ---: |
| 原始 JS | 802.35 kB | 354.74 kB |
| gzip 估算 | 235.11 kB | 108.43 kB |

Markdown 与密码加密包仍保留，只是不在配对/密码页面首屏下载。Playwright 实际请求断言验证：初始页面无这些包；提交密码时才获取加密包；进入会话时才获取聊天与 Markdown 包。构建输出保留原有第三方 Zod 注释警告，不影响成功构建。

## 已验证

- 250 个集成测试通过（有 2 条现有 Windows 异步管道释放警告）。
- 49 个前端单元测试通过；生产构建与 TypeScript 检查通过。
- 最终 8 个真实 Chromium 端到端测试全部通过，覆盖配对、密码信封、错误密码、无安全随机源时拒绝、SSE、历史、停止生成、手机宽度、输入法和延迟加载。人工查看了生成的手机聊天截图，未发现布局回归。
- 全项目 mypy：189 个源文件通过。
- 本轮修改文件 Ruff 通过；`git diff --check` 通过。全库 Ruff 另有 5 项非本轮范围的问题，位于复选框绘制、主题、已有启动清理测试和同步新增的 IPC 测试；未做无关全库格式化。
- 新增与直接相关的核心测试单次 84 项通过；历史/存储/重新生成组合测试 50 项通过。最终较大范围的独立单元/UI 回归结果见下方补充。

**全量回归说明：** 开始优化前，unit+UI 基线为 2590 passed、9 failed、7 skipped、1 error；失败主要在 offscreen 像素/字体、设置列表及日志锁定预期。不能将当前项目描述为“全量测试全部通过”。一次中间全量运行还遇到同步写入、尚未配套完成的会话流/IPC 测试失败；后续重新验证当前文件。再次全量运行只保留部分日志，未取得完成结果，未将不完整日志当作通过结果。

## 复现命令（项目根目录）

```powershell
.\.venv\Scripts\python.exe scripts/performance_benchmark.py --samples 5 --include-settings --history-intents 2000 --output .var/performance.json
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest tests/unit/test_history_attachments.py tests/unit/test_performance_benchmark.py tests/ui/test_startup_responsiveness.py tests/ui/test_lan_controller.py -q
.\.venv\Scripts\mypy.exe src
cd web
npm test
npm run build
npm run test:e2e
```

基准只使用临时数据，不需要真实主密码、外部模型、API 密钥或局域网监听。`--history-intents` 可调整合成数据库规模。

## 最终独立回归结果

- 全部单元测试：**1556 passed、6 skipped**（76.62 秒）；跳过项为当前平台不支持的 POSIX/符号链接场景。
- 18 个相关 UI 测试文件：**280 passed、1 failed**（210 秒）。启动、懒加载、LAN 生命周期、历史、重新生成、会话流/分页、Markdown、设置页和数据重置均纳入本次组合验证。
- 唯一组合失败：`test_settings_dialog.py::test_binding_thinking_controls_share_compact_row`，offscreen 下控件顶部相差 4 px，而断言要求不超过 2 px。随即将整个 `test_settings_dialog.py` 独立重跑，**58 项全部通过**（13.37 秒）。保留该组合差异记录，不修改断言来掩盖结果，也不声称全 UI 套件已经全绿。
- 最终真实入口冒烟：`python -m limbowave --smoke --log-dir .var/performance-smoke-logs`，offscreen 模式正常创建窗口并退出，输出 `[smoke] ok`，退出码 0；该模式不解锁或修改真实资料库。
- 最终相关代码 Ruff 与 `git diff --check` 通过。未提交、重置或覆盖工作区的其他修改。

最终日志：`.var/performance-final-unit-tests.log`、`.var/performance-final-ui-tests.log`、`.var/performance-settings-isolated-tests.log`、`.var/performance-integration-tests.log`、`.var/performance-web-tests.log`、`.var/performance-web-final-e2e.log`、`.var/performance-mypy.log`、`.var/performance-final-smoke.log`。
