# OOBE Actual Models Implementation Plan

**Goal:** Reuse the Settings / Endpoints / Actual Models UI during onboarding and finish only with a user-selected saved model.

**Architecture:** Split endpoint persistence from final logical-model/default binding. Embed ActualModelsPage unchanged in OobeDemo; the live gate connects its existing intents to the same settings/discovery/probe services as normal Settings. Use a bounded executor, endpoint rate limiting, cancellable request generations and queued Qt signals to avoid stale writes after navigation or closing. Preview remains simulated and never persists or makes network requests.

**Tech Stack:** Python, PySide6, pytest-qt.

## Tasks
1. Add failing regression tests covering endpoint-only save, explicit default selection, manual fallback, capabilities, probe progress, stale callbacks, navigation and shutdown.
2. Implement endpoint/model persistence split, shared model-page embedding and live controller integration. Preserve display names and existing endpoint advanced settings.
3. Verify shared settings/model pages and OOBE navigation/lifecycle; verify the existing development-only environment gates, run lint/type checks and inspect rendered layouts.

Do not change unrelated working-tree edits, commit, rebuild or restart the user's application.


## Verification results
- Initial live-flow regression tests failed before the integration; the final focused onboarding/OOBE suite has 61 passing cases.
- Broader regression: 330 passed, including development environment guards, restart lifecycle, settings panels, actual-model capabilities, discovery and probe progress.
- Ruff lint/format checks pass for all 7 changed Python files; scoped strict mypy passes for all 4 changed source files.
- Rendered and inspected dark/light populated and manual-fallback screens at 1080 x 740; inputs, shared model list, default selector and completion controls are visible.
- Verified endpoint-only save, display-name fallback, explicit default selection, capability persistence, logical-binding reuse, ID collision handling, request cancellation, stale result rejection and real worker-to-GUI delivery.
- Normal release onboarding remains available; forced debug OOBE remains restricted to development checkouts. No real provider calls, user data changes, app restart, packaging or commits.
