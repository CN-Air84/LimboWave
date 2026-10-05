"""Rendered outline/shadow regressions, including nested panels and fade lifetime."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from limbowave.ui import soft_shadow, theme
from limbowave.ui.floating import FloatingPanel


def _pixel(image: QImage, point: QPoint) -> QColor:
    scale = image.devicePixelRatio()
    return image.pixelColor(round(point.x() * scale), round(point.y() * scale))


def _settle(qtbot: QtBot, panel: FloatingPanel) -> None:
    panel.popup()
    qtbot.waitUntil(lambda: panel._motion is None)


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT], ids=["dark", "light"])
def test_panel_outline_and_shadow_pixels(qtbot: QtBot, monkeypatch, palette) -> None:
    monkeypatch.setattr(theme, "BG_SURFACE", palette.bg_surface)
    monkeypatch.setattr(theme, "TEXT_SECONDARY", palette.text_secondary)
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(800, 600)
    # Identical host/card colors ensure the separation really comes from chrome.
    host.setStyleSheet(f"background: {palette.bg_surface};")
    host.show()
    panel = FloatingPanel(host, "Outline")
    panel.setFixedSize(420, 200)
    _settle(qtbot, panel)
    image = host.grab().toImage()
    surface = QColor(palette.bg_surface)
    for point in (QPoint(0, 100), QPoint(419, 100), QPoint(210, 0), QPoint(210, 199)):
        edge = _pixel(image, panel.pos() + point)
        assert max(abs(edge.getRgb()[i] - surface.getRgb()[i]) for i in range(3)) > 15
    assert _pixel(image, panel.pos() + QPoint(18, 182)) == surface
    below = panel.pos() + QPoint(210, 202)
    assert _pixel(image, below).lightness() < surface.lightness()
    assert _pixel(image, panel.pos() + QPoint(210, 218)) == surface
    assert panel._shadow.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert host.childAt(below) is host or host.childAt(below) is None


def test_shadow_tracks_geometry_stacking_and_panel_lifetime(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(900, 700)
    host.show()
    panel = FloatingPanel(host, "Outer")
    panel.setFixedSize(600, 400)
    _settle(qtbot, panel)
    shadow = panel._shadow
    assert shadow.isVisible()
    assert shadow.geometry() == soft_shadow.shadow_bounds(panel.geometry())
    panel.move(panel.pos() + QPoint(24, 16))
    panel.setFixedSize(620, 420)
    assert shadow.geometry() == soft_shadow.shadow_bounds(panel.geometry())
    nested = FloatingPanel(panel, "Inner", width=300)
    nested.setFixedSize(300, 160)
    _settle(qtbot, nested)
    assert nested._shadow.parentWidget() is panel
    assert nested._shadow.isVisible()
    nested.raise_()
    assert panel.childAt(nested.pos() + QPoint(150, 100)) is nested
    panel.hide()
    assert not shadow.isVisible()
    panel.show()
    assert shadow.isVisible()
    panel.close_panel()
    assert shadow.isVisible()  # Keep the shadow throughout the fade, not a hard cut.
    qtbot.waitUntil(lambda: not isValid(panel))
    qtbot.waitUntil(lambda: not isValid(shadow))
    qtbot.waitUntil(lambda: not isValid(nested))


def test_shadow_fades_with_panel_without_replacing_opacity_effect(qtbot: QtBot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(800, 600)
    host.show()
    panel = FloatingPanel(host, "Fade")
    panel.setFixedSize(420, 200)
    _settle(qtbot, panel)
    assert panel.graphicsEffect() is panel._opacity
    assert not panel._opacity.isEnabled()
    shadow = panel._shadow
    point = QPoint(shadow.width() // 2, soft_shadow.SHADOW_SPREAD - soft_shadow.SHADOW_OFFSET + 202)
    full = _pixel(shadow.grab().toImage(), point).alpha()
    assert full > 0
    panel._opacity.setOpacity(0.5)
    half = _pixel(shadow.grab().toImage(), point).alpha()
    assert 0 < half < full
    panel._opacity.setOpacity(0.0)
    assert _pixel(shadow.grab().toImage(), point).alpha() == 0
