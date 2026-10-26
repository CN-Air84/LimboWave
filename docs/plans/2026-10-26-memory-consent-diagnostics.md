# Memory mutation scope diagnostics and real consent popup regression

**Evidence:** The reported memory ID was returned by the saved `search_memory` result with `scope: global`. Both mutation tools failed before the dialog because `prepare_mutation` only accepts current-branch session memories. The app swallowed that ValueError and the extension mislabeled every rejection as an authorization failure.

**Fix:** Preserve the documented scope and consent boundary. Return explicit model-writability metadata in memory search. Before asking for mutation consent, resolve the exact ID through the existing run-bound read-only search tool; return a concrete read-only/missing-target error rather than an authorization error. Valid session mutations still use the existing one-shot approval handler and revalidate at execution. Add content-free stage diagnostics for unexpected gate failures.

**Tests:** Reproduce the global-memory misuse through real Pi and assert the final visible error explains the scope, no misleading permission wording, no mutation, and no consent request for an unsupported operation. Exercise real FloatingPanel UI under FULL_ACCESS for update/archive, including allow and deny; do not mock the dialog. Run existing memory and remote boundary regressions.

## Result

- Confirmed the reported target was `scope: global` from the matching saved search result; no saved memory content was modified during diagnosis.
- Added `model_writable` to search rows and a run-bound, read-only exact-ID preflight in the Pi extension. Unsupported scope now produces a visible scope error with the management path and “未提交审批，也未执行”, rather than a fake authorization denial. This does not expand model write permissions.
- Added content-free approval failure stage diagnostics.
- Verified **12 real FloatingPanel cases** under FULL_ACCESS (update/archive/delete × allow/deny × two repositories), as part of **114 passing UI/unit checks**. No mocked confirmation dialog in the new popup tests.
- Active-memory real Pi tests: **6 passed**, including both global-memory misuse cases and their visible error text. Existing memory/bridge/remote-boundary integration regressions: **239 passed**.
- Related Ruff checks and mypy checks passed. Captured the actual confirmation popup with synthetic data at `.tmp/active-memory-qa/actual-update-confirmation.png`; the popup was closed without approving the sample mutation.

> Subsequent user request: global `update_memory` proposals are now explicitly allowed with per-call global-impact consent. The earlier global-read-only restriction in this diagnostic plan is superseded by `2026-10-26-global-memory-update-consent.md`; session-only deletion and concrete unsupported-target errors remain.
