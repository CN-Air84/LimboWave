# MCP Client Support Implementation Plan

**Goal:** Connect user-configured MCP servers to LimboWave desktop conversations through the existing permission, privacy, IPC and audit pipeline.

**Architecture:** Keep MCP outside Pi: a Python SDK client opens bounded, per-operation stdio or Streamable HTTP sessions. Expose `list_mcp_tools` and `call_mcp_tool` through the existing application-tool bridge; no dynamic provider tool-name collision or process restart is needed. Store a separate validated, atomic `mcp.json` configuration; secrets are host environment references, never inline saved credentials. A lazy native settings page manages servers and tests discovery off the UI thread.

**Tech Stack:** Python 3.12+, official MCP Python SDK (stable 1.x), Pydantic, jsonschema, PySide6, existing TypeScript Pi extension, pytest.

## Decisions and boundaries
- Client integration, not exposing LimboWave itself as an MCP server.
- Prefer a gateway facade over injecting dynamic tools into Pi: preserves existing gates and immediately sees configuration changes.
- Support stdio argv (never shell strings) and Streamable HTTP. HTTPS required except explicit loopback HTTP; no redirects. No legacy SSE, OAuth, resources/prompts, sampling, roots or automatic reconnection/retry in this slice.
- Servers disabled by default; startup does not launch them. Discovery can execute local code and therefore also needs per-call confirmation. Read-only/chat-only cannot invoke external MCP, even if server annotations claim read-only. Web-origin calls stay denied. No persistent MCP grants; every request requires confirmation even in full-access mode.
- Bound handshake, discovery pagination, schema size, calls and output. Close sessions/subprocesses after each operation; report failures without raw SDK exceptions, URLs or credentials. Never retry a potentially side-effecting call.
- Remote results and descriptions are untrusted evidence, never instructions. Run outbound and inbound privacy checks through the existing gateway.

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
