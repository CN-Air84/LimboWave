"""Quiet dark surfaces and a viewport-anchored no-wallpaper fallback."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from limbowave.domain.appearance import BUILTIN_THEMES
from limbowave.ui import theme
from limbowave.ui.main_window import MainWindow


@pytest.fixture(autouse=True)
def restore_theme(qapp):
    old_css = qapp.styleSheet()
    yield
    theme.set_palette("dark")
    qapp.setStyleSheet(old_css)


@pytest.mark.parametrize("legacy", [True, False])
def test_default_dark_has_tonal_surfaces_and_quiet_borders(legacy):
    if legacy:
        theme.set_palette("dark")
    else:
        theme.apply_appearance_theme(BUILTIN_THEMES[0])
    assert theme.BG_APP.upper() == "#0B0C0E"
    assert theme.sidebar_surface().upper() == "#111317"
    assert theme.card_surface().upper() == "#16181D"
    assert theme.container_border() == "rgba(255, 255, 255, 15)"
    assert theme.container_border(hovered=True) == "rgba(255, 255, 255, 20)"
    assert "border: none" in theme.bubble_stylesheet("assistant")
    assert f"background: {theme.card_surface()};" in theme.bubble_stylesheet("assistant")
    assert QColor(theme.BORDER).isValid()  # painter tokens remain QColor compatible


def test_window_containers_share_soft_border(qtbot, qapp):
    theme.apply_appearance_theme(BUILTIN_THEMES[0])
    qapp.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_appearance_theme(BUILTIN_THEMES[0])
    assert theme.sidebar_surface() in window.sidebar.styleSheet()
    assert theme.container_border() in window.chat._composer.styleSheet()
    assert theme.container_border() in window.toolbar.styleSheet()
    window.chat._composer._set_drag_active(True)
    assert theme.ACCENT in window.chat._composer.styleSheet()
    assert f"border: 2px solid {theme.ACCENT}" in theme.app_stylesheet()


@pytest.mark.parametrize("target", ["light", "dark"])
def test_inline_container_border_tracks_palette_switch(qtbot, target):
    theme.set_palette("dark" if target == "light" else "light")
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.setStyleSheet(f"border: 1px solid {theme.container_border()};")
    previous = theme.current_palette()
    sizes = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
    theme.set_palette(target)
    theme.refresh_inline_styles(widget, previous, sizes)
    assert widget.styleSheet() == f"border: 1px solid {theme.container_border()};"


def test_ambient_is_static_subtle_and_anchored_to_viewport(qtbot):
    from limbowave.ui.ambient_background import paint_ambient_background

    theme.set_palette("dark")
    canvas = QWidget()
    canvas.resize(1000, 700)
    qtbot.addWidget(canvas)
    child = QWidget(canvas)
    child.setGeometry(0, -120, 1000, 1000)

    def render(widget):
        image = QImage(widget.size(), QImage.Format.Format_ARGB32)
        image.fill(QColor(theme.BG_APP))
        painter = QPainter(image)
        paint_ambient_background(widget, painter, canvas=canvas)
        painter.end()
        return image

    full, scrolled = render(canvas), render(child)
    assert full == render(canvas)
    assert full.pixelColor(80, 70) == scrolled.pixelColor(80, 190)
    assert full.pixelColor(80, 70) != QColor(theme.BG_APP)
    base = QColor(theme.BG_APP)
    for point in (QPoint(80, 70), QPoint(500, 350), QPoint(900, 600)):
        color = full.pixelColor(point)
        assert (
            max(abs(a - b) for a, b in zip(color.getRgb()[:3], base.getRgb()[:3], strict=True))
            <= 20
        )


@pytest.mark.parametrize("wallpaper,light", [(True, False), (False, True)])
def test_ambient_never_overlays_wallpaper_or_light_theme(qtbot, wallpaper, light):
    from limbowave.ui.ambient_background import paint_ambient_background

    definition = BUILTIN_THEMES[1 if light else 0]
    if wallpaper:
        definition = replace(
            definition, background=replace(definition.background, asset="wall.png")
        )
    theme.apply_appearance_theme(definition)
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.resize(300, 200)
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(QColor("#553322"))
    original = image.copy()
    painter = QPainter(image)
    paint_ambient_background(widget, painter)
    painter.end()
    assert image == original


def test_dark_surfaces_are_visible_in_composited_window(qtbot, qapp):
    theme.apply_appearance_theme(BUILTIN_THEMES[0])
    qapp.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.set_appearance_theme(BUILTIN_THEMES[0])
    window.show()
    qtbot.wait(30)
    image = window.grab().toImage()
    sidebar = image.pixelColor(window.sidebar.mapTo(window, QPoint(5, 350)))
    assert sidebar == QColor("#111317")
    composer = window.chat._composer
    plate = image.pixelColor(composer.mapTo(window, QPoint(8, composer.height() // 2)))
    assert plate == QColor("#16181D")
    top = window.chat.mapTo(window, QPoint(70, 70))
    assert image.pixelColor(top) != QColor(theme.BG_APP)
    assert window._backdrop_engine.render_count == 0


def test_floating_panel_does_not_restore_a_bright_outline(qtbot, qapp):
    from limbowave.ui.floating import FloatingPanel

    theme.set_palette("dark")
    qapp.setStyleSheet(theme.app_stylesheet())
    host = QWidget()
    host.resize(600, 400)
    qtbot.addWidget(host)
    panel = FloatingPanel(host, "Panel")
    panel.resize(400, 220)
    host.show()
    panel.show()
    image = panel.grab().toImage()
    edge = image.pixelColor(image.width() // 2, 0)
    base = QColor(theme.BG_SURFACE)
    assert edge.alpha() == 255
    for a, b in zip(edge.getRgb()[:3], base.getRgb()[:3], strict=True):
        assert 0 <= a - b <= 20


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_assistant_background_is_painted_with_rounded_corners(qtbot, qapp, palette):
    from limbowave.ui.chat_view import ChatView

    theme.set_palette(palette)
    qapp.setStyleSheet(theme.app_stylesheet())
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1000, 700)
    view.show()
    view.load_history([("assistant", "回复背景板", "", "m1")])
    panel = view._rows[0]._assistant_panel
    qtbot.waitUntil(lambda: panel.isVisible() and panel.width() == view._column_width)
    image = view.grab().toImage()
    # 顶部留白不受正文覆盖；角落则应露出卡片后面的聊天底色。
    top = panel.mapTo(view, QPoint(panel.width() // 2, 4))
    corner = panel.mapTo(view, QPoint(0, 0))
    assert image.pixelColor(top) == QColor(theme.BG_SURFACE)
    assert image.pixelColor(corner) != QColor(theme.BG_SURFACE)


def test_existing_assistant_background_refreshes_with_palette_and_material(qtbot, qapp):
    from limbowave.ui.chat_view import ChatView

    base = BUILTIN_THEMES[0]
    wallpaper = replace(
        base,
        background=replace(base.background, asset="wall.png"),
        materials=replace(base.materials, content_opacity=0.6),
    )
    opaque_wallpaper = replace(
        wallpaper, materials=replace(wallpaper.materials, cards_enabled=False)
    )
    theme.apply_appearance_theme(base)
    qapp.setStyleSheet(theme.app_stylesheet())
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history([("assistant", "已加载的回复", "", "m1")])
    row = view._rows[0]
    panel = row._assistant_panel
    for definition in (wallpaper, opaque_wallpaper, BUILTIN_THEMES[1], base):
        previous = theme.current_palette()
        sizes = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
        old_card = theme.card_surface()
        theme.apply_appearance_theme(definition)
        qapp.setStyleSheet(theme.app_stylesheet())
        theme.refresh_inline_styles(view, previous, sizes, old_card_surface=old_card)
        assert f"background: {theme.card_surface()};" in panel.styleSheet()
        assert f"color: {theme.TEXT_PRIMARY};" in panel.styleSheet()
        assert f"border-radius: {theme.RADIUS_BUBBLE}px;" in panel.styleSheet()
        assert view._rows[0] is row
        assert row.content_text() == "已加载的回复"
