# OOBE Display Name Implementation Plan

**Goal:** Let users name preset and custom providers during OOBE, and persist that name.

**Architecture:** Add a field to the existing PySide6 form, retain drafts per provider, and snapshot its value with each connection check. Pass the optional display name separately from provider identity into onboarding persistence; blank names retain the existing preset/hostname fallback. Reuse existing field styling and accessible labels.

**Tech Stack:** Python, PySide6, pytest/pytest-qt.

## Tasks
1. Add regression tests for visible/default names, navigation retention, blank-name fallback, persistence and asynchronous check snapshots. Run them before implementation.
2. Update `oobe_copy.py`, `oobe_demo.py`, `oobe_live.py` and `application/services/onboarding.py`. Keep provider IDs, protocols, credentials and URL validation unchanged.
3. Run targeted tests, Ruff and scoped mypy; render both forms in light/dark themes and inspect their layout.

Preserve all unrelated working-tree changes. Do not commit or restart the user's application.

## Verification results
- Confirmed all 11 new regression cases failed before implementation; all 45 focused onboarding/OOBE tests pass after the change.
- Broader onboarding, restart, model-discovery, settings-service and settings-dialog regression: 246 passed.
- Ruff lint/format checks pass for all 7 changed Python files; scoped strict mypy passes for all 4 changed source files.
- Rendered and inspected preset/custom forms in both dark and light themes using offscreen Qt and an explicitly loaded Chinese font. All inputs and action buttons remain visible.
- Connection checks were mocked and persistence used temporary test vaults. No real provider requests, user configuration changes, application restart or packaging were performed.
