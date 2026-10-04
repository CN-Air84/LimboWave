"""Register installed or user-supplied fonts without changing Qt's global font DB repeatedly."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFontDatabase

_loaded: dict[str, list[str]] = {}


def family_for_file(path: str) -> list[str]:
    if not path or Path(path).suffix.lower() not in {".ttf", ".otf", ".ttc"}:
        return []
    try:
        resolved = str(Path(path).resolve(strict=True))
    except (OSError, ValueError):
        return []
    try:
        if Path(resolved).stat().st_size > 16 * 1024 * 1024:
            return []
    except (OSError, ValueError):
        return []
    if resolved not in _loaded:
        font_id = QFontDatabase.addApplicationFont(resolved)
        _loaded[resolved] = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    return _loaded[resolved]


def select_family(family: str, file: str = "") -> str:
    available = family_for_file(file) if file else QFontDatabase.families()
    return family if family in available else ""
