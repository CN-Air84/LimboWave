# Lightweight Environment Awareness Implementation Plan

**Goal:** Add a deterministic `get_environment` tool returning compact weather, sunrise/sunset and location timezone data, without search or webpage content.

**Architecture:** Existing Pi proxy → ToolGateway parameter/origin/network-scope checks → cached application service → fixed-host HTTP JSON provider. MSN is opt-in with an authorized API key; Open-Meteo is the no-key personal/non-commercial default. Never fall back between providers on failure. No IP geolocation or inferred location; accept a user-provided city (ambiguous matches require selection) or coordinates.

**Tech Stack:** Python 3.12+, httpx, zoneinfo/tzdata, existing Pi TypeScript extension, pytest/httpx MockTransport.

## Research and alternatives
- MSN overview at `https://api.msn.com/weather/overview` was tested on 2026-10-26: no key returns 401; first-party website requests return JSON with `responses[].source.location`, `current` and `forecast.days[].almanac`.
- The MSN response explicitly reserves API access and reuse to Microsoft's written permission. Do not ship/reuse the public website key as an application credential. Read an authorized key only in the application HTTP backend.
- Microsoft documents an MSN connector for Power Automate/Logic Apps, not an unrestricted direct third-party REST contract: `https://learn.microsoft.com/en-us/connectors/msnweather/`.
- Open-Meteo supplies deterministic forecast/geocoding JSON and supports yesterday's modeled daily temperatures and sunrise/sunset. Free API is non-commercial and needs attribution: `https://open-meteo.com/en/docs`, `https://open-meteo.com/en/docs/geocoding-api`, `https://open-meteo.com/en/terms`.
- Alternatives: MSN-only (not usable without authorization); HTML/search scraping (noisy/fragile); fixed-provider JSON adapter (chosen, with explicit source and no silent fallback).

## Contract and safeguards
- `get_environment(location?: string, latitude?: number, longitude?: number)` requires either a nonempty explicit place or both finite coordinates. No URLs, provider names or credentials in model parameters.
- Return location, provider/attribution, actual fetch and source-update timestamps, local date/time/IANA timezone/DST, Celsius weather, daily highs/lows/sunrise/sunset, and explicitly labeled daily comparisons. Never claim a current-vs-yesterday observation if only daily model data exists.
- Fixed HTTPS hosts, DNS/private-network guard, no redirects, finite timeout, bounded streamed JSON. Sanitize HTTP failures so request URLs/API keys never enter tool results.
- Five-minute bounded in-memory cache; recompute clock/daylight on every call and refresh at local midnight. Check permissions even for cache hits. No disk location cache.
- MSN selected explicitly or when an authorized key is present; configuration failure/401/429/timeout returns a structured error, not guessed weather. Remove backend API-key environment variables from child Pi environments.

## Task 1: Tests before implementation
Create G:/LimboWave/tests/unit/test_environment_awareness.py for parameter validation, clean provider projections, ambiguity, missing fields, cache TTL/midnight, timezone DST, polar missing sun times, permissions/remote ceiling, bounded responses and redacted failures.
Run `.venv/Scripts/python.exe -m pytest tests/unit/test_environment_awareness.py -q`; expect missing module failure initially.

## Task 2: Service and HTTP providers
Create G:/LimboWave/src/limbowave/application/services/environment_service.py and G:/LimboWave/src/limbowave/infrastructure/environment.py. Add tzdata to G:/LimboWave/pyproject.toml and update G:/LimboWave/uv.lock for Windows IANA timezone support.

## Task 3: Wiring
Append schema/capability/backend dispatch in G:/LimboWave/src/limbowave/application/services/tool_gateway.py; instantiate service in G:/LimboWave/src/limbowave/app.py. Synchronize G:/LimboWave/src/limbowave/infrastructure/extensions/policy_enforcement.ts and G:/LimboWave/src/limbowave/infrastructure/pi_runtime/environment_builder.py. Filter the backend key from G:/LimboWave/src/limbowave/infrastructure/pi_adapter.py. Extend the all-tools remote boundary fixture without altering other ongoing work.

## Task 4: Verify and document
Run focused unit, gateway, clock, permission/origin, IPC and real-extension registration tests. Run ruff/mypy on feature files. Live-smoke default provider using explicit coordinates; authorized MSN must be supplied by the user for deployment validation. Write G:/LimboWave/docs/development/environment-tool.md with usage, provider configuration, source licensing and truthful limitations. Preserve all pre-existing modifications; no commit of others' work.


## Verification results
- Focused suite: **391 passed** (environment service/provider tests; actual Pi environment calls under all permission presets; gateway, clock, IPC, runtime environment/registration and remote-origin regressions).
- Ruff passed for the nine touched production/test Python files checked; mypy passed for six touched production files.
- Live default-provider smoke through the actual ToolGateway with explicit coordinates and the configured proxy: successful, three daily records, approximately 1.8 KiB result, first request approximately 1.4–1.5 seconds, immediate repeat served from memory cache with no network request.
- Windows packaging inspection: installed PyInstaller hooks import tzdata for zoneinfo and collect tzdata resources/submodules. No new packaging code needed. A new executable was not built as part of this task.
- MSN mapping, optional app ID, authentication/timeout/rate-limit and credential-redaction contracts were tested with mock responses. Deployment with a user's legally authorized MSN key remains unverified; no website key is shipped.
- No commits or broad cleanup of other in-progress changes.
