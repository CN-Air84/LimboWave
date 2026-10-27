"""The shipped night wallpaper uses the existing shared backdrop renderer."""

from pathlib import Path

from PySide6.QtGui import QColor
from pytestqt.qtbot import QtBot

from limbowave.application.services.appearance_theme_service import AppearanceThemeService
from limbowave.domain.appearance import BUILTIN_THEMES
from limbowave.ui import theme
from limbowave.ui.main_window import MainWindow


def test_night_wallpaper_loads_and_clears_on_theme_switch(qtbot: QtBot, qapp, tmp_path: Path):
    service = AppearanceThemeService(tmp_path / "themes", legacy_theme="dark")
    definition = service.active_theme
    image_path = service.resolve_asset(definition.background.asset)
    assert image_path is not None
    old_css = qapp.styleSheet()
    window = None
    try:
        theme.apply_appearance_theme(definition)
        qapp.setStyleSheet(theme.app_stylesheet())
        window = MainWindow()
        qtbot.addWidget(window)
        window.resize(1000, 700)
        window.show()
        window.set_appearance_theme(definition, str(image_path))
        qtbot.waitUntil(lambda: window._backdrop_engine.active, timeout=5000)
        assert window._backdrop_engine.render_count > 0
        assert window._background_image == str(image_path)
        frame = window.grab().toImage()
        assert not frame.isNull()
        assert frame.pixelColor(500, 350) != QColor(definition.colors.background)

        light = BUILTIN_THEMES[1]
        theme.apply_appearance_theme(light)
        qapp.setStyleSheet(theme.app_stylesheet())
        window.set_appearance_theme(light)
        assert not window._backdrop_engine.active
        assert window._background_image == ""

        theme.apply_appearance_theme(definition)
        qapp.setStyleSheet(theme.app_stylesheet())
        window.set_appearance_theme(definition, str(image_path))
        qtbot.waitUntil(lambda: window._backdrop_engine.active, timeout=5000)
    finally:
        if window is not None:
            window.close()
        theme.set_palette("dark")
        qapp.setStyleSheet(old_css)
