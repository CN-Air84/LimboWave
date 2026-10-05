"""Appearance editor and background-renderer integration tests."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QAbstractAnimation, QPoint
from PySide6.QtGui import QColor, QFontDatabase, QImage
from PySide6.QtWidgets import QApplication, QGroupBox, QWidget
from pytestqt.qtbot import QtBot

from limbowave.application.services.appearance_theme_service import AppearanceThemeService
from limbowave.application.services.preferences_service import PreferencesService
from limbowave.domain.appearance import BUILTIN_THEMES, CONTROL_CATEGORIES, BackgroundSettings
from limbowave.ui import theme
from limbowave.ui.backdrop import BackdropEngine, _compose
from limbowave.ui.main_window import MainWindow
from limbowave.ui.settings_dialog import _AppearanceTab


def _image(path: Path, *, size: tuple[int, int] = (500, 300)) -> None:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff4422"))
    assert image.save(str(path))


def test_backdrop_reuses_frames_for_multiple_radii(qtbot: QtBot, tmp_path: Path) -> None:
    image = tmp_path / "wall.png"
    _image(image)
    workspace = QWidget()
    qtbot.addWidget(workspace)
    workspace.resize(500, 300)
    engine = BackdropEngine(workspace)
    assert engine.prepare(str(image), 14)
    assert engine.render_count == 1
    assert engine.prepare(str(image), 14)
    assert engine.render_count == 1
    assert engine.prepare(str(image), 20)
    assert engine.render_count == 2
    assert engine.prepare(str(image), 14)
    assert engine.render_count == 2
    assert not engine.prepare(str(tmp_path / "missing.png"), 14)
    assert not engine.active


def test_cover_contain_stretch_and_tile_are_distinct(tmp_path: Path) -> None:
    source = tmp_path / "split.png"
    image = Image.new("RGB", (40, 20), "red")
    for x in range(20, 40):
        for y in range(20):
            image.putpixel((x, y), (0, 0, 255))
    image.save(source)
    frames = {
        mode: _compose(
            str(source),
            (60, 60),
            BackgroundSettings(fit_mode=mode, position="bottom-right"),
            "#00FF00",
        )
        for mode in ("cover", "contain", "stretch", "tile")
    }
    assert len({frame.tobytes() for frame in frames.values()}) == 4
    assert frames["contain"].getpixel((0, 0))[:3] == (0, 255, 0)
    assert frames["stretch"].getpixel((0, 0))[:3] == (255, 0, 0)


def test_image_and_mask_opacity_are_composed(tmp_path: Path) -> None:
    source = tmp_path / "red.png"
    Image.new("RGB", (10, 10), "red").save(source)
    frame = _compose(
        str(source),
        (10, 10),
        BackgroundSettings(image_opacity=0.5, mask_color="#0000FF", mask_opacity=0.25),
        "#000000",
    )
    red, green, blue, alpha = frame.getpixel((5, 5))
    assert 80 < red < 120
    assert green == 0
    assert 55 < blue < 75
    assert alpha == 255


def test_async_generation_never_installs_stale_background(qtbot: QtBot, tmp_path: Path) -> None:
    red = tmp_path / "red.png"
    blue = tmp_path / "blue.png"
    Image.new("RGB", (600, 400), "red").save(red)
    Image.new("RGB", (600, 400), "blue").save(blue)
    workspace = QWidget()
    qtbot.addWidget(workspace)
    workspace.resize(600, 400)
    engine = BackdropEngine(workspace)
    settings = BackgroundSettings(asset="managed.png")
    started = time.perf_counter()
    assert engine.request(str(red), settings, "#000000", (32, 18))
    assert engine.request(str(blue), settings, "#000000", (0,))
    assert (time.perf_counter() - started) < 0.1
    deadline = time.monotonic() + 3
    while not engine.active and time.monotonic() < deadline:
        time.sleep(0.02)
        QApplication.processEvents()
    assert engine.active
    center = engine._frame.toImage().pixelColor(300, 200)
    assert center.blue() > 240 and center.red() < 10
    assert engine.render_count == 1
    engine.close()


def test_window_backdrop_does_not_recompose_on_message_scroll(qtbot: QtBot, tmp_path: Path) -> None:
    image = tmp_path / "wall.png"
    _image(image)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.set_backdrop(str(image), 16)
    assert window._backdrop_engine.active
    count = window._backdrop_engine.render_count
    window.chat.add_user_message("hello")
    qtbot.wait(30)
    assert window._backdrop_engine.render_count == count
    window.set_backdrop("", 0)
    assert not window._backdrop_engine.active


def test_workspace_controls_show_backdrop_through_clear_surfaces(
    qtbot: QtBot, tmp_path: Path
) -> None:
    image = tmp_path / "wall.png"
    _image(image)
    source = BUILTIN_THEMES[0]
    definition = replace(source, background=replace(source.background, asset=str(image)))
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    old_css = app.styleSheet()
    theme.apply_appearance_theme(definition)
    app.setStyleSheet(theme.app_stylesheet())
    try:
        window = MainWindow()
        qtbot.addWidget(window)
        window.show()
        window.set_appearance_theme(definition, str(image))
        qtbot.waitUntil(lambda: window._backdrop_engine.active, timeout=3000)
        qtbot.wait(20)
        screenshot = window.grab().toImage()
        for widget in (window._sidebar._list.viewport(), window.chat._input):
            point = widget.mapTo(window, QPoint(widget.width() // 2, widget.height() // 2))
            pixel = screenshot.pixelColor(point)
            # An opaque legacy surface is charcoal; the wallpaper tint retains red.
            assert pixel.red() > pixel.blue() + 10, (widget, pixel.name())
    finally:
        theme.apply_appearance_theme(source)
        app.setStyleSheet(old_css)


def test_light_theme_sidebar_button_keeps_accent_fill_and_visible_glyph(
    qtbot: QtBot,
) -> None:
    """浅色主题下「新建会话」按钮必须仍是强调色底 + 可见文字。

    回归形态：侧栏拿到无选择器的 ``background: <card>`` 级联样式，把应用级
    QSS 里 ``[accent="true"]`` 的强调色底盖成白卡片——白字（text_on_accent）
    落在白底上，整颗按钮渲染成单色一片。
    """
    source = BUILTIN_THEMES[0]
    light = next(theme_def for theme_def in BUILTIN_THEMES if theme_def.id == "builtin:clear-day")
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    old_css = app.styleSheet()
    theme.apply_appearance_theme(light)
    app.setStyleSheet(theme.app_stylesheet())
    try:
        window = MainWindow()
        qtbot.addWidget(window)
        window.show()
        window.set_appearance_theme(light, "")
        qtbot.wait(20)
        assert window._sidebar.styleSheet().startswith("Sidebar {")
        image = window._sidebar._new_btn.grab().toImage()
        colors = {
            image.pixelColor(x, y).name().upper()
            for y in range(image.height())
            for x in range(image.width())
        }
        assert theme.ACCENT.upper() in colors
        # 文字可见：除了底色与抗锯齿边缘，画面里必须有第二种实色。
        assert len(colors - {theme.ACCENT.upper()}) >= 1
    finally:
        theme.apply_appearance_theme(source)
        app.setStyleSheet(old_css)


def test_empty_session_shows_clear_backdrop_except_composer_plate(
    qtbot: QtBot, tmp_path: Path
) -> None:
    image = tmp_path / "wall.png"
    _image(image)
    source = BUILTIN_THEMES[0]
    definition = replace(source, background=replace(source.background, asset=str(image)))
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    old_css = app.styleSheet()
    theme.apply_appearance_theme(definition)
    app.setStyleSheet(theme.app_stylesheet())
    try:
        window = MainWindow()
        qtbot.addWidget(window)
        window.resize(1280, 800)
        window.show()
        window.set_appearance_theme(definition, str(image))
        chat = window.chat
        qtbot.waitUntil(
            lambda: window._backdrop_engine.active and chat.composer_lift > 0, timeout=3000
        )
        qtbot.wait(20)
        screenshot = window.grab().toImage()

        def pixel(widget: QWidget, y: int) -> str:
            return screenshot.pixelColor(widget.mapTo(window, QPoint(8, y))).name()

        transcript = pixel(chat._scroll, chat._scroll.height() // 2)
        # 输入框行里、输入框左边的空白（离输入框和它的阴影都很远）
        beside_composer = pixel(chat, chat._composer.y() + chat._composer.height() // 2)
        lift = pixel(chat._composer_lift, chat._composer_lift.height() // 2)
        # 输入框左侧内边距里，只有背景板
        plate = pixel(chat._composer, chat._composer.height() // 2)
        # Only the composer itself carries the frosted plate; the rest of its row and
        # the blank lift under a centred composer show the same clear wallpaper.
        assert beside_composer == transcript, (beside_composer, transcript)
        assert lift == transcript, (lift, transcript)
        assert plate != transcript
    finally:
        theme.apply_appearance_theme(source)
        app.setStyleSheet(old_css)


def test_appearance_uses_horizontal_secondary_tabs_without_card_groups(
    qtbot: QtBot, tmp_path: Path
) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    assert editor._subtabs.count() == 7
    assert [editor._subtabs.tabText(index) for index in range(editor._subtabs.count())] == [
        "主题",
        "颜色",
        "背景",
        "材质",
        "控件",
        "光晕与交互",
        "字体",
    ]
    assert editor._stack.count() == editor._subtabs.count()
    assert editor.findChildren(QGroupBox) == []
    editor._subtabs.setCurrentIndex(4)
    assert editor._stack.currentIndex() == 4


def test_secondary_tab_bar_is_centered_compact_without_clipping_long_labels(
    qtbot: QtBot, tmp_path: Path
) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    editor.resize(900, 640)
    editor.show()
    qtbot.waitExposed(editor)
    qtbot.wait(20)

    bar = editor._subtabs
    assert bar.width() < editor.width()
    assert bar.width() == bar.preferred_width()
    left_gap = bar.mapTo(editor, QPoint(0, 0)).x()
    right_gap = editor.width() - (left_gap + bar.width())
    assert abs(left_gap - right_gap) <= 2
    assert bar.tabRect(bar.count() - 1).right() < bar.width()
    glow_index = next(index for index in range(bar.count()) if bar.tabText(index) == "光晕与交互")
    required = bar.fontMetrics().horizontalAdvance("光晕与交互") + 24
    assert bar.tabRect(glow_index).width() >= required
    assert bar.tabRect(glow_index).width() > bar.tabRect(0).width()
    assert bar.tabRect(0).width() == bar.tabRect(1).width()
    bar.setCurrentIndex(glow_index)
    qtbot.waitUntil(lambda: editor._stack.current_index == glow_index, timeout=1500)
    assert bar._target_rect(glow_index).right() < bar.width()


def test_secondary_tabs_animate_indicator_slide_and_fade(qtbot: QtBot, tmp_path: Path) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    editor.resize(960, 640)
    editor.show()
    qtbot.waitExposed(editor)
    editor._subtabs.sync_indicator()
    old_indicator = editor._subtabs._indicator.geometry()

    editor._subtabs.setCurrentIndex(3)

    assert editor._subtabs._indicator_animation.state() == QAbstractAnimation.State.Running
    assert editor._stack._animation is not None
    qtbot.waitUntil(
        lambda: (
            editor._stack.current_index == 3
            and editor._stack._effect.opacity() >= 0.999
            and editor._stack._stack.pos().x() == 0
            and editor._subtabs._indicator_animation.state() == QAbstractAnimation.State.Stopped
        ),
        timeout=1500,
    )
    assert editor._subtabs._indicator.x() > old_indicator.x()


def test_secondary_tab_animation_retargets_after_rapid_clicks(qtbot: QtBot, tmp_path: Path) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    editor.resize(960, 640)
    editor.show()
    qtbot.waitExposed(editor)

    editor._subtabs.setCurrentIndex(6)
    qtbot.wait(70)
    editor._subtabs.setCurrentIndex(2)

    assert editor._stack._generation == 2
    assert editor._stack._animation is not None
    qtbot.waitUntil(
        lambda: (
            editor._stack.current_index == 2
            and editor._stack._animation is None
            and editor._subtabs._indicator_animation.state() == QAbstractAnimation.State.Stopped
        ),
        timeout=1800,
    )
    assert editor._subtabs._indicator.geometry() == editor._subtabs._target_rect(2)


def test_decimal_radius_fields_match_integer_and_combo_input_geometry(
    qtbot: QtBot, tmp_path: Path
) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    editor.resize(1000, 680)
    editor.show()
    qtbot.waitExposed(editor)
    qtbot.wait(20)

    editor._subtabs.setCurrentIndex(2)
    editor._stack.set_index(2, animated=False)
    qtbot.wait(1)
    assert editor.background_fit_combo.height() == editor.background_image_opacity.height()
    assert editor.background_position_combo.height() == editor.background_mask_opacity.height()

    editor._subtabs.setCurrentIndex(3)
    editor._stack.set_index(3, animated=False)
    qtbot.wait(1)
    assert editor.content_blur_radius.height() == editor.content_opacity.height()
    assert editor.sidebar_blur_radius.height() == editor.sidebar_opacity.height()

    editor._subtabs.setCurrentIndex(5)
    editor._stack.set_index(5, animated=False)
    qtbot.wait(1)
    assert editor.glow_minimum_radius.height() == editor.glow_minimum_intensity.height()
    assert editor.glow_maximum_radius.height() == editor.glow_maximum_intensity.height()


def test_editor_exposes_every_planned_setting_and_updates_one_draft(
    qtbot: QtBot, tmp_path: Path
) -> None:
    preferences = PreferencesService(tmp_path / "preferences.json")
    service = AppearanceThemeService(tmp_path / "themes")
    editor = _AppearanceTab(preferences, theme_service=service)
    qtbot.addWidget(editor)
    assert editor.theme_combo.count() == 8
    assert not editor.theme_save_button.isEnabled()
    editor.background_color.set_color("#123456")
    editor.background_fit_combo.setCurrentIndex(editor.background_fit_combo.findData("tile"))
    editor.background_position_combo.setCurrentIndex(
        editor.background_position_combo.findData("bottom-right")
    )
    editor.background_image_opacity.setValue(42)
    editor.background_mask_color.set_color("#654321")
    editor.background_mask_opacity.setValue(27)
    editor.content_enabled.setChecked(False)
    editor.content_opacity.setValue(63)
    editor.content_blur_radius.setValue(13.5)
    editor.cards_enabled.setChecked(False)
    editor.controls_master_enabled.setChecked(False)
    for category in CONTROL_CATEGORIES:
        editor.control_checks[category].setChecked(category != "scrollbars")
    editor.sidebar_enabled.setChecked(False)
    editor.sidebar_opacity.setValue(71)
    editor.sidebar_blur_radius.setValue(9.5)
    editor.tab_indicator_opacity.setValue(58)
    editor.text_glow_enabled.setChecked(True)
    editor.glow_minimum_intensity.setValue(11)
    editor.glow_maximum_intensity.setValue(72)
    editor.glow_minimum_radius.setValue(2.5)
    editor.glow_maximum_radius.setValue(8.5)
    editor.hover_suspend_enabled.setChecked(False)
    editor.hover_enter_speed.setValue(125)
    editor.hover_restore_speed.setValue(275)

    draft = editor.draft
    assert draft.colors.background == "#123456"
    assert draft.background.fit_mode == "tile"
    assert draft.background.position == "bottom-right"
    assert draft.background.image_opacity == pytest.approx(0.42)
    assert draft.background.mask_color == "#654321"
    assert draft.background.mask_opacity == pytest.approx(0.27)
    assert not draft.materials.content_enabled
    assert draft.materials.content_opacity == pytest.approx(0.63)
    assert draft.materials.content_blur_radius == 13.5
    assert not draft.materials.cards_enabled
    assert not draft.materials.controls_master_enabled
    assert not draft.materials.controls.scrollbars
    assert draft.materials.controls.buttons
    assert not draft.materials.sidebar_enabled
    assert draft.materials.sidebar_opacity == pytest.approx(0.71)
    assert draft.materials.sidebar_blur_radius == 9.5
    assert draft.materials.tab_indicator_opacity == pytest.approx(0.58)
    assert draft.text_glow.enabled
    assert draft.text_glow.minimum_intensity == pytest.approx(0.11)
    assert draft.text_glow.maximum_intensity == pytest.approx(0.72)
    assert draft.text_glow.minimum_radius == 2.5
    assert draft.text_glow.maximum_radius == 8.5
    assert not draft.materials.hover_suspend_enabled
    assert draft.materials.hover_enter_ms == 125
    assert draft.materials.hover_restore_ms == 275
    assert editor.dirty


def test_checkbox_preview_is_deferred_until_click_feedback_plays(
    qtbot: QtBot, tmp_path: Path
) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    emitted: list[object] = []
    editor.appearance_changed.connect(emitted.append)

    editor.cards_enabled.toggle()
    editor.cards_enabled.toggle()
    # The heavy global restyle must not run inside the click handler.
    assert emitted == []
    qtbot.waitUntil(lambda: len(emitted) == 1, timeout=editor.PREVIEW_DELAY_MS * 5)
    assert emitted == [editor.draft]

    editor.cards_enabled.toggle()
    editor._reset_draft()  # an explicit reset supersedes the pending preview
    assert emitted[-1] == editor.draft
    qtbot.wait(editor.PREVIEW_DELAY_MS * 2)
    assert len(emitted) == 2


def test_color_button_stylesheet_is_valid_qss(qtbot: QtBot, tmp_path: Path) -> None:
    editor = _AppearanceTab(PreferencesService(tmp_path / "preferences.json"))
    qtbot.addWidget(editor)
    css = editor.accent_color.styleSheet()
    assert css.count("{") == css.count("}") == 1


def test_builtin_draft_can_be_saved_as_custom(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from limbowave.ui import appearance_editor

    preferences = PreferencesService(tmp_path / "preferences.json")
    service = AppearanceThemeService(tmp_path / "themes")
    editor = _AppearanceTab(preferences, theme_service=service)
    qtbot.addWidget(editor)
    editor.accent_color.set_color("#AABBCC")
    monkeypatch.setattr(
        appearance_editor.QInputDialog, "getText", lambda *args, **kwargs: ("我的主题", True)
    )
    assert editor._save_as()
    assert service.active_theme.name == "我的主题"
    assert service.active_theme.colors.accent == "#AABBCC"
    assert editor.theme_combo.count() == 9
    assert editor.theme_save_button.isEnabled() is False
    assert editor.theme_rename_button.isEnabled()
    assert editor.theme_delete_button.isEnabled()


def test_background_import_is_managed_and_previewed(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from limbowave.ui import appearance_editor

    source = tmp_path / "wall.jpg"
    Image.new("RGB", (30, 20), "purple").save(source)
    preferences = PreferencesService(tmp_path / "preferences.json")
    service = AppearanceThemeService(tmp_path / "themes")
    editor = _AppearanceTab(preferences, theme_service=service)
    qtbot.addWidget(editor)
    monkeypatch.setattr(
        appearance_editor.QFileDialog, "getOpenFileName", lambda *args: (str(source), "")
    )
    editor._import_background()
    qtbot.waitUntil(editor.isEnabled)
    assert editor.draft.background.asset.startswith("assets/")
    assert service.resolve_asset(editor.draft.background.asset) is not None
    editor._remove_background()
    assert editor.draft.background.asset == ""


def test_system_font_preference_remains_independent_from_theme(
    qtbot: QtBot, tmp_path: Path
) -> None:
    preferences = PreferencesService(tmp_path / "preferences.json")
    editor = _AppearanceTab(preferences)
    qtbot.addWidget(editor)
    families = QFontDatabase.families()
    if not families:
        pytest.skip("offscreen Qt has no fonts")
    family = families[0]
    editor._family.setCurrentIndex(editor._family.findData(family))
    assert preferences.load().font_family == family
    assert theme.set_font_family(family) == family
    assert family in theme.app_stylesheet()
    theme.set_font_family("")


def test_imported_font_is_managed_and_survives_reload(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fonts_dir = Path(os.environ.get("WINDIR", "/usr/share/fonts"))
    if os.name == "nt":
        fonts_dir /= "Fonts"
    candidates = list(fonts_dir.rglob("*.ttf")) if fonts_dir.is_dir() else []
    if not candidates:
        pytest.skip("No system font files available")
    from limbowave.ui import appearance_editor
    from limbowave.ui.font_registry import family_for_file

    source = next((font for font in candidates if family_for_file(str(font))), None)
    if source is None:
        pytest.skip("No readable font file available")
    preferences = PreferencesService(tmp_path / "preferences.json")
    editor = _AppearanceTab(preferences)
    qtbot.addWidget(editor)
    monkeypatch.setattr(
        appearance_editor.QFileDialog, "getOpenFileName", lambda *args: (str(source), "")
    )
    editor._import_font()
    qtbot.waitUntil(editor.isEnabled)
    saved = preferences.load()
    assert saved.font_family in family_for_file(saved.font_file)
    assert Path(saved.font_file).parent == preferences.path.parent / "fonts"
    editor.reload()
    assert editor._family.currentData() == saved.font_family
