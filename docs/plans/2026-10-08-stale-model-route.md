# Model route refresh implementation plan

**Goal:** Never silently show a different logical model while sending to a removed or stale route.

**Architecture:** Keep the selected logical model and endpoint authoritative. Reconcile their binding against configuration before desktop sends, edits, retries and regeneration, then synchronize the runtime catalog. Missing targets are rejected without fallback, and the selector displays an explicit unavailable state. Preserve unchanged thinking trials and endpoint overrides.

**Tech Stack:** Python, PySide6, pytest, existing Pi RPC model catalog.

## Steps
1. Add regression tests in `tests/ui/test_model_selector.py` and `tests/ui/test_model_selector_routing.py` for removed models, removed endpoints/bindings, same-endpoint rebinding, and unchanged trials.
2. Run those tests and confirm the failures.
3. Update `src/limbowave/ui/chat_view.py` to show an unavailable selection instead of silently selecting the first model.
4. Update `src/limbowave/app.py` to reconcile setup with configuration at the common desktop send boundary and route retry/edit/regenerate through it. Do not fall back to another endpoint or logical model.
5. Run focused UI, routing, coordinator and catalog tests, then lint and a broader suite. Use only local/mock providers; do not change user configuration or make billable requests.

Existing uncommitted work is retained. No commit or application restart is part of this repair.

## Verification
- The initial regression run reproduced the misleading first-model selection and missing selection signal (13 failing tests before implementation).
- Focused route/controller/catalog coverage: 187 passed before the final unrouted-controller compatibility adjustment.
- Final selector, real-controller reselection/send, branch/regeneration and queue coverage: 64 passed.
- Ruff passed for both changed source files and all changed regression tests; mypy passed for the two source files.
- Rendered and inspected the unavailable-model state using a local offscreen Qt fixture; no live provider request was made.
- Broader suite also reports unrelated failures, including hard-coded fonts in existing OOBE demo code and offscreen checkbox rendering. The branch/regeneration regression discovered during that run was fixed and is included in the final 64 passing UI tests.
- Pi RPC route guard and model-catalog hot-reload integration: 4 passed (local mock provider).
- The broad suite was stopped after unrelated failures (around 80%); it was not a clean/full-suite pass. Only this task's pytest process was stopped; the user's running app was left untouched.
