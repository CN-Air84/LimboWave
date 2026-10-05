"""Real popup pixels and shared-frame performance contracts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from pytestqt.qtbot import QtBot

from limbowave.domain.appearance import BUILTIN_THEMES
from limbowave.ui import theme
from limbowave.ui.attachment_bar import AttachmentMenu
from limbowave.ui.chat_view import _PermissionMenu
from limbowave.ui.floating import FloatingPanel
from limbowave.ui.main_window import MainWindow
from limbowave.ui.popup_motion import UpwardComboBox


@pytest.fixture(params=["builtin:limbowave-dark", "builtin:clear-day"])
def workspace(request: pytest.FixtureRequest, qtbot: QtBot, tmp_path: Path) -> Iterator[MainWindow]:
    source = next(item for item in BUILTIN_THEMES if item.id == request.param)
    path = tmp_path / "stripes.png"
    image = Image.new("RGB", (640, 480))
    for x in range(image.width):
        for y in range(image.height):
            image.putpixel((x, y), (240, y // 2, 30) if x % 16 < 8 else (30, y // 2, 240))
    image.save(path)
    definition = replace(
        source,
        background=replace(source.background, asset=str(path), fit_mode="stretch"),
        materials=replace(source.materials, content_opacity=0.35, content_blur_radius=12),
    )
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    old_css = app.styleSheet()
    effects = {
        effect: app.isEffectEnabled(effect)
        for effect in (
            Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateMenu,
        )
    }
    for effect in effects:
        app.setEffectEnabled(effect, False)
    theme.apply_appearance_theme(definition)
    app.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.set_appearance_theme(definition, str(path))
    qtbot.waitUntil(lambda: window._backdrop_engine.active, timeout=3000)
    qtbot.wait(180)
    try:
        yield window
    finally:
        window.close()
        theme.apply_appearance_theme(BUILTIN_THEMES[0])
        app.setStyleSheet(old_css)
        for effect, enabled in effects.items():
            app.setEffectEnabled(effect, enabled)


def _expected(window: MainWindow, widget: QWidget, point: QPoint, *, card: bool = False) -> QColor:
    definition = window._appearance_theme
    engine = window._backdrop_engine
    radius = min(engine._frames, key=lambda value: abs(value - 12))
    frame = engine._frames[radius].toImage()
    position = window._workspace.mapFromGlobal(widget.mapToGlobal(point))
    x = round(position.x() * frame.width() / window._workspace.width())
    y = round(position.y() * frame.height() / window._workspace.height())
    assert 0 <= x < frame.width() and 0 <= y < frame.height(), (
        widget.geometry(),
        widget.window().geometry(),
        widget.mapToGlobal(QPoint(0, 0)),
        window._workspace.geometry(),
        window._workspace.mapToGlobal(QPoint(0, 0)),
        point,
    )
    pixel = frame.pixelColor(x, y)
    tint = QColor(definition.colors.card if card else definition.colors.component)
    alpha = round(definition.materials.content_opacity * 255) / 255
    return QColor(
        *(
            round(original * (1 - alpha) + overlay * alpha)
            for original, overlay in zip(pixel.getRgb()[:3], tint.getRgb()[:3], strict=True)
        )
    )


def _assert_pixel(actual: QColor, expected: QColor) -> None:
    assert all(abs(a - b) <= 3 for a, b in zip(actual.getRgb(), expected.getRgb(), strict=True)), (
        actual.name(),
        expected.name(),
    )


def _combo(window: MainWindow) -> UpwardComboBox:
    combo = UpwardComboBox(window._content_host)
    combo.setGeometry(450, 350, 220, 40)
    combo.addItems(["first", "second", "third", "fourth"])
    combo.show()
    return combo


def test_combo_popup_paints_shared_blur(workspace: MainWindow, qtbot: QtBot) -> None:
    combo = _combo(workspace)
    combo.showPopup()
    qtbot.wait(20)
    viewport = combo.view().viewport()
    row = combo.view().visualRect(combo.model().index(2, 0))
    point = QPoint(viewport.width() - 8, row.center().y())
    actual = viewport.grab().toImage().pixelColor(point)
    _assert_pixel(actual, _expected(workspace, viewport, point))
    combo.hidePopup()


@pytest.mark.parametrize("menu_type", [AttachmentMenu, _PermissionMenu])
def test_menu_paints_shared_blur(
    workspace: MainWindow, qtbot: QtBot, menu_type: type[QMenu]
) -> None:
    anchor = _combo(workspace)
    menu = menu_type(workspace._workspace)
    menu.popup_above(anchor)  # type: ignore[attr-defined]
    qtbot.wait(20)
    row = menu.actionGeometry(menu.actions()[0])
    point = QPoint(menu.width() - 8, row.center().y())
    _assert_pixel(menu.grab().toImage().pixelColor(point), _expected(workspace, menu, point))
    menu.hide()


@pytest.mark.parametrize("menu_type", [AttachmentMenu, _PermissionMenu])
@pytest.mark.parametrize("animated", [False, True])
@pytest.mark.parametrize("material", [False, True])
def test_menu_round_corners_from_first_open(
    workspace: MainWindow,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    menu_type: type[AttachmentMenu | _PermissionMenu],
    animated: bool,
    material: bool,
) -> None:
    if not material:
        workspace.set_backdrop("", 0)
    monkeypatch.setattr(
        QApplication,
        "isEffectEnabled",
        staticmethod(lambda effect: animated and effect == Qt.UIEffect.UI_AnimateMenu),
    )
    anchor = _combo(workspace)
    menu = menu_type(workspace._workspace)
    # QSS only rounds the painted background. The native surface needs alpha
    # before its first show, not after a paint/grab has warmed up the menu.
    assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def assert_pixels() -> None:
        image = menu.grab().toImage()
        for x in (0, image.width() - 1):
            for y in (0, image.height() - 1):
                assert image.pixelColor(x, y).alpha() == 0
        row = menu.actionGeometry(menu.actions()[2])
        point = QPoint(menu.width() - 8, row.center().y())
        scale = image.devicePixelRatio()
        actual = image.pixelColor(round(point.x() * scale), round(point.y() * scale))
        expected = _expected(workspace, menu, point) if material else QColor(theme.BG_SURFACE)
        _assert_pixel(actual, expected)

    for _ in range(2):
        menu.popup_above(anchor)
        if animated:
            animation = menu._motion._reveal_anim
            assert animation is not None
            animation.pause()
            animation.setCurrentTime(animation.duration() // 2)
            qtbot.wait(20)
            assert_pixels()
            animation.resume()
            qtbot.waitUntil(lambda: menu._motion._reveal_anim is None, timeout=1000)
        else:
            qtbot.wait(20)
            assert menu._motion._reveal_anim is None
        assert_pixels()
        menu.hide()
        if animated:
            ghost = menu._motion._ghost
            assert ghost is not None
            pixels = ghost._pixmap.toImage()
            assert pixels.pixelColor(0, 0).alpha() == 0
            assert pixels.pixelColor(pixels.width() - 1, pixels.height() - 1).alpha() == 0
            qtbot.waitUntil(lambda: menu._motion._ghost is None, timeout=1000)


@pytest.mark.parametrize("menu_type", [AttachmentMenu, _PermissionMenu])
def test_menu_round_corners_survive_interrupted_reveal(
    workspace: MainWindow,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    menu_type: type[AttachmentMenu | _PermissionMenu],
) -> None:
    monkeypatch.setattr(
        QApplication,
        "isEffectEnabled",
        staticmethod(lambda effect: effect == Qt.UIEffect.UI_AnimateMenu),
    )
    anchor = _combo(workspace)
    menu = menu_type(workspace._workspace)
    menu.popup_above(anchor)
    animation = menu._motion._reveal_anim
    assert animation is not None
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    menu.hide()
    assert menu._motion._reveal_anim is None
    assert menu._motion._ghost is not None

    # Reopen before the exit ghost finishes; neither its mask nor its pixels
    # should leak into the new reveal.
    menu.popup_above(anchor)
    assert menu._motion._ghost is None
    qtbot.waitUntil(lambda: menu._motion._reveal_anim is None, timeout=1000)
    assert menu.mask().isEmpty()
    pixels = menu.grab().toImage()
    for x in (0, pixels.width() - 1):
        for y in (0, pixels.height() - 1):
            assert pixels.pixelColor(x, y).alpha() == 0
    menu.hide()
    qtbot.waitUntil(lambda: menu._motion._ghost is None, timeout=1000)


def test_floating_panel_paints_shared_blur(workspace: MainWindow, qtbot: QtBot) -> None:
    panel = FloatingPanel(workspace, "Material")
    panel.setFixedSize(420, 200)
    panel.popup()
    qtbot.waitUntil(lambda: panel._motion is None, timeout=1000)
    point = QPoint(18, panel.height() - 18)
    _assert_pixel(
        panel.grab().toImage().pixelColor(point), _expected(workspace, panel, point, card=True)
    )
    panel.close_panel()


@pytest.mark.parametrize("nested", [False, True])
def test_material_panel_keeps_outline_and_external_shadow(
    workspace: MainWindow, qtbot: QtBot, nested: bool,
) -> None:
    parent: QWidget = workspace
    if nested:
        outer = FloatingPanel(workspace, "Outer")
        outer.setFixedSize(620, 380)
        outer.popup()
        qtbot.waitUntil(lambda: outer._motion is None)
        parent = outer
    before = parent.grab().toImage()
    panel = FloatingPanel(parent, "Chrome", width=300)
    panel.setFixedSize(300, 180)
    panel.popup()
    qtbot.waitUntil(lambda: panel._motion is None)
    image = panel.grab().toImage()
    scale = image.devicePixelRatio()
    for point in (QPoint(0, 90), QPoint(299, 90), QPoint(150, 0), QPoint(150, 179)):
        actual = image.pixelColor(round(point.x() * scale), round(point.y() * scale))
        surface = _expected(workspace, panel, point, card=True)
        assert max(abs(actual.getRgb()[i] - surface.getRgb()[i]) for i in range(3)) > 15
    # The material's inside pixels remain unchanged; only the outside is shadowed.
    inside = QPoint(18, 162)
    _assert_pixel(
        image.pixelColor(round(inside.x() * scale), round(inside.y() * scale)),
        _expected(workspace, panel, inside, card=True),
    )
    point = panel.pos() + QPoint(150, 182)
    after = parent.grab().toImage()
    x, y = round(point.x() * scale), round(point.y() * scale)
    assert after.pixelColor(x, y).lightness() < before.pixelColor(x, y).lightness()
    panel.close_panel()


def test_popup_global_mapping_and_outside_workspace_fallback(
    workspace: MainWindow,
) -> None:
    popup = QWidget(workspace, Qt.WindowType.Popup)
    popup.resize(80, 80)
    popup.move(workspace._workspace.mapToGlobal(QPoint(-30, -30)))
    frame = QPixmap(popup.size())
    frame.fill(Qt.GlobalColor.magenta)
    painter = QPainter(frame)
    workspace._backdrop_engine.paint(
        popup, painter, tint="#00000000", radius=12, fallback="#246824"
    )
    painter.end()
    pixels = frame.toImage()
    assert pixels.pixelColor(10, 10).name() == "#246824"
    source = workspace._backdrop_engine._frames[12].toImage()
    _assert_pixel(pixels.pixelColor(50, 50), source.pixelColor(20, 20))


def test_open_popup_updates_when_material_disabled_and_restored(
    workspace: MainWindow,
    qtbot: QtBot,
) -> None:
    combo = _combo(workspace)
    combo.showPopup()
    qtbot.wait(20)
    viewport = combo.view().viewport()
    definition = workspace._appearance_theme
    disabled = replace(
        definition, materials=replace(definition.materials, controls_master_enabled=False)
    )
    workspace.set_appearance_theme(disabled, workspace._background_image)
    qtbot.wait(20)
    assert not viewport.property("limbowavePopupMaterial")
    row = combo.view().visualRect(combo.model().index(2, 0))
    point = QPoint(viewport.width() - 8, row.center().y())
    assert viewport.grab().toImage().pixelColor(point).name().upper() == theme.BG_ELEVATED.upper()
    workspace.set_appearance_theme(definition, workspace._background_image)
    qtbot.wait(20)
    assert viewport.property("limbowavePopupMaterial")
    _assert_pixel(
        viewport.grab().toImage().pixelColor(point), _expected(workspace, viewport, point)
    )
    workspace.set_backdrop("", 0)
    assert not viewport.property("limbowavePopupMaterial")
    combo.hidePopup()


def test_repeated_popup_interaction_never_requests_blur(
    workspace: MainWindow,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = workspace._backdrop_engine
    count = engine.render_count
    cache_bytes = engine._cache_bytes

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("popup interaction must not request background processing")

    monkeypatch.setattr(engine, "request", unexpected)
    monkeypatch.setattr(engine, "prepare", unexpected)
    combo = _combo(workspace)
    # Offscreen platforms report native effects disabled even after setEffectEnabled;
    # exercise our reveal/ghost animations independently of that platform policy.
    monkeypatch.setattr(
        QApplication,
        "isEffectEnabled",
        staticmethod(lambda effect: effect == Qt.UIEffect.UI_AnimateCombo),
    )
    combo.addItems([f"row {index}" for index in range(100)])
    helpers: tuple[object, ...] | None = None
    for _ in range(3):
        combo.showPopup()
        qtbot.wait(20)
        surfaces = (combo.view().window(), combo.view(), combo.view().viewport())
        current = tuple(surface._popup_material for surface in surfaces)
        if helpers is not None:
            assert current == helpers
        helpers = current
        combo.view().verticalScrollBar().setValue(20)
        qtbot.mouseMove(combo.view().viewport(), QPoint(12, 12))
        combo.view().viewport().grab()
        combo.hidePopup()
        assert combo._motion._ghost is not None
        qtbot.waitUntil(lambda: combo._motion._ghost is None, timeout=1000)
    panel = FloatingPanel(workspace, "Cached")
    panel.popup()
    qtbot.waitUntil(lambda: panel._motion is None, timeout=1000)
    panel.grab()
    panel.close_panel()
    assert engine.render_count == count
    assert engine._cache_bytes == cache_bytes


@pytest.mark.parametrize(
    ("switch", "card_enabled", "selection_enabled"),
    [("content", False, False), ("cards", False, True), ("selections", True, False)],
)
def test_material_categories_are_respected(
    workspace: MainWindow,
    switch: str,
    card_enabled: bool,
    selection_enabled: bool,
) -> None:
    panel = FloatingPanel(workspace, "Categories")
    menu = AttachmentMenu(workspace.chat)
    definition = workspace._appearance_theme
    materials = definition.materials
    if switch == "selections":
        materials = replace(materials, controls=replace(materials.controls, selections=False))
    else:
        materials = replace(materials, **{f"{switch}_enabled": False})
    workspace.set_appearance_theme(
        replace(definition, materials=materials), workspace._background_image
    )
    assert bool(panel.property("limbowavePopupMaterial")) == card_enabled
    assert bool(menu.property("limbowavePopupMaterial")) == selection_enabled


def test_panel_refreshes_theme_and_releases_subscription(
    workspace: MainWindow,
    qtbot: QtBot,
) -> None:
    from shiboken6 import isValid

    context = workspace._popup_materials
    subscriptions = context.receivers("2changed()")
    panel = FloatingPanel(workspace, "Theme switch")
    panel.setFixedSize(420, 200)
    panel.popup()
    qtbot.waitUntil(lambda: panel._motion is None, timeout=1000)
    assert context.receivers("2changed()") == subscriptions + 1
    definition = workspace._appearance_theme
    updated = replace(definition, colors=replace(definition.colors, card="#228822"))
    workspace.set_appearance_theme(updated, workspace._background_image)
    qtbot.wait(20)
    point = QPoint(18, panel.height() - 18)
    _assert_pixel(
        panel.grab().toImage().pixelColor(point), _expected(workspace, panel, point, card=True)
    )
    # Switching away from the workspace must not put its wallpaper on other pages.
    workspace.show_full_page(QWidget())
    assert not panel.property("limbowavePopupMaterial")
    workspace.show_workspace()
    assert panel.property("limbowavePopupMaterial")
    panel.close_panel()
    qtbot.waitUntil(lambda: not isValid(panel), timeout=1000)
    assert context.receivers("2changed()") == subscriptions
    context.set_theme(definition)  # no stale receiver may touch the deleted panel


def test_plain_host_has_no_material_or_extra_context(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(600, 500)
    host.show()
    panel = FloatingPanel(host, "Plain")
    panel.popup()
    assert not panel.property("limbowavePopupMaterial")
    assert panel._popup_material._context is None
    panel.close_panel()
