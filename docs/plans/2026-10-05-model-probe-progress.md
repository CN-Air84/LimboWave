# Actual Model Probe Progress Implementation Plan

**Goal:** Reuse each actual-model row background as a request-based capability detection progress bar without changing its controls or sorting animation.

**Architecture:** Report immutable completed/total snapshots from the synchronous probe after each fully consumed/closed request (including handled failures). Initially reserve the protocol-specific maximum: one streaming request, six effort requests or one budget request, and up to three tool turns. At normal termination remove unused requests from the denominator. Forward worker callbacks through the asyncio GUI loop, retain active progress per endpoint/model in SettingsPage, and paint a clipped translucent accent fill beneath the row controls. Successful completion keeps the full fill; errors retain the last fraction, and retries reset it. No credentials or response bodies are included in progress.

**Tech Stack:** Python, httpx, asyncio, PySide6, pytest/pytest-qt.

## Tasks
1. Add failing service tests in tests/unit/test_model_probe_progress.py for request completion timing, errors, cancellation, protocol totals and early termination.
2. Add ModelProbeProgress and the request tracker in src/limbowave/application/services/model_probe.py; propagate it through streaming, thinking and tool turns without changing requests or RPM behavior.
3. Add row painting and page progress handling in src/limbowave/ui/model_probe_page.py. Use current theme tokens, keep borders and real controls, expose counts through tooltips/accessibility, reset on retry.
4. Wire worker callbacks in src/limbowave/app.py through call_soon_threadsafe; retain/restore active snapshots and ignore stale completions in src/limbowave/ui/settings_panel.py.
5. Add UI regression coverage in tests/ui/test_model_probe_progress_ui.py, run targeted and broader tests, lint/type checks, and inspect light/dark rendered images. Use mock HTTP only.

## Verification
Run .venv/Scripts/python.exe -m pytest for the new tests plus model discovery/capability, model probe, actual-model page, settings page and endpoint limiter suites. Run ruff and mypy on changed production files. Distinguish pre-existing unrelated failures. Do not overwrite other uncommitted changes.

## Verification results
- 24 new regression cases passed (18 service, 6 UI), including RPM wait/cancel, all four protocols, 1–3 tool turns, stream-close timing, endpoint restore, retry reset and GUI-thread delivery after page destruction.
- Related suites: 131 passed, 2 deselected. The excluded existing side-rail hover tests also fail independently and with this change's row/settings progress logic removed in memory; no workspace rollback was performed.
- Ruff and strict mypy passed on all four changed production files. Dark/light previews rendered and inspected with local fonts under .var/probe-progress-preview/.

## Follow-up: animate progress advancement
The initial implementation changed the fill width immediately and missed the requested motion. Keep actual completed/total values immediate, but animate a separate displayed fraction with a 320 ms OutCubic QVariantAnimation. Retarget from the currently displayed fraction on rapid updates; repeated targets must not restart the animation. Retry snaps to zero. Hidden rows settle to the latest target rather than replaying stale progress on return. Keep this independent from the existing row-position animation. Add real event-loop, deterministic intermediate-frame, retarget, reset and hide/show tests, then rerun the existing progress/page suites.

Animation follow-up verified: 75 progress/service/actual-model-page tests passed, including three new motion regressions and dark/light end-state painting checks. Ruff and strict mypy passed.
