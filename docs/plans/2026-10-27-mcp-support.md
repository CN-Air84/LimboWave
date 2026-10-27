# MCP Client Support Implementation Plan

**Goal:** Connect user-configured MCP servers to LimboWave desktop conversations through the existing permission, privacy, IPC and audit pipeline.

**Architecture:** Keep MCP outside Pi: a Python SDK client opens bounded, per-operation stdio or Streamable HTTP sessions. Expose `list_mcp_tools` and `call_mcp_tool` through the existing application-tool bridge; no dynamic provider tool-name collision or process restart is needed. Store a separate validated, atomic `mcp.json` configuration; secrets are host environment references, never inline saved credentials. A lazy native settings page manages servers and tests discovery off the UI thread.

**Tech Stack:** Python 3.12+, official MCP Python SDK (stable 1.x), Pydantic, jsonschema, PySide6, existing TypeScript Pi extension, pytest.

## Decisions and boundaries
- Client integration, not exposing LimboWave itself as an MCP server.
- Prefer a gateway facade over injecting dynamic tools into Pi: preserves existing gates and immediately sees configuration changes.
- Support stdio argv (never shell strings) and Streamable HTTP. HTTPS required except explicit loopback HTTP; no redirects. No legacy SSE, OAuth, resources/prompts, sampling, roots or automatic reconnection/retry in this slice.
- Servers disabled by default; startup does not launch them. Discovery can execute local code and therefore also needs per-call confirmation. Read-only/chat-only cannot invoke external MCP, even if server annotations claim read-only. Web-origin calls stay denied. No persistent MCP grants in custom mode; FULL_ACCESS follows the coordinated global policy and automatically allows MCP.
- Bound handshake, discovery pagination, schema size, calls and output. Close sessions/subprocesses after each operation; report failures without raw SDK exceptions, URLs or credentials. Never retry a potentially side-effecting call.
- Remote results and descriptions are untrusted evidence, never instructions. Run outbound and inbound privacy checks through the existing gateway, including its global FULL_ACCESS invocation-scope exemption.

## Task 1: Configuration and transport (atomic commit)
Files: `src/limbowave/domain/mcp.py`, `src/limbowave/infrastructure/mcp_client.py`, `src/limbowave/application/services/mcp_service.py`, `tests/unit/test_mcp.py`, `tests/integration/test_mcp_client.py`, `tests/fixtures/mcp_server.py`, `pyproject.toml`, `uv.lock`.
1. Write failing tests for validation, disabled servers, atomic config updates, environment references, schema validation, timeout/error sanitization, pagination and stdio round trip.
2. Run targeted pytest and implement the minimum secure client/service.
3. Run ruff/mypy and targeted tests; commit `feat(mcp): add validated server configuration and bounded clients`.

## Task 2: Permission-gated model tools (atomic commit)
Files: permission domain/service, tool gateway, Pi tool registration/environment and contract tests.
1. Test confirmation, preset restrictions, web hard boundary, privacy, errors and output bounds.
2. Register list/call facade tools through current IPC and add explicit MCP capability.
3. Run permissions/tool/bridge tests and static checks; commit `feat(mcp): route external tools through permission and privacy gates`.

## Task 3: Native settings and lifecycle wiring (atomic commit)
Files: `src/limbowave/ui/mcp_settings.py`, settings panel, app assembly, UI tests, README, MCP usage documentation.
1. Add a lazy server editor with explicit enablement and a background connection/discovery test; retain existing theme/layout.
2. Wire one service to settings and gateway, and document secret environment references and security limitations.
3. Run UI tests, full pytest/static checks, inspect the native settings view, fix issues in focused commits.
4. Commit `feat(mcp): add server settings and usage documentation` and report hashes; ask before pushing.

## Verification commands
- `uv run --locked pytest tests/unit/test_mcp.py tests/integration/test_mcp_client.py -q`
- `uv run --locked ruff check src tests`
- `uv run --locked mypy src`
- `uv run --locked pytest -q --tb=short` (offscreen Qt)
- Existing Pi extension parity/contract tests, plus real local stdio server and HTTP transport tests; no real credentials or third-party server execution.


