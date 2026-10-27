# HIST-02 Context Window Expansion Implementation Plan

**Goal:** Extend `query_chat_history` to expand a saved message into a bounded, authorized conversation window.

**Architecture:** Preserve the existing search API and HISTORY_READ gate. Add a mutually exclusive message-ID mode, resolve effective branch segments using existing metadata-only lineage logic, and fetch only public message fields through bounded keyset reads. Apply the requester's privacy policy before returning any source text.

**Tech Stack:** Python application services, encrypted SQLite/in-memory repositories, Pi TypeBox tool schema, pytest.

## Decisions
- Reuse one tool instead of adding a duplicate permission surface or automatically expanding every search hit.
- `message_id` selects the anchor; optional `conversation_id` must match, and `branch_id` selects an effective branch containing that anchor (default: owning branch).
- `before_turns` / `after_turns`: default 2, integer 0–3. A turn starts with a user message and includes following assistant messages; include the anchor turn itself.
- Search/pagination parameters cannot be mixed with expansion parameters. Reject invalid input before approval or payload reads.
- Return chronological public messages with authoritative IDs, timestamps, statuses, anchor and truncation metadata. Never fetch thinking, tool payloads, segments, attachments, or runtime mirrors.
- Enforce message-count, per-message character and UTF-8 output limits; retain the anchor when trimming. Privacy-excluded text cannot be recovered by ID or through its replies. Fresh-start forbids cross-conversation expansion.
- Existing uncommitted workspace changes belong to other work and must be preserved. No database migration, semantic search, automatic grants, time-range search or full-message continuation in this increment.

## Steps
1. Add failing unit tests on both repositories for turns, mixed arguments, missing/mismatched anchors, branch forks, private ciphertext and privacy filtering.
2. Add bounded public-only keyset projection; implement effective-branch expansion, privacy filtering and output sizing.
3. Wire mutually exclusive expansion arguments into service validation, the gateway schema and Pi tool guidance; keep existing search responses unchanged.
4. Update tool documentation with examples and boundary semantics.
5. Run targeted and adjacent history/privacy/permission regression tests, lint/type checks for changed code, and inspect the final diff without reverting unrelated changes.

## Completion — 2026-10-27

- [x] Public-only bounded keyset reads implemented for SQLite and in-memory repositories; no migration or plaintext index.
- [x] Existing `query_chat_history` extended with message-ID context mode, whole-turn bounds, selected-branch lineage and anchor-preserving UTF-8 trimming.
- [x] Privacy rules and fresh-start restrictions enforced, including assistant paraphrases of blocked user turns and unknown provenance at scan caps.
- [x] Gateway and Pi schemas updated; default windows are explicit in approval/audit parameters. Permission UI displays escaped anchor IDs and requested turn counts.
- [x] Documentation and unit/integration tests completed. Existing search behavior retained.
- [x] Final selected regression run: **586 passed, 2 skipped** (memory variants of SQLite-only ciphertext tests), including real Pi model -> desktop approval -> IPC execution.
- [x] Ruff passed for all changed Python areas; mypy passed for six implementation modules with silent imported-module following; scoped `git diff --check` passed.

