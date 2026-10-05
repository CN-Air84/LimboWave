# Endpoint Unbind and Delete Implementation Plan

**Goal:** Replace the bound-endpoint error with an explicit slide-to-confirm bulk unbind and delete action.

**Architecture:** Keep deletion guarded by default. Add an explicit service flag that removes the endpoint, its actual-model catalog entries and its bindings in one validated save, preserving logical models and surviving default routes. Share the toolbar slider with a dedicated deletion panel; require dragging from the handle so clicking the track cannot confirm.

**Tech Stack:** Python, PySide6, Pydantic, pytest / pytest-qt.

## Tasks
1. Add tests/unit/test_endpoint_delete_service.py: guarded deletion, one-save bulk deletion, default binding repair, unaffected settings and save failure.
2. Extend src/limbowave/application/services/settings_service.py with explicit unbind_models deletion and a shared binding-removal helper.
3. Extract src/limbowave/ui/slide_confirm.py from the existing toolbar slider, retaining its appearance and snap-back. Only handle-origin gestures can confirm.
4. Add src/limbowave/ui/endpoint_delete_panel.py: site name, affected model list, empty-route warning, cancel and slide-to-confirm. Wire settings_dialog.py to refresh only after successful deletion.
5. Add tests/ui/test_endpoint_unbind_delete.py for cancellation, direct clicks, partial/full drags, duplicate confirmation, deletion failures and long lists. Run settings, toolbar and floating-panel suites, lint and type-check touched code, and render the panel for visual inspection.

## Interaction decision
Use a single explicit “解绑并删除站点” slide instead of immediate cascading deletion or an extra unbind-only step. The panel explains that logical models remain and other sites are unaffected. No real configuration is changed during implementation or tests.

## Verification completed
- 18 new service/UI regressions passed, covering guarded deletion, a single save, default-route preservation, cancellation, track clicks, wrong-button/non-handle drags, partial drags, hidden gestures and save failures.
- Settings service/dialog/page, endpoint confirmation, toolbar and floating-panel suites: 221 passed with 2 known hover-color cases excluded. Those 2 cases fail independently and also fail with this task’s three existing-source edits removed in memory; working files were not reverted.
- Ruff, targeted mypy (5 source files), and git diff --check passed.
- Dark and light panels rendered with the installed Chinese font and visually inspected; previews are under .var/qa/endpoint-delete-{dark,light}.png.
