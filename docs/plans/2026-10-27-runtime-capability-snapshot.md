# CAP-01 Runtime Capability Snapshot Implementation Plan

**Goal:** Give every provider request a fresh, lightweight, truthful view of runtime capabilities.

**Architecture:** Keep tool registration and permission enforcement unchanged. Derive capability availability from the real gateway services and current session permissions, then fetch it over an authenticated, read-only IPC operation in the final context hook. Bind requests to the active run; unavailable, malformed, stale or timed-out snapshots become unknown, never cached success.

**Tech Stack:** Python application services and asyncio IPC; TypeScript Pi extension; pytest and Node.

## Tasks
1. Add failing tests for configured/unconfigured services, presets, remote origin, reminder context, and refresh after changes.
2. Add a gateway-owned snapshot projection without executing tools, issuing permissions, or exposing credentials, paths, memory or reminder content.
3. Add authenticated, run-bound read-only IPC and application wiring. Do not register it as a model-callable tool.
4. Inject one transient custom context message after privacy projection on every provider request, with bounded latency and conservative fallback.
5. Verify dynamic refresh, unavailable channel, bad token/run identity, no stale snapshots, no duplicate overlays, and unchanged tool enforcement. Run related tests and static checks.

## Behavior contract
- Distinguish availability (active/unconfigured/restricted/unknown) from permission (allowed/requires_confirmation/scoped/denied).
- Reminders are local-only and require the app running; only successful set_reminder output confirms scheduling. No background monitoring capability is implemented.
- Explain relevant limitations early and offer alternatives; do not dump unrelated capabilities in every greeting or claim an offline network based solely on absent search configuration.
- No cached snapshot, history writes, secrets, network probes or permission grants.
- Preserve existing uncommitted privacy/reminder work. No Git commit requested.

## Implementation result
- Completed the live gateway projection, authenticated run-bound IPC and final transient context hook.
- Integrated concurrent NET-01 work: inspect real fetch/search backends and live credential reference presence, not a hardcoded search absence.
- Added Python unit tests, executable Node extension tests and real Pi/local-provider integration tests.
- Focused permission/network/reminder/privacy/tool bridge suite: 241 passed.
- Ruff and mypy checks pass for changed capability service/transport modules.
- Standalone extension type checking exposes existing tool-schema/result signature diagnostics outside CAP-01; do not silently broaden this change to rewrite tool registration.
- No Git commit or unrelated workspace cleanup performed.
- Existing memory/attachment runtime integrations: 19 passed; new CAP-01 real-runtime test passed.
- Manual/automatic retries with/without prior turns and attachments: all 8 passed. Updated the user-turn test helper to exclude CAP-01 custom overlays, and added explicit single-overlay checks instead of weakening the retry invariant.
- TypeScript baseline comparison against HEAD: both baseline and current report 22 TS2322 tool registration/result type diagnostics; no added diagnostic count or class, none in the CAP-01 block.
- Final current-tree combined capability/tool/network/privacy/real-Pi suite: 254 passed; plus 8 retry variants and 19 existing memory/attachment integrations. Final scoped Ruff and mypy checks passed.
