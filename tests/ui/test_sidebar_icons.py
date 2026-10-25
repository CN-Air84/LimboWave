"""Pixel-level regressions for sidebar icon geometry."""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QSize

from limbowave.ui import theme
from limbowave.ui.sidebar import _settings_icon


@pytest.mark.parametrize("palette", [theme.DARK, theme.LIGHT], ids=["dark", "light"])
@pytest.mark.parametrize("device_pixel_ratio", [1.0, 1.25, 1.5, 2.0])
def test_settings_gear_and_hub_share_center(qapp, monkeypatch, palette, device_pixel_ratio):
    monkeypatch.setattr(theme, "TEXT_SECONDARY", palette.text_secondary)
    image = _settings_icon().pixmap(QSize(18, 18), device_pixel_ratio).toImage()

    # Measure alpha-weighted centers independently, in logical coordinates.
    # The empty gap at radius 4 separates the hub stroke from the gear outline.
    scale = image.width() / 18
    components = {"gear": [], "hub": []}
    for y in range(image.height()):
        for x in range(image.width()):
            alpha = image.pixelColor(x, y).alpha()
            if not alpha:
                continue
            px, py = (x + 0.5) / scale, (y + 0.5) / scale
            component = "hub" if math.hypot(px - 9, py - 9) < 4 else "gear"
            components[component].append((px, py, alpha))

    for name, pixels in components.items():
        assert pixels, f"Missing settings icon {name}"
        total_alpha = sum(pixel[2] for pixel in pixels)
        center = tuple(
            sum(pixel[axis] * pixel[2] for pixel in pixels) / total_alpha
            for axis in (0, 1)
        )
        assert center == pytest.approx((9.0, 9.0), abs=0.1), name