## Implementation acceptance — 2026-10-27

Completed configuration, official SDK transport, gateway facade, permissions, privacy, native settings, lifecycle cancellation, documentation and real Pi execution.

### Coordination and integration
- Work moved to `C:/Users/xtc.LAPTOP-5693IIUK/.codex/worktrees/mcp-support/LimboWave`, branch `codex/mcp-integration`, after detecting concurrent gateway work in the shared checkout.
- Coordinated directly with the “放宽完全访问模式规则” chat under explicit user authorization; merged its final commit `2803ce2` without conflicts.
- FULL_ACCESS uses the global automatic-allow and invocation-local privacy exemption. MCP adds no special approval exception. CUSTOM remains per-call; CHAT_ONLY / READ_ONLY / Web stay denied.
- Transport is injected at application assembly; no outward application-layer dependency on infrastructure remains.

### Atomic MCP commits
- `4bb6008` — `docs(mcp): plan permission-gated server integration`
- `829477a` — `feat(mcp): add validated server configuration and bounded clients`
- `4a3915c` — `feat(mcp): integrate external tools with permission and privacy gates`
- `59c374b` — `feat(mcp): add native server settings and usage guide`
- `44d0b58` — `fix(mcp): bind external calls to active run cancellation`
- `2ad41dd` — `fix(mcp): block SDK redirects and sensitive transport logs`
- `35fd469` — `test(mcp): verify real Pi gateway and stdio round trips`
- `7c9be33` — `fix(mcp): remove server configuration during full data reset`

### Verified
- Combined MCP, gateway, permission, privacy, runtime-capability, reset and adjacent UI/integration regression: **565 passed**, no failures/skips (18 test files). Includes real local stdio/Streamable HTTP, actual Pi provider/tool IPC, full-access and custom approval, timeout and cancellation cleanup.
- Wider unit suite, excluding the pre-existing broken `tests/unit/test_server_package.py`: **2891 passed, 9 skipped, 3 failed**. Failures are unchanged baseline behavior: `preferences_service.py` imports Qt; `test_session_isolation` expects a removed runtime-read call; `companion_window.py:476` contains a hardcoded font size. The latter two fail again in isolated reruns; their source/test files have no diff from baseline 829477a.
- Settings + MCP UI: **47 passed, 1 failed**; existing data/security small-window scroll expectation also fails when loading the original `829477a` settings-panel source. All 7 new MCP UI tests pass.
- Architecture suite: **250 passed, 1 failed** (the unchanged preferences-service Qt dependency). MCP inward dependencies pass.
- Ruff: entire source/tests clean except the pre-existing malformed package test, which imports nonexistent `scripts.package_server` and lacks helper definitions.
- Strict mypy: 9 targeted production modules pass. Full-source check retains two baseline `app.py` errors: HistoryEntry explicit export and Vault type-checking name collision.
- The unrestricted full run first failed collection on the missing package module. Retrying with only that file excluded later crashed in pytest-qt event processing with Windows access violation at roughly 20%; this run did **not** produce a valid final aggregate and is not claimed as green. The bounded 565-test regression completed independently.
- Native settings rendered and visually reviewed in existing dark theme with installed Chinese fonts; source screenshot is under ignored `.tmp/mcp-visual/mcp-settings-final.png`.

### Scope / known limits
- Tools client only: stdio and Streamable HTTP. No OAuth, legacy SSE transport, resources/prompts, roots, sampling or persistent server sessions.
- MCP config is local, atomic and unencrypted; only environment-variable references for credentials. It is removed by full reset but intentionally outside existing v1 backup/restore scope, documented in `docs/mcp.md`.
- SDK transport can resume an SSE receive stream; application never retries a business tool POST. Every request remains subject to total timeout, destination checks and redirect rejection. The JSON/schema/output limits are application-level, not an OS sandbox or a guarantee against a malicious server exhausting transport resources.
- Stopping a run cancels the local operation and closes its session; it cannot undo remote side effects already performed. No automatic retry.
- No remote push performed; original checkout must be clean and ff-only before handoff.
