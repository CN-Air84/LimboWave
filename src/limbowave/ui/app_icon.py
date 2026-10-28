"""Shared runtime icon, bundled alongside the other UI assets."""

from pathlib import Path

from PySide6.QtGui import QIcon

APP_ICON_PATH = Path(__file__).with_name("assets") / "limbowave.ico"


def application_icon() -> QIcon:
    return QIcon(str(APP_ICON_PATH))
