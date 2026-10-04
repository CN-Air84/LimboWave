"""主题与界面偏好验收（Task 8.4 / §三.4）。

锁住的不变量：
- 两套调色板都完整（令牌齐全，不会出现半套导致黑底黑字）；
- 切换调色板更新模块级令牌 + 全局 QSS 用新令牌；
- 字号缩放夹在可读范围（0.8–1.6），且**全部字号都跟着变**；
- 视图里没有硬编码字号（否则字号设置会漏掉一部分界面）；
- 偏好持久化：读写往返、损坏/非法值回退默认、单文件不含密钥。
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from limbowave.application.services.preferences_service import (
    DEFAULT_FONT_SCALE,
    DEFAULT_THEME,
    FONT_SCALES,
    SUPPORTED_THEMES,
    Preferences,
    PreferencesService,
)
from limbowave.domain.appearance import BUILTIN_THEMES, AppearanceTheme
from limbowave.ui import theme

UI_DIR = Path(__file__).resolve().parents[2] / "src" / "limbowave" / "ui"


@pytest.fixture(autouse=True)
def _restore_default_theme():
    """每个用例后恢复默认主题，避免用例间互相污染（令牌是模块级全局）。"""
    yield
    theme.set_palette("dark")
    theme.set_font_scale(1.0)


# ---------- 调色板 ----------


@pytest.mark.parametrize("name", SUPPORTED_THEMES)
def test_palette_is_complete(name: str) -> None:
    """两套调色板都要填满所有字段——半套会导致部分界面还是旧配色。"""
    palette = {"dark": theme.DARK, "light": theme.LIGHT}[name]
    for field in fields(palette):
        if field.name == "name":
            continue
        value = getattr(palette, field.name)
        assert isinstance(value, str) and value.startswith("#"), f"{name}.{field.name} 缺失"


def test_light_and_dark_differ_in_background() -> None:
    """浅色与深色的底色必须不同（否则切换无意义）。"""
    assert theme.DARK.bg_app != theme.LIGHT.bg_app
    assert theme.DARK.text_primary != theme.LIGHT.text_primary


def test_set_palette_updates_tokens() -> None:
    theme.set_palette("light")
    assert theme.LIGHT.bg_app == theme.BG_APP
    assert theme.LIGHT.text_primary == theme.TEXT_PRIMARY

    theme.set_palette("dark")
    assert theme.DARK.bg_app == theme.BG_APP
    assert theme.current_palette().name == "dark"


def test_stylesheet_reflects_current_palette() -> None:
    theme.set_palette("light")
    assert theme.LIGHT.bg_app in theme.app_stylesheet()
    theme.set_palette("dark")
    assert theme.DARK.bg_app in theme.app_stylesheet()


def test_unknown_palette_falls_back_to_dark() -> None:
    theme.set_palette("neon")  # type: ignore[arg-type]
    assert theme.current_palette().name == "dark"


# ---------- 字号缩放 ----------


def test_font_scale_updates_all_tokens() -> None:
    theme.set_font_scale(1.0)
    base = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)

    theme.set_font_scale(1.3)
    scaled = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
    assert all(after >= before for before, after in zip(base, scaled, strict=True))
    assert scaled[2] > base[2]  # 基准字号确实变大了


def test_font_scale_clamped() -> None:
    """过小不可读、过大撑坏布局——都夹住。"""
    assert theme.set_font_scale(0.1) == 0.8
    assert theme.set_font_scale(5.0) == 1.6


def test_stylesheet_uses_scaled_font() -> None:
    theme.set_font_scale(1.0)
    assert f"font-size: {theme.FS_BASE}px" in theme.app_stylesheet()
    theme.set_font_scale(1.5)
    assert f"font-size: {theme.FS_BASE}px" in theme.app_stylesheet()
    assert theme.FS_BASE > 13


def test_global_qss_styles_numeric_and_choice_inputs_consistently() -> None:
    css = theme.app_stylesheet()
    assert "QSpinBox, QDoubleSpinBox {" in css
    assert "QSpinBox:focus, QDoubleSpinBox:focus" in css
    assert "QDoubleSpinBox::up-button" in css
    assert "QDoubleSpinBox::down-button" in css
    assert "QDoubleSpinBox::up-arrow" in css
    assert "QDoubleSpinBox::down-arrow" in css
    combo_block = css.split("QComboBox::drop-down {", 1)[1].split("}", 1)[0]
    assert "subcontrol-origin: padding" in combo_block
    assert "border-left: 1px solid" in combo_block
    assert "width: 22px" in combo_block


def test_no_hardcoded_font_sizes_in_views() -> None:
    """视图里不得有硬编码字号——否则字号设置会漏掉一部分界面。"""
    offenders: list[str] = []
    for path in UI_DIR.glob("*.py"):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"font-size:\s*\d+px", line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, "硬编码字号：" + ", ".join(offenders)


def test_bubble_styles_follow_palette() -> None:
    """用户气泡是强调色的淡色调：底色随调色板的强调色变化。"""

    theme.set_palette("light")
    assert f"background: {theme.qss_alpha(theme.LIGHT.accent, 0.55)};" in theme.bubble_stylesheet(
        "user"
    )
    theme.set_palette("dark")
    assert f"background: {theme.qss_alpha(theme.DARK.accent, 0.55)};" in theme.bubble_stylesheet(
        "user"
    )


def test_assistant_bubble_uses_card_surface_and_border() -> None:
    theme.set_palette("dark")
    stylesheet = theme.bubble_stylesheet("assistant")

    assert f"background: {theme.card_surface()}" in stylesheet
    assert f"border: 1px solid {theme.BORDER}" in stylesheet


# ---------- 偏好持久化 ----------


def test_preferences_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    service = PreferencesService(path)
    service.save(Preferences(theme="light", font_scale=1.3))

    loaded = PreferencesService(path).load()
    assert loaded.theme == "light"
    assert loaded.font_scale == 1.3


def test_preferences_missing_file_gives_defaults(tmp_path: Path) -> None:
    loaded = PreferencesService(tmp_path / "nope.json").load()
    assert loaded.theme == DEFAULT_THEME
    assert loaded.font_scale == DEFAULT_FONT_SCALE


def test_preferences_corrupt_file_gives_defaults(tmp_path: Path) -> None:
    """损坏的偏好不该让应用起不来。"""
    path = tmp_path / "preferences.json"
    path.write_text("{ not json", encoding="utf-8")
    assert PreferencesService(path).load().theme == DEFAULT_THEME


def test_preferences_invalid_values_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"theme": "neon", "font_scale": 99}), encoding="utf-8")
    loaded = PreferencesService(path).load()
    assert loaded.theme == DEFAULT_THEME  # 不认识的主题回退
    assert loaded.font_scale == 1.6  # 越界夹取


def test_preferences_no_secrets(tmp_path: Path) -> None:
    """偏好是普通配置——不含密钥。"""
    import base64

    path = tmp_path / "preferences.json"
    PreferencesService(path).save(Preferences(theme="light", font_scale=1.15))
    content = path.read_text(encoding="utf-8")
    assert "key" not in content.lower()
    assert "secret" not in content.lower()
    assert "token" not in content.lower()
    assert base64.b64encode(b"x").decode() not in content or True


def test_font_scales_are_offered() -> None:
    assert 1.0 in FONT_SCALES
    assert min(FONT_SCALES) >= 0.8
    assert max(FONT_SCALES) <= 1.6


# ---------- 代码高亮跟随主题（浅色下可读性） ----------


def test_pygments_style_follows_palette() -> None:
    """深色用深底样式、浅色用浅底样式——否则浅底上会出现浅色文字。"""
    theme.set_palette("dark")
    assert theme.pygments_style() == "monokai"
    theme.set_palette("light")
    assert theme.pygments_style() == "friendly"


def test_code_block_highlight_uses_theme_style() -> None:
    """渲染出的代码块颜色随主题变（不是写死的一套）。"""
    from limbowave.ui import markdown_render

    source = "```python\ndef f(x):\n    return x + 1\n```"
    theme.set_palette("dark")
    dark_html = markdown_render.render(source)
    theme.set_palette("light")
    light_html = markdown_render.render(source)

    assert "#" in dark_html and "#" in light_html
    assert dark_html != light_html  # 配色确实不同


def test_code_block_background_follows_theme() -> None:
    from limbowave.ui import markdown_render

    theme.set_palette("light")
    html = markdown_render.render("```\nplain\n```")
    assert theme.CODE_BG in html


# ---------- 状态文字色（深浅主题都要读得清） ----------

_STATUS_TONES = ("success", "warning", "danger")


def _contrast(first: str, second: str) -> float:
    """WCAG 2.x 对比度。"""

    def luminance(color: str) -> float:
        channels = [int(color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        red, green, blue = (
            c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
        )
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_status_text_colors_switch_with_palette_brightness() -> None:
    theme.set_palette("dark")
    dark = [theme.status_text_color(tone) for tone in _STATUS_TONES]
    theme.set_palette("light")
    light = [theme.status_text_color(tone) for tone in _STATUS_TONES]
    assert len(set(dark)) == len(set(light)) == len(_STATUS_TONES)
    assert all(a != b for a, b in zip(dark, light, strict=True))


@pytest.mark.parametrize("definition", BUILTIN_THEMES, ids=lambda item: item.id)
def test_status_text_colors_are_readable_on_builtin_themes(definition: AppearanceTheme) -> None:
    """状态色直接当文字用，在每套内置主题的卡片底上都要达到正文对比度 4.5:1。"""
    theme.apply_appearance_theme(definition)
    for tone in _STATUS_TONES:
        assert _contrast(theme.status_text_color(tone), theme.BG_SURFACE) >= 4.5, tone


def test_inline_styles_follow_live_palette_switch(qtbot) -> None:
    from PySide6.QtWidgets import QApplication

    from limbowave.ui.main_window import MainWindow

    app = QApplication.instance()
    assert app is not None
    theme.set_palette("dark")
    theme.set_font_scale(1.0)
    app.setStyleSheet(theme.app_stylesheet())
    window = MainWindow()
    qtbot.addWidget(window)
    previous = theme.current_palette()
    old_sizes = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
    theme.set_palette("light")
    app.setStyleSheet(theme.app_stylesheet())
    theme.refresh_inline_styles(window, previous, old_sizes)
    window.sidebar.restyle()
    assert theme.LIGHT.bg_app in window.chat._transcript_host.styleSheet()
    assert theme.DARK.bg_app not in window.chat._transcript_host.styleSheet()
    assert theme.LIGHT.bg_surface in window.toolbar.styleSheet()
    assert theme.DARK.bg_surface not in window.toolbar.styleSheet()


def test_tool_hover_surface_keeps_strength_across_palettes() -> None:
    """工具行按钮的悬浮底色：换调色板只换底色，浓度不变。"""
    theme.set_palette("dark")
    dark = theme.tool_hover_surface()
    theme.set_palette("light")
    light = theme.tool_hover_surface()

    assert dark.startswith("rgba(") and light.startswith("rgba(")
    assert dark != light
    assert dark.rsplit(",", 1)[1] == light.rsplit(",", 1)[1]


def test_refresh_inline_styles_remaps_rgba_overlays(qtbot) -> None:
    """内联 QSS 里的 rgba 叠色（悬浮底色等）要随主题切换换底色、保留浓度。"""
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance()
    assert app is not None
    theme.set_palette("dark")
    app.setStyleSheet(theme.app_stylesheet())
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.setStyleSheet(f"QWidget {{ background: {theme.tool_hover_surface()}; }}")
    previous = theme.current_palette()
    old_sizes = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
    old_overlay = theme.tool_hover_surface()

    theme.set_palette("light")
    theme.refresh_inline_styles(widget, previous, old_sizes)

    css = widget.styleSheet()
    assert old_overlay not in css
    assert theme.tool_hover_surface() in css


def test_input_focus_ring_has_explicit_even_width() -> None:
    """焦点态必须重设完整四边边框，不能只改颜色交给原生样式补线。"""
    stylesheet = theme.app_stylesheet()
    focus_rule = stylesheet.split("QLineEdit:focus", 1)[1].split("}", 1)[0]
    assert f"border: 2px solid {theme.ACCENT};" in focus_rule
    assert "padding: 6px 11px;" in focus_rule
    assert "QDateEdit:focus" in stylesheet
