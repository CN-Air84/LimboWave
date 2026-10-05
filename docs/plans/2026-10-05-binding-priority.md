# Logical Binding Priority Implementation Plan

**Goal:** Enlarge the binding list and make each logical model’s ordered bindings the only source of endpoint priority and default routing.

**Architecture:** The first binding is always the default endpoint. Remove the independent default_binding field and setter; legacy config values are ignored on load and omitted on save. Reordering, adding, replacing and deleting bindings all use the same ordered list. Session-level explicit overrides remain supported.

**Tech Stack:** Python, Pydantic, PySide6, pytest/pytest-qt.

### Steps
1. Update priority tests to assert routing to the first item, including legacy configuration, empty bindings, removal and session overrides.
2. Remove default_binding from the domain and settings service; route to bindings[0]. Remove the separate default button, update stars and copy, and align the toolbar with the same order.
3. Preserve per-model drag sorting, immediate persistence, validation and failure rollback.
4. Update configuration documentation; run related unit/UI tests, Ruff and Mypy.

Preserve unrelated workspace changes. Do not commit.
