"""架构边界守卫：领域层与应用层不得依赖 Qt。

为什么用 AST 静态扫描而不是运行时 ``sys.modules`` 检查：静态扫描能覆盖**所有**文件
（包括将来新增的），不依赖某个模块恰好被执行到；运行时检查只能证明"这条路径没导入"。

这条边界的意义（ADR-0001）：只有 GUI 依赖 Qt，业务规则与运行编排必须能在无 Qt 环境下
被测试与复用。一旦有人把 ``QMessageBox`` 写进协调器，这里会立刻失败。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "limbowave"

# 受守卫的层：领域规则 + 应用编排。GUI 与基础设施可以依赖 Qt。
GUARDED_ROOTS = (SRC / "domain", SRC / "application")

FORBIDDEN_ROOTS = frozenset({"PySide6", "PyQt6", "PyQt5", "qasync", "shiboken6"})


def _iter_modules() -> list[Path]:
    files: list[Path] = []
    for root in GUARDED_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_guarded_layers_are_not_empty() -> None:
    """守卫本身要有效：扫不到文件时必须失败，而不是静默通过。"""
    files = _iter_modules()
    assert len(files) >= 10, f"受守卫的文件太少（{len(files)}），守卫可能已失效"
    names = {p.name for p in files}
    assert "run_coordinator.py" in names
    assert "conversation.py" in names


@pytest.mark.parametrize("path", _iter_modules(), ids=lambda p: p.name)
def test_layer_does_not_import_qt(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = _imported_roots(tree) & FORBIDDEN_ROOTS
    assert not offenders, f"{path.relative_to(SRC)} 不允许依赖 {sorted(offenders)}"
