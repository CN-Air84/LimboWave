# Per-site RPM Implementation Plan

**Goal:** Add a per-endpoint RPM setting (default 5) that paces capability probes and actual conversation provider requests.

**Architecture:** One thread-safe, monotonic-clock limiter in the application owns each endpoint's last request time. HTTP probes acquire before every request. The Pi extension acquires through an authenticated, rate-limit-only IPC capability before every provider request, including tool continuations and isolated background kernels. Smooth pacing avoids bursts; waiting never blocks the Qt event loop. Existing configuration defaults to 5 RPM.

**Tech Stack:** Python/Pydantic/PySide6/httpx, asyncio JSONL IPC, TypeScript Pi extension, pytest.

## Steps
1. Add validated `EndpointConfig.rpm` and an RPM spinbox with new/edit/save/load coverage; preserve existing endpoint settings when editing.
2. Add a testable thread-safe rate limiter with endpoint isolation, live configuration, cancellation and monotonic-clock tests.
3. Pace each probe HTTP request, not only each model; use a dedicated bounded probe executor and stop waits during shutdown.
4. Add an independent rate-limit-only IPC token and Pi request hook so chat, retries, tool continuations and background text requests share the same budget without granting isolated kernels tool access.
5. Add unit/UI/real-Pi mock-provider coverage, run targeted tests and static checks, and document the setting.

## Verification
Run `.venv/Scripts/python.exe -m pytest` for configuration, settings, probes, limiter, IPC and provider integration tests. Run ruff and mypy on changed production files, distinguish existing unrelated failures from regressions. No real provider keys or requests are used in tests.