Verification command:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_context.py tests/unit/test_chat_history.py tests/unit/test_privacy_boundaries.py tests/unit/test_history_service.py tests/unit/test_web_history.py tests/unit/test_tool_gateway.py tests/unit/test_tool_ipc_server.py tests/unit/test_runtime_capabilities.py tests/ui/test_permission_prompt.py tests/integration/test_web_permission_boundary.py tests/integration/test_chat_history_pi.py -q
```

The full repository test suite was not run. Unrelated uncommitted changes were preserved; no commit or deployment was performed.


## 用户决定 — 2026-10-27（取代此前的 OOBE 介绍要求）

HIST-02 **不加入 OOBE、不参与联合 onboarding demo**。保留已实现的历史前后文读取工具，仅清理误加入的历史演示页、导航、样式、截图钩子、专用测试与联合提案中的历史介绍内容。语音和联网 demo 保留，正式 OOBE 三个文件不改动；其他 OOBE 新板块仍需用户验收后另行批准实装。用户已授权将此决定回复 VOICE-01 会话，且已发送。后续不得依据旧提案重新加入历史介绍。

### READY — OOBE 残留清理交接（2026-10-27）

- 已向 VOICE-01 协调会话回复：HIST-02 不加入 OOBE；之后按协调请求在本会话完成交接，不重复跨会话派发。
- 共享 demo 已移除历史页、模拟授权交互、导航/样式/截图钩子及专用测试；原语音与新联网交互类保持 AST 一致。流程为欢迎 → 语音 → 联网 → 服务商，语音设置支线保留。
- 联合提案后来加入了 NET-01，因此保留文件及联网段落，只移除历史介绍内容；未整份删除其他会话的新工作。
- 正式 OOBE 三文件相对 HEAD 无改动；HIST-02 服务实现清理前后哈希一致。
- 人工验收：语音和联网 demo 继续等待用户验收，正式 OOBE 未实装；HIST-02 无 OOBE 验收项。无剩余清理代码任务。

本次实际修改文件：
1. `G:\LimboWave\tools\demos\voice_oobe_demo.py`
2. `G:\LimboWave\tools\demos\test_voice_oobe_demo.py`
3. `G:\LimboWave\tools\demos\README-voice-oobe.md`
4. `G:\LimboWave\docs\plans\2026-10-27-oobe-companion-demo.md`
5. `G:\LimboWave\docs\plans\2026-10-27-hist-02-context-expansion.md`

最终验证命令（工作目录 `G:\LimboWave`）：
```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tools/demos/test_voice_oobe_demo.py tests/unit/test_chat_history_context.py tests/unit/test_chat_history.py tests/ui/test_oobe_demo.py tests/ui/test_oobe_live.py -q --maxfail=3
.venv\Scripts\python.exe -m ruff check tools/demos/voice_oobe_demo.py tools/demos/test_voice_oobe_demo.py
```
结果：**201 passed, 2 skipped**（SQLite 专用检查的内存仓库分支），Ruff 全部通过。历史介绍残留扫描仅命中取消决定与防回归断言。全部本轮执行会话已结束，无本轮未完成进程/写操作；未发现仍运行的 `voice_oobe_demo.py` 窗口进程。未提交、未合并、未部署。共享 demo 编辑权可以交回协调会话。

## READY — HIST/PRIV P1 普通搜索轮次泄漏修复（2026-10-27）

### 修复范围
- 已先写失败复现：内存仓库普通检索曾放出被排除用户轮的无关键词助手改述；SQLite 普通投影缺少 run 元数据还会误杀安全助手回复。
- 普通搜索启用禁区时，按消息所属有效分支进行公开投影 keyset 回读，直到最近用户消息，再用完整轮次前缀判定；之后才做关键词与 offset/limit 分页。
- 每候选最多64条、每页最多8条，缓存最多256条；来源缺失、损坏分支、超限均保守拒绝该候选。超限失败不把更早可验证候选一并写成拒绝。
- 覆盖父未来/兄弟分支隔离、起点包含/替换、嵌套继承、同时间ID排序、来源ID屏蔽、安全新轮恢复、私有密文隔离，以及旧派生/无run回复隔离。
- 原检索DTO、排序、结果与UTF-8上限、逐次授权不变；无禁区时保留原路径。未改 `chat_history_context.py`、仓库、tool_gateway、privacy领域/服务、扩展或OOBE/demo。

### 本次实际写集
1. `G:\LimboWave\src\limbowave\application\services\chat_history_service.py`
2. `G:\LimboWave\tests\unit\test_chat_history_search_privacy.py`（新增）
3. `G:\LimboWave\docs\development\chat-history-tool.md`
4. 本交接计划文档。

### 最终验证
```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_search_privacy.py tests/unit/test_chat_history.py tests/unit/test_chat_history_context.py tests/unit/test_privacy_boundaries.py tests/integration/test_privacy_pi.py tests/integration/test_chat_history_pi.py -q --maxfail=3
# 202 passed, 3 skipped；含真实Pi和PRIV交叉集。
# 随后增加旧派生/无run和损坏分支用例，最终代码再跑完整HIST/PRIV单元集：
.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_search_privacy.py tests/unit/test_chat_history.py tests/unit/test_chat_history_context.py tests/unit/test_privacy_boundaries.py -q --maxfail=3
# 186 passed, 3 skipped。
.venv\Scripts\python.exe -m ruff check src/limbowave/application/services/chat_history_service.py tests/unit/test_chat_history_search_privacy.py
.venv\Scripts\python.exe -m mypy src/limbowave/application/services/chat_history_service.py --follow-imports=silent
```
首次交叉集因PRIV新集遗漏memory_stack导入报1个setup error；其他会话修复后完整复跑已通过，我未编辑该PRIV文件。3个skip均为SQLite专用检查的内存夹具分支。

剩余工作：无本修复代码任务；协调会话可做全批回归/人工验收。HIST仍不加入OOBE。所有本轮测试进程已结束；完成本条记录后不再有待结束写操作。未提交、合并或部署，可释放限定写集。
