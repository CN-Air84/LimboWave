# 对话过程响应性优化与验证

日期：2026-10-06（Asia/Shanghai）。

## 范围与结论

本轮聚焦桌面聊天的正文流式更新、阅读/翻页、滚动跟随和密集事件调度；不改启动装配、登录、模型预热或 LAN/Web 功能。工作区中原有未提交修改和另一个对话的启动优化均保留，未执行 Git 重置或提交。

核心收益是减少重复的 GUI 工作，而不是加快模型生成：首个正文片段立即写入文档，后续按约 32 ms 合并；翻页不再重建已经显示的消息；密集 RPC/工具后续事件不再连续独占共享事件循环。

## 实现

### 流式正文

- 新增 `src/limbowave/ui/streaming_text.py`：GUI 线程内的单次定时器合并缓冲，不是不断重启计时的防抖。持续输出时仍会按期刷新，空闲后恢复立即显示。
- `chat_view.py` 用独立 QTextCursor 追加文本，保留选择、禁止撤销历史；滚动更新随实际文档刷新，而非每个 token。
- 工具/分段边界与没有 message.end 的停止/收尾会补齐缓冲。权威最终文本、重试和清空会丢弃被替代的旧片段，避免晚到文字污染新内容。
- 隐藏回复暂停文档更新，重新显示时补齐；复制取完整内容。测试同时验证清空后行与缓冲对象可释放。
- 折叠思考流不再逐 token 请求滚动同步；保留已有思考区按需物化实现。

### 历史分页与阅读位置

- 提取复用同一套分段/run 分组渲染逻辑；加载更早时只插入下一页，保留旧行对象、选中文字、展开的思考/工具和正在生成的回复指针。
- 一页插入期间暂挂外层 transcript 布局，结束后集中激活，避免每插一行就重算全部历史。
- 用可见行坐标作为短期阅读锚点，纠正延迟布局的高度变化；主动滚动或点击回到底部可取消锚点恢复。
- 单独保留用户向上滚动的意图，解决输入框停靠动画令滚动范围短暂归零后，下一批正文错误地把阅读位置拉回底部的问题。

### RPC 与工具后的事件积压

- `pi_rpc.py` 的 stdout 分发在约 8 ms 或 64 帧后显式让出事件循环。已缓冲的 read 和小帧 async 解码可能同步完成，不能仅靠写了 await 就假定界面有机会响应。
- 大 JSON 帧使用 bytearray 累积，只搜索新增后缀，避免每个管道读都复制、扫描已有完整前缀。保留字节换行协议、CRLF、Unicode 分隔符、尾帧与事件顺序。
- `run_coordinator.py` 只修改排空工具后续队列的循环，使用相同软预算；让出后重新核对队列与 run 身份。权威事件、审计、落库顺序不合并、不丢弃。

## 同机合成复测

两组使用相同脚本、当前依赖和输入，分别运行三次，以下耗时取中位数。优化前的 ChatView/PiRpcProcess 源码在开始修改前单独保存在 `.var/conversation-performance/baseline/`，对照只通过内存导入，不回滚工作区。基准直接使用最小离屏 ChatView；不包括完整主窗口、真实字体/合成器或真实资料库。

机器未与其他工作负载隔离，绝对时间存在明显波动；最初基线的逐字增量约 2.07 秒，最终复测中位数约 4.83 秒。因此把文档更新次数、旧行保留数量和事件顺序作为主要回归约束，不把固定毫秒数写成跨机器承诺。

