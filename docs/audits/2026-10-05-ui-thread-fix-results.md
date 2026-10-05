# UI 线程阻塞修复记录

日期：2026-10-05（Asia/Shanghai）

## 实施范围

本轮修复了审计中已复现及主要高成本的同步业务路径，没有重写界面或降低密码学参数。保留了开始工作时已有的未提交功能改动；未自动提交 Git。

- 工具：所有同步工具统一通过取消安全的后台边界执行；前置路径解析、权限读取和审计写入也移出 GUI。记忆一次性授权增加短临界区保护，落库前复核运行有效性。
- 搜索：180 ms 防抖，HistoryReader 后台查询，查询代次校验，过期结果不覆盖新输入。
- 附件：文件/文件夹/剪贴板/会话片段导入、图片读取解码、发送载荷与 Base64 编码后台化。图片按 ID 查询，不再为每个附件扫描全部图片记录。已发送附件支持异步回填。
- 发送竞争：图片编码期间不允许发送；发送等待全部附件准备任务，包含文件夹扫描派生出的导入任务；按会话和草稿代次校验迟到结果。
- 分叉/重试：扩展 ConversationProcess 操作集合，后台准备手动 fork 和 retry，分叉事件直接携带历史 DTO。可见历史复用区分“保留起点的分叉”和“排除起点的重生成”。
- 外观：背景规范化、哈希、PNG 保存、字体读取/复制、孤立资源扫描清理移到后台；预览默认不再读完整聊天历史。Qt 字体注册和控件更新仍在 GUI。
- 维护：导出列表准备、导出文件、备份、恢复、系统恢复验证与关闭后台化。导出/备份/恢复写入串行，避免后台并发写同一产物。
- 压缩/记忆/权限面板：后台加载 DTO、组装压缩提示词、写入版本和记忆；面板先捕获控件值，工作线程不读取 QWidget；完成后更新 GUI。
- 其他：模型目录刷新后台化；重命名、删除、自动命名、失败切换的历史读取移出主线程；权限更新串行且发送前等待刷新。
- 生命周期：取消协程不能遗留事务；退出先禁止新请求、等待后台任务，再关闭存储。独立面板通过 Qt 定时器检查 Future，工作线程不调用 QObject 方法。

## 关键文件

- [取消安全执行与关停等待](../../src/limbowave/application/background.py)
- [独立面板后台任务与 GUI 投递](../../src/limbowave/ui/background_tasks.py)
- [应用装配和事件入口](../../src/limbowave/app.py)
- [会话协调器](../../src/limbowave/application/services/run_coordinator.py)
- [会话后台进程](../../src/limbowave/infrastructure/conversation_process.py)
- [业务入口响应性及草稿校验测试](../../tests/ui/test_business_offload.py)
- [重生成、手动分叉、重试写锁响应性测试](../../tests/ui/test_regenerate_responsiveness.py)

## 心跳实测（同机合成数据，单次样本）

使用离屏 QApplication + qasync，5 ms Qt 心跳。没有读取真实资料库。比较的是 Qt 最大心跳间隔，而不是业务总耗时；线程调度和系统负载会导致波动。

| 场景 | 修复前最大心跳间隔 | 修复后最大心跳间隔 |
| --- | ---: | ---: |
| 1,000 条约 1 KiB 消息搜索 | 34.9 ms | 9.5 ms |
| 10,000 条约 1 KiB 消息搜索 | 331.6 ms | 10.7 ms |
| list_directory 遇到 400 ms SQLite 写锁 | 368.0 ms | 7.5 ms |
| 3840×2160 合成 JPEG 背景导入 | 3213.9 ms | 20.9 ms |
| 极小配置备份（默认 Argon2id） | 62.5 ms | 5.9 ms |

4K 图仍需约 3.2 秒处理，但这段时间 GUI 心跳持续运行。

方法边界：工具测试提取实际 app.py 派发函数体；背景导入通过真实 AppearanceEditor 槽函数；搜索使用同一个 HistoryReader 边界；备份通过实际取消安全执行器调用同步备份服务。并非真实模型/用户数据端到端录制。

- 复测脚本：`.var/ui-thread-fix/probe.py`（仅本地）
- 复测结果：`.var/ui-thread-fix/post-fix-results.json`（仅本地）

## 验证与测试维护

新增 15 个（含参数化）回归用例，覆盖工具线程、Qt 心跳、过期搜索、背景导入、发送草稿失效、剪贴板准备、取消等待和 fork/retry 写锁。原有压缩、记忆、外观测试改为等待实际异步完成后断言，没有移除数据/语义断言。

定向验证曾完成 282 项通过；最终阶段的聊天、剪贴板与新增业务测试 82 项通过、进程正常退出。mypy 检查 151 个源文件通过；本轮修改范围 Ruff 通过；git diff --check 无补丁空白错误（既有 CRLF 文件有 Git 换行提示）。

排查了 Qt 离屏剪贴板在进程退出时的原生异常：用修复前原样同步粘贴函数做独立对照也返回 0xC0000005。为测试补齐 clipboard MIME 清理后，相关测试正常退出。没有通过清空真实应用剪贴板规避问题。

- 原同步粘贴对照日志：`.var/ui-thread-fix/original-paste-control.log`（仅本地）
- 正常退出验证日志：`.var/ui-thread-fix/clipboard-cleanup2.log`（仅本地）
- 全量验证日志：`.var/ui-thread-fix/full-verified.log`（仅本地）

## 仍然存在的边界

- 没有将所有低频、小型 JSON 配置/偏好读写统一改造成异步接口；部分设置保存仍是同步的小文件操作。本轮不声称“GUI 线程零 I/O”。
- 大量搜索命中结果或消息控件的创建、布局和绘制仍属 GUI 工作，未将这部分前端性能问题纳入本轮重构。
- QImage 转 QPixmap、字体注册、对话框与控件更新必须留在 GUI。后台线程的纯 Python 工作仍可能竞争 GIL；必要时再针对测得的 CPU 瓶颈扩展进程隔离。
- Qt/操作系统平台测试的剩余失败应单独处理，不靠修改产品安全逻辑或删除断言让它们变绿。

## 最终全量结果

最终同进程运行：**2081 passed, 6 failed, 7 skipped, 2 warnings，363.47 秒**。退出码为正常测试失败码 1，不再发生 Qt 原生退出崩溃。新增的性能/线程/竞态用例均通过。

仍未解决的 6 项断言：
1. test_checkbox_style.py::test_unchecked_and_checked_both_draw_a_visible_box：复选框像素边界颜色断言。
2. test_diagnostics_integration.py::test_actual_smoke_survives_log_target_lock：冒烟进程退出正常，但 stderr 没有预期的“仅内存”文字。
3. test_login_page.py::test_login_page_types_then_reveals_password：Segoe UI / Segoe UI Symbol 字体断言。
4. test_login_page.py::test_login_scroll_direction_and_fades：转场像素颜色断言。
5. test_main_window.py::test_transition_snapshot_caps_high_dpi_frame_without_losing_content：高 DPI 快照颜色断言。
6. test_shell_executor.py::test_real_child_process_output_decoded：本机 pwsh 子进程命令没有得到预期 stdout。

没有删除或放宽上述断言，也没有将这些失败归为“全量通过”。详情见 full-verified.log。两条警告来自 Windows 子进程 transport 的回收。
