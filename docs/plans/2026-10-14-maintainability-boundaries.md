# Maintainability Boundaries Implementation Plan

> 本轮在现有工作树执行；保留开始时所有未提交改动，不自动提交。采用 writing-plans 的分步验证流程。

**Goal:** 降低附件应用服务对具体存储实现和私有函数的耦合，并把分层约束变成可执行测试。

**Architecture:** 保留桌面、服务端、Web 共用 domain/application 的架构。附件服务只依赖应用层的结构化 Protocol；加密、路径和密钥仍由基础设施与组合根管理。行索引是领域纯函数。对存量跨层依赖建立精确、带理由的例外清单，禁止新增与过期例外。

**Tech Stack:** Python 3.12、typing.Protocol、pytest、AST、ruff、mypy。

---

## 方案选择

1. **选择：渐进式收紧依赖边界。** 不改变存储格式、对外命令、UI 或关闭/恢复时序；可由局部回归证明兼容性。
2. 仅记录巡检结论：风险低，但不能阻止继续耦合，不满足优化要求。
3. 立即重写 app.py / RunCoordinator：可能消除更多复杂度，但与当前未提交修复重叠，验证成本过大，另行规划。

## Task 1: 附件存储端口

**Files:** 新建 `src/limbowave/application/blob_storage.py`；修改 `application/services/{file_service,clipboard_service,image_service,export_service,attachment_gc}.py`；新建 `tests/unit/test_attachment_storage_ports.py`。

1. 用不继承具体存储类的内存替身，补剪贴板写入→按行读取回归；先确认旧实现的 isinstance 断言失败。
2. 定义 `BlobReader.get`、`BlobStore.put/get/delete`、`BlobPruner.prune` 小协议；只读使用者只接受 reader，回收器只接受 pruner。
3. 替换服务依赖与参数类型，删除 FileService 的运行时具体类型断言。保留真实加密实现及其错误传播，不引入包装或新存储后端。
4. 导出图片渲染直接返回成功数量，删除重复的 exists 计数路径；回归锁住损坏 blob 不计入导出数量、只读存储不需要 exists。
5. 覆盖读错误透传、未配置存储、图片与导出/回收可替换性；运行真实加密存储的现有测试。

## Task 2: 共享行索引

**Files:** 修改 `src/limbowave/domain/files.py`、`application/services/file_service.py`、`application/services/clipboard_service.py`；新建 `tests/unit/test_line_offsets.py`。

1. 补空文件、CR/LF/CRLF、混合换行、末尾换行和中文 UTF-8 的索引测试。
2. 将 `_compute_line_offsets` 原算法移动为领域公开函数 `compute_line_offsets`，两个服务改用它，不改变行号规则。
3. 重新索引时只计算一次偏移；剪贴板登记卡使用同一份偏移计算行数。
4. 运行文件、Word、剪贴板相关回归。

## Task 3: 可执行分层约束

**Files:** 修改 `tests/unit/test_architecture_boundaries.py`。

1. 为 AST 扫描器补普通导入、from 导入、相对导入、包级导入、嵌套导入测试。
2. 保留 Qt 禁入约束，增加 domain 只能依赖 domain、application 只能依赖 domain/application 的内部依赖规则。
3. 为备份恢复、默认内存仓库、同步缓存的当前依赖列精确例外与理由；例外消失时也失败，防止旧豁免掩盖回归。
4. 全局静态扫描；不把 AST 检查宣称为运行时动态导入/传递依赖的完全证明。

## Task 4: 项目地图与验证

**Files:** 新建 `docs/architecture/adr-0003-application-storage-boundaries.md`、`docs/development/project-map.md`。

1. 记录主要模块职责、当前维护热点、依赖规则与下一步拆分优先级。
2. 局部测试：`.venv/Scripts/python.exe -m pytest tests/unit/test_architecture_boundaries.py tests/unit/test_attachment_storage_ports.py tests/unit/test_line_offsets.py tests/unit/test_file_service.py tests/unit/test_clipboard_service.py tests/unit/test_image_service.py tests/unit/test_export_service.py -q -p no:cacheprovider`。
3. 检查本轮文件的 ruff 与 mypy；运行完整 unit 与 integration 回归，尽量验证 UI；区分既有问题、环境限制和本轮回归。
4. 检查最终差异，确认未覆盖原有改动，无数据库迁移或生产数据修改。

## 修改前基线

173 passed：architecture_boundaries、file_service、clipboard_service、image_service、export_service。沙箱中的默认 pytest 临时目录不可访问，已获准在沙箱外重跑通过。
