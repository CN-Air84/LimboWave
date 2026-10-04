from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from limbowave.application.services.appearance_theme_service import AppearanceThemeService
from limbowave.domain.appearance import (
    BACKGROUND_FIT_MODES,
    BACKGROUND_POSITIONS,
    BUILTIN_THEMES,
    DEFAULT_THEME_ID,
    MaterialSettings,
    TextGlowSettings,
    replace_controls,
)


def _png(path: Path, color: str = "red", size: tuple[int, int] = (80, 40)) -> None:
    Image.new("RGB", size, color).save(path)


def test_builtins_and_all_adjustable_fields_are_valid() -> None:
    assert len(BUILTIN_THEMES) == 8
    assert BUILTIN_THEMES[0].id == DEFAULT_THEME_ID
    assert all(theme.readonly for theme in BUILTIN_THEMES)
    theme = BUILTIN_THEMES[0]
    assert set(theme.colors.__dataclass_fields__) == {
        "background",
        "card",
        "component",
        "accent",
        "text",
        "info",
        "warning",
        "error",
        "tab_indicator",
    }
    assert theme.background.fit_mode in BACKGROUND_FIT_MODES
    assert theme.background.position in BACKGROUND_POSITIONS
    assert set(theme.materials.controls.__dataclass_fields__) == {
        "buttons",
        "text_inputs",
        "selections",
        "item_views",
        "scrollbars",
    }


def test_material_control_master_preserves_category_choices() -> None:
    settings = replace_controls(MaterialSettings(), scrollbars=False, text_inputs=False)
    assert not settings.control_enabled("scrollbars")
    disabled = replace(settings, controls_master_enabled=False)
    assert not disabled.control_enabled("buttons")
    restored = replace(disabled, controls_master_enabled=True)
    assert restored.control_enabled("buttons")
    assert not restored.control_enabled("scrollbars")
    assert not restored.control_enabled("text_inputs")


def test_text_glow_interpolates_and_validates_ranges() -> None:
    glow = TextGlowSettings(True, 0.1, 0.7, 2.0, 8.0)
    assert glow.values_for_size(10, 10, 30) == pytest.approx((0.1, 2.0))
    assert glow.values_for_size(20, 10, 30) == pytest.approx((0.4, 5.0))
    assert glow.values_for_size(40, 10, 30) == pytest.approx((0.7, 8.0))
    with pytest.raises(ValueError):
        TextGlowSettings(True, 0.8, 0.2, 1.0, 2.0)


def test_theme_service_custom_lifecycle_and_restart(tmp_path: Path) -> None:
    service = AppearanceThemeService(tmp_path / "themes")
    custom = service.create("我的玻璃", BUILTIN_THEMES[1])
    assert not custom.readonly
    assert service.active_theme.id == custom.id
    changed = replace(custom, materials=replace(custom.materials, content_opacity=0.41))
    service.save(changed)
    service.rename(changed.id, "我的玻璃 2")

    restarted = AppearanceThemeService(tmp_path / "themes")
    assert restarted.active_theme.name == "我的玻璃 2"
    assert restarted.active_theme.materials.content_opacity == pytest.approx(0.41)
    fallback = restarted.delete(changed.id)
    assert fallback.id == DEFAULT_THEME_ID
    assert restarted.active_theme.id == DEFAULT_THEME_ID
    with pytest.raises(PermissionError):
        restarted.delete(DEFAULT_THEME_ID)


def test_theme_state_corruption_is_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "themes"
    root.mkdir()
    (root / "theme-state.json").write_text("{bad", encoding="utf-8")
    service = AppearanceThemeService(root)
    assert service.active_theme.id == DEFAULT_THEME_ID
    assert list(root.glob("theme-state.corrupt-*.json"))
    assert not (root / "theme-state.json").exists()


def test_asset_import_normalizes_and_resolves_inside_root(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    Image.new("RGB", (32, 24), "#336699").save(source)
    service = AppearanceThemeService(tmp_path / "themes")
    asset = service.import_background(source)
    assert asset.startswith("assets/") and asset.endswith(".png")
    resolved = service.resolve_asset(asset)
    assert resolved is not None and resolved.is_file()
    with Image.open(resolved) as image:
        assert image.mode == "RGBA"
        assert image.size == (32, 24)
    assert service.resolve_asset("../source.jpg") is None


def test_webp_is_rejected_even_if_pillow_can_decode_it(tmp_path: Path) -> None:
    source = tmp_path / "source.webp"
    Image.new("RGB", (8, 8), "red").save(source)
    service = AppearanceThemeService(tmp_path / "themes")
    with pytest.raises(ValueError, match="PNG, JPEG and BMP"):
        service.import_background(source)


def test_state_json_is_single_authoritative_file(tmp_path: Path) -> None:
    service = AppearanceThemeService(tmp_path / "themes")
    service.create("自定义", BUILTIN_THEMES[2])
    payload = json.loads(service.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["active_theme_id"] == service.active_theme_id
    assert len(payload["themes"]) == 1
    assert not service.path.with_suffix(".tmp").exists()


def test_legacy_background_is_migrated_to_managed_custom_theme(tmp_path: Path) -> None:
    source = tmp_path / "legacy.png"
    _png(source)
    service = AppearanceThemeService(
        tmp_path / "themes",
        legacy_theme="light",
        legacy_background_image=str(source),
        legacy_blur_radius=23,
    )
    assert not service.active_theme.readonly
    assert service.active_theme.background.asset.startswith("assets/")
    assert service.active_theme.materials.content_blur_radius == 23
    assert service.resolve_asset(service.active_theme.background.asset) is not None


def test_orphan_assets_are_only_deleted_on_explicit_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    _png(source)
    service = AppearanceThemeService(tmp_path / "themes")
    referenced = service.import_background(source)
    custom = service.create(
        "有背景",
        replace(
            BUILTIN_THEMES[0],
            background=replace(BUILTIN_THEMES[0].background, asset=referenced),
        ),
    )
    orphan = service.assets / ("0" * 64 + ".png")
    _png(orphan)
    assert service.orphan_assets() == (orphan,)
    assert orphan.exists()
    assert service.cleanup_orphan_assets() == (orphan,)
    assert not orphan.exists()
    assert service.resolve_asset(custom.background.asset) is not None
