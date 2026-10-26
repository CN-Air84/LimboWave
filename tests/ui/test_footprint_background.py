"""Gallery chrome paints a continuous theme-aware background, not a black slab."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage

from limbowave.domain.appearance import BUILTIN_THEMES, BackgroundSettings
from limbowave.domain.footprint import MemoryFootprint
from limbowave.domain.memory import MemoryItem
from limbowave.ui import theme
from limbowave.ui.main_window import MainWindow
from limbowave.ui.memory_timeline import MemoryTimelinePage


class GalleryData:
    def timeline(self):
        return [
            MemoryFootprint(
                MemoryItem(
                    str(i),
                    f"A retained fragment {i}",
                    "user",
                    "2026-10-25T00:00:00Z",
                ),
                "global",
            )
            for i in range(12)
        ]


@pytest.fixture
def gallery_style(qapp, monkeypatch):
    # Theme setters mutate module tokens; keep other UI suites' state intact.
    for name in (
        "BG_APP",
        "BG_SURFACE",
        "BG_SURFACE_HOVER",
        "BG_ELEVATED",
        "BORDER",
        "ACCENT",
        "ACCENT_HOVER",
        "ACCENT_PRESSED",
        "TEXT_PRIMARY",
        "TEXT_SECONDARY",
        "TEXT_ON_ACCENT",
        "DANGER_BG",
        "DANGER_TEXT",
        "WARNING",
        "CODE_BG",
        "TAB_INDICATOR",
        "_current_palette",
        "_current_materials",
        "_background_active",
    ):
        monkeypatch.setattr(theme, name, getattr(theme, name))
    original = qapp.styleSheet()
    yield
    qapp.setStyleSheet(original)


def show_gallery(qtbot, qapp):
    qapp.setStyleSheet(theme.app_stylesheet())
    page = MemoryTimelinePage(GalleryData())
    qtbot.addWidget(page)
    page.resize(950, 760)
    page.show()
    qtbot.waitUntil(lambda: not page._loading)
    return page


def test_dark_gallery_is_a_soft_gradient_not_the_app_black(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    frame = page.grab().toImage()
    top = frame.pixelColor(6, 90)
    bottom = frame.pixelColor(6, page.height() - 12)
    assert top != QColor(theme.BG_APP)
    assert top != bottom
    assert top.lightness() > QColor(theme.BG_APP).lightness()
    assert bottom.lightness() > QColor(theme.BG_APP).lightness()


def test_list_is_transparent_and_header_keeps_its_reading_board(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    frame = page.grab().toImage()
    gap = page._body.mapTo(page, QPoint(2, 30))
    assert frame.pixelColor(gap) != QColor(theme.footprint_panel_surface())
    subtitle = page.findChild(type(page.summary), "footprintSubtitle")
    corner = subtitle.mapTo(page, QPoint(subtitle.width() - 2, subtitle.height() - 2))
    assert frame.pixelColor(corner) == QColor(theme.footprint_panel_surface())
    before = frame.pixelColor(6, 90)
    page._scroll.verticalScrollBar().setValue(page._scroll.verticalScrollBar().maximum())
    qtbot.wait(20)
    assert page.grab().toImage().pixelColor(6, 90) == before


@pytest.mark.parametrize("theme_id", ["builtin:clear-day", "builtin:sea-salt-cyan"])
def test_light_and_custom_gallery_keeps_its_selected_palette(
    qtbot,
    qapp,
    gallery_style,
    theme_id,
):
    definition = next(item for item in BUILTIN_THEMES if item.id == theme_id)
    theme.apply_appearance_theme(definition)
    page = show_gallery(qtbot, qapp)
    top = page.grab().toImage().pixelColor(6, 90)
    assert top.lightness() > 180
    assert abs(top.red() - QColor(definition.colors.background).red()) < 20
    assert abs(top.green() - QColor(definition.colors.background).green()) < 20


def test_full_page_uses_and_refreshes_the_existing_wallpaper(
    qtbot,
    qapp,
    tmp_path,
    gallery_style,
):
    def wallpaper(name, color):
        path = tmp_path / name
        image = QImage(600, 400, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        assert image.save(str(path))
        return str(path)

    red = wallpaper("red.png", "#dd6655")
    blue = wallpaper("blue.png", "#5577dd")
    definition = replace(BUILTIN_THEMES[0], background=BackgroundSettings(asset=red))
    theme.apply_appearance_theme(definition)
    qapp.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(950, 760)
    window.show()
    window.set_appearance_theme(definition, red)
    qtbot.waitUntil(lambda: window._backdrop_engine.active)
    page = MemoryTimelinePage(GalleryData(), window)
    window.show_full_page(page)
    qtbot.waitUntil(lambda: not page._loading)
    window._stop_transition()
    assert page._backdrop_engine is window._backdrop_engine
    first = page.grab().toImage().pixelColor(6, 90)
    assert first.red() > first.blue() + 40
    window.set_backdrop(blue, 0)
    second = page.grab().toImage().pixelColor(6, 90)
    assert second.blue() > second.red() + 40
    renders = window._backdrop_engine.render_count
    page._scroll.verticalScrollBar().setValue(400)
    qtbot.wait(20)
    assert window._backdrop_engine.render_count == renders
    window.show_workspace()
    page.dispose()


def test_background_reacts_to_live_palette_changes(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    dark = page.grab().toImage().pixelColor(6, 90)
    definition = next(item for item in BUILTIN_THEMES if item.id == "builtin:sea-salt-cyan")
    theme.apply_appearance_theme(definition)
    qapp.setStyleSheet(theme.app_stylesheet())
    light = page.grab().toImage().pixelColor(6, 90)
    assert light != dark and light.lightness() > 180
    assert len(page.cards) == 12


def test_missing_wallpaper_falls_back_without_a_black_flash(
    qtbot,
    qapp,
    gallery_style,
    tmp_path,
):
    definition = replace(
        BUILTIN_THEMES[0],
        background=BackgroundSettings(asset=str(tmp_path / "missing.png")),
    )
    theme.apply_appearance_theme(definition)
    page = show_gallery(qtbot, qapp)
    assert page.grab().toImage().pixelColor(6, 90) != QColor(theme.BG_APP)
