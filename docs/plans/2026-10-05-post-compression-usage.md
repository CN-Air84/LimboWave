# Post-compression Context Usage Implementation Plan

**Goal:** 压缩完成后立即显示压缩后实际上下文的估算占用，不需要额外一轮模型请求。

**Architecture:** 优先使用 Pi 的 get_session_stats.contextUsage。只有原生 tokens/percent 无效且窗口大小有效时，读取 get_messages 返回的有效上下文，使用与 Pi estimateTokens 一致的内容估算法；不读取旧账单 usage，不扫描完整历史树，也不缓存跨会话数值。CompressionService 和 UI 继续使用现有估算展示链路。

**Tech Stack:** Python 3.12+, Pi RPC, pytest, PySide6.

---

### Task 1: Lock the regression
- Create tests/unit/test_pi_context_usage.py.
- Cover immediate estimates, retained tail with stale usage, native usage priority, missing capability/window/messages, Unicode and multimodal/tool content, and the next native usage update.
- Update tests/integration/test_compression_runtime.py to assert immediate estimated reports after apply/resume and no extra provider requests.
- Run .venv/Scripts/python.exe -m pytest tests/unit/test_pi_context_usage.py -q before implementation; expect missing fallback failures.

### Task 2: Implement the adapter fallback
- Create src/limbowave/infrastructure/pi_runtime/context_usage.py for pure message-content estimates (chars/4; images count as 1200 tokens, never base64 length).
- Modify only get_context_usage and imports in src/limbowave/infrastructure/pi_adapter.py.
- Preserve unknown for unavailable data and preserve native usage when valid; include summary and retained messages, ignore stale usage fields.

### Task 3: Verify
- Run the new unit suite plus existing compression/runtime/service/UI regression suites.
- Run the real Pi/local mock provider compression integration test; no real endpoint calls or synthetic prompts.
- Run Ruff and type checks on changed production modules and inspect the final scoped diff.
- Preserve all pre-existing unrelated working-tree changes; do not commit them.

## Verification results
- Added tests/ui/test_post_compression_usage.py to assert the actual chat ring and tooltip refresh immediately without a prompt RPC.
- Red phase: 11 failures / 11 passes before implementation (missing fallback/helper).
- Targeted unit suites: 87 passed.
- Compression/chat Qt UI suites (offscreen): 102 passed.
- Real Pi + local mock provider integration: 1 passed; immediate apply/resume estimates with zero extra provider calls.
- Full unit suite: 1279 passed, 6 skipped, 2 failures. Both failures were reproduced in a separate Python process executing the pre-fix Pi adapter: compression marker ordering and shell child-process output. Neither is changed by this fix.
- Ruff and scoped mypy checks passed; git diff --check passed.
