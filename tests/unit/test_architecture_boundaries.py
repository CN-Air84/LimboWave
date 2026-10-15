"""Executable inward dependency rules, including relative and package-level imports.

AST scanning covers imports in every module, including TYPE_CHECKING and local
imports. It is not a proof about dynamic importlib calls or third-party transitive
imports. Legacy exceptions are exact, explained, and removed as soon as unused.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "limbowave"
GUARDED_ROOTS = (SRC / "domain", SRC / "application")
FORBIDDEN_ROOTS = frozenset({"PySide6", "PyQt6", "PyQt5", "qasync", "shiboken6"})

# These existing dependencies require separate lifecycle/format migrations.
# Never allow an entire layer or add an exception merely to make a test pass.
LEGACY_INFRASTRUCTURE_IMPORTS = {
    "application/services/backup_service.py": {
        "limbowave.infrastructure.backup.archive": "Backup archive format orchestration",
        "limbowave.infrastructure.crypto.vault": "Backup key document and KDF format",
    },
    "application/services/data_reset_service.py": {
        "limbowave.infrastructure.crypto.recovery": "Platform recovery/Hello gate",
        "limbowave.infrastructure.crypto.vault": "Vault reset lifecycle",
    },
    "application/services/restore_service.py": {
        "limbowave.infrastructure.configuration.json_config_repository": (
            "Restored config validation"
        ),
        "limbowave.infrastructure.crypto.blob_store": (
            "Re-encryption into the replacement library"
        ),
        "limbowave.infrastructure.crypto.secret_store": "Credential re-encryption",
        "limbowave.infrastructure.crypto.vault": "Vault/key document restore",
        "limbowave.infrastructure.database.migrations": "Restored database schema migration",
    },
    "application/services/session_controller.py": {
        "limbowave.infrastructure.memory_repositories": "Existing optional in-memory default",
    },
}


def _iter_modules() -> list[Path]:
    return [path for root in GUARDED_ROOTS for path in sorted(root.rglob("*.py"))]


def _imported_names(tree: ast.AST, module: str) -> set[str]:
    """Resolve from-import names too, so `from limbowave import ui` is visible."""
    package = module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            names.update(
                base if alias.name == "*" else f"{base}.{alias.name}" for alias in node.names
            )
    return names


def _dependencies(path: Path) -> set[str]:
    module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
    return _imported_names(ast.parse(path.read_text(encoding="utf-8")), module)


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _violates_layer(source: str, dependency: str) -> bool:
    if not dependency.startswith("limbowave."):
        return False
    allowed = ("limbowave.domain",)
    if source.startswith("application/"):
        allowed += ("limbowave.application",)
    return not any(_under(dependency, prefix) for prefix in allowed)


def test_guarded_layers_are_not_empty() -> None:
    files = _iter_modules()
    assert len(files) >= 10, f"Too few guarded modules: {len(files)}"
    assert {"run_coordinator.py", "conversation.py"} <= {path.name for path in files}


@pytest.mark.parametrize("path", _iter_modules(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_layer_does_not_import_qt(path: Path) -> None:
    offenders = {name.split(".")[0] for name in _dependencies(path)} & FORBIDDEN_ROOTS
    assert not offenders, f"{path.relative_to(SRC)} cannot depend on {sorted(offenders)}"


@pytest.mark.parametrize("path", _iter_modules(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_layers_depend_inward(path: Path) -> None:
    source = path.relative_to(SRC).as_posix()
    exceptions = LEGACY_INFRASTRUCTURE_IMPORTS.get(source, {})
    offenders = {
        name
        for name in _dependencies(path)
        if _violates_layer(source, name) and not any(_under(name, prefix) for prefix in exceptions)
    }
    assert not offenders, f"{source} has outward dependencies: {sorted(offenders)}"


def test_legacy_exceptions_are_specific_documented_and_still_used() -> None:
    for source, exceptions in LEGACY_INFRASTRUCTURE_IMPORTS.items():
        assert source.startswith("application/services/")
        dependencies = _dependencies(SRC / source)
        for prefix, reason in exceptions.items():
            assert prefix.startswith("limbowave.infrastructure.") and prefix.count(".") >= 2
            module_path = SRC.parent.joinpath(*prefix.split(".")).with_suffix(".py")
            assert module_path.is_file(), f"Exception must name a concrete module: {prefix}"
            assert reason.strip(), f"Missing rationale: {source}: {prefix}"
            assert any(_under(name, prefix) for name in dependencies), (
                f"Remove obsolete exception: {source}: {prefix}"
            )


@pytest.mark.parametrize(
    ("code", "module", "expected"),
    [
        ("import PySide6.QtCore as qt", "limbowave.domain.example", {"PySide6.QtCore"}),
        ("from PySide6 import QtCore", "limbowave.domain.example", {"PySide6.QtCore"}),
        ("from limbowave import ui", "limbowave.domain.example", {"limbowave.ui"}),
        ("from .. import ui", "limbowave.domain.example", {"limbowave.ui"}),
        ("from ..ui import theme", "limbowave.domain.example", {"limbowave.ui.theme"}),
        (
            "from ...infrastructure import memory_repositories",
            "limbowave.application.services.example",
            {"limbowave.infrastructure.memory_repositories"},
        ),
        (
            "from .files import FileDocument",
            "limbowave.domain.__init__",
            {"limbowave.domain.files.FileDocument"},
        ),
        ("from limbowave.ui import *", "limbowave.domain.example", {"limbowave.ui"}),
        (
            "def local():\n    from limbowave.ui import theme",
            "limbowave.application.example",
            {"limbowave.ui.theme"},
        ),
        (
            "if TYPE_CHECKING:\n    from limbowave.ui import theme",
            "limbowave.application.example",
            {"limbowave.ui.theme"},
        ),
    ],
)
def test_import_scanner(code: str, module: str, expected: set[str]) -> None:
    assert _imported_names(ast.parse(code), module) == expected


@pytest.mark.parametrize(
    ("source", "dependency", "violates"),
    [
        ("domain/example.py", "limbowave.application.repositories", True),
        ("domain/example.py", "limbowave.infrastructure.crypto", True),
        ("domain/example.py", "limbowave.domain.files", False),
        ("domain/example.py", "limbowave.domainish", True),
        ("application/example.py", "limbowave.ui.theme", True),
        ("application/example.py", "limbowave.server.composition", True),
        ("application/example.py", "limbowave.web.dto", True),
        ("application/example.py", "limbowave.app", True),
        ("application/example.py", "limbowave.application.repositories", False),
        ("application/example.py", "limbowave.domain.files", False),
        ("application/example.py", "pathlib.Path", False),
    ],
)
def test_dependency_direction(source: str, dependency: str, violates: bool) -> None:
    assert _violates_layer(source, dependency) is violates
