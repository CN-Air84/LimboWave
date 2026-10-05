# Logical Model Name Ordering Implementation Plan

**Goal:** Sort model display names alphabetically in the composer submenus and settings list; keep GLM/GPT uppercase in ID-derived names.

**Architecture:** Use shared pure display-name helpers with existing pypinyin for Chinese alphabetical ordering. Sort only view projections, preserving configuration order, stable logical IDs, default model, binding priority, selection and unsaved fields.

**Tech Stack:** Python, PySide6, pypinyin, pytest/pytest-qt.

## Steps
1. Add failing unit tests for case-insensitive alphabetical/pinyin ordering and ID-derived GLM/GPT capitalization.
2. Add UI tests for sorted menu actions and settings refresh/save/rename behavior, including preserving selected IDs and user-entered names.
3. Implement src/limbowave/domain/model_display.py; use its key in src/limbowave/ui/model_selector.py and src/limbowave/ui/logical_models_tab.py. Keep first-level brand order unchanged.
4. Update existing selector ordering assertions; extend settings auto-name cases. Run focused new tests plus existing selector/chat/settings/binding-priority tests and static checks. Do not commit or modify unrelated work.

## Verification
- 277 related tests passed (new display/order cases, selector and app routing, settings dialog/panel, binding priority, chat view and settings service).
- Ruff passed on all touched Python files; focused mypy passed on the helper, selector and logical-model settings modules.
- Existing multi-select deletion test now selects stable IDs rather than assuming configuration insertion order matches display order.
- Sorting remains display-only: persisted model order, default model, binding priority, current selection and unsaved manual names are preserved.