| 测项 | 优化前 | 优化后 |
|---|---:|---:|
| 4,000 个逐字增量：文档更新次数 | 4,000 | 2 |
| 4,000 个逐字增量：同步处理调用耗时 | 4832.46 ms | 21.54 ms |
| 4,000 个逐字增量：含等待文档补齐的耗时 | 4832.56 ms | 37.58 ms |
| 1,000 个多行增量：文档更新次数 | 1,000 | 2 |
| 1,000 个多行增量：含等待文档补齐的耗时 | 385.64 ms | 62.75 ms |
| 连续加载更早消息，第 1 页 | 1617.00 ms | 741.40 ms |
| 连续加载更早消息，第 2 页 | 2335.90 ms | 728.43 ms |
| 连续加载更早消息，第 3 页 | 2952.66 ms | 753.14 ms |
| 三次翻页分别保留的既有消息行 | 0 / 0 / 0 | 60 / 120 / 180 |
| 10,000 个已就绪 RPC 事件：中途让其他任务运行的次数 | 0 | 156–158 |
| 8 MiB JSON 分帧读取、解码与分发耗时 | 458.67 ms | 82.96 ms |

所有样本均保留立即写入首个正文片段和完整最终文本。正文“补齐”是 QTextDocument 的内容完成，不等同屏幕完成呈现；RPC 中途运行次数来自 asyncio 测试任务，也不等同真实 Qt 帧率。调度公平性可能增加少量吞吐开销，目标是输入、停止和绘制不被整段事件队列饿死。

复现当前版本：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_conversation.py --repeats 3 --output .var/conversation-performance/recheck.json
```

本机保留的优化前对照：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_conversation.py --chat-source .var/conversation-performance/baseline/chat_view.py --rpc-source .var/conversation-performance/baseline/pi_rpc.py --output .var/conversation-performance/baseline-recheck.json
```

原始数据：`baseline-final.json`、`optimized-final.json`（均位于 `.var/conversation-performance/`）。基准脚本会检查首片段立即到达和输出完整性，未补齐的样本不算性能成功。

## 验证结果

- 最终专项回归：**236 passed / 1 skipped**。覆盖本轮 21 项 UI 性能/生命周期回归、聊天/思考/消息窗口、切换会话、滚动、停止重试、分支、压缩、队列公平性、RPC，以及协调器端到端、故障注入和长会话压力。
- 后端专项：**87 passed / 1 skipped**，含真实 spawn 子进程、工具顺序、run 协调与请求日志。两组覆盖有重叠，不相加冒充唯一测试数。跳过项为本机不适用的 POSIX 符号链接布局。
- 更宽 UI 回归：**273 passed / 1 failed**；失败为 `test_slide_interpolates_panel_height_without_endpoint_jump[tool_only]`。把相关模块改用优化前保存的 ChatView 后仍以同一 `131 != 188` 断言复现（22 passed / 1 failed）；当前动画模块单独运行 14 项通过，说明有执行顺序/布局时序影响。未为本次流式优化修改该动画实现或放宽其断言。
- 尝试全量 `pytest tests`，在 Qt 的 `pytest_runtest_teardown -> _process_events` 阶段发生 Windows access violation（退出码 `0xC0000005`），约 34% 后终止；此前还有失败/错误。因此**不宣称全量测试通过**，也不把所有未定位失败都归为既有问题。
- 本轮改动文件的 Ruff 与 4 个生产源码文件的 mypy 检查通过；Git 差异空白检查通过。
- 另生成合成对话离屏截图，加载现有主题与系统字体核对正文、代码块和气泡布局；未操作真实用户窗口、资料库或连接真实模型。

日志：`verification-final.log`、`backend-regression.log`、`focused-ui-final.log`、`baseline-tool-comparison.log`、`full-regression.log`，均在 `.var/conversation-performance/`。

## 保留的限制

- 加载更早的一页本身仍同步创建 Qt 控件；本次复测一页约 0.73–0.75 秒，虽然不再随着已展开页数重复重建，仍不代表翻页完全无停顿。逐帧创建新页/完整虚拟化是后续可继续优化的方向。
- 单条很大的 Markdown 定稿和 QTextDocument 布局不能被 8 ms 事件预算抢占；本轮没有把 Qt 文档操作搬到工作线程，也没有截断内容换速度。
- 未进行真实模型首字延迟、真实合成器 FPS、长时间生产环境 CPU/内存曲线测试；不以离屏结果代替实机体感验收。
