from __future__ import annotations

import math
from dataclasses import replace

from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from limbowave.domain.appearance import (
    BUILTIN_THEMES,
    MaterialSettings,
    TextGlowSettings,
    replace_controls,
)
from limbowave.ui import theme
from limbowave.ui.theme_effects import apply_text_glow, install_hover_suspension


def test_text_glow_applies_to_dark_glyphs_and_skips_light_text(qtbot: QtBot) -> None:
    root = QWidget()
    layout = QVBoxLayout(root)
    dark = QLabel("dark")
    dark_palette = dark.palette()
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor("#202020"))
    dark.setPalette(dark_palette)
    light = QLabel("light")
    light_palette = light.palette()
    light_palette.setColor(QPalette.ColorRole.WindowText, QColor("#F8F8F8"))
    light.setPalette(light_palette)
    layout.addWidget(dark)
    layout.addWidget(light)
    qtbot.addWidget(root)
    apply_text_glow(root, TextGlowSettings(True, 0.2, 0.6, 2, 7))
    assert dark.graphicsEffect() is not None
    assert dark.graphicsEffect().property("limbowaveTextGlow")
    assert light.graphicsEffect() is None
    apply_text_glow(root, TextGlowSettings(enabled=False))
    assert dark.graphicsEffect() is None


def test_hover_suspension_changes_only_surface_style_and_restores(qtbot: QtBot) -> None:
    root = QWidget()
    button = QPushButton("hover", root)
    button.setStyleSheet("color: #123456;")
    qtbot.addWidget(root)
    settings = MaterialSettings(content_opacity=0.4, hover_enter_ms=0, hover_restore_ms=0)
    install_hover_suspension(root, settings)
    QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
    qtbot.wait(1)
    assert "rgba(" in button.styleSheet()
    assert "color: #123456" in button.styleSheet()
    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    qtbot.wait(1)
    assert button.styleSheet() == "color: #123456;"


def test_hover_suspension_skips_accent_buttons(qtbot: QtBot) -> None:
    """强调色按钮的底就是 ACCENT 填充，悬浮材质换底会把文字锁在浅色卡片上。

    浅色主题的 ``text_on_accent`` 是白色：底一旦被换成表面色，白字就隐形。
    """
    root = QWidget()
    flat = QPushButton("flat", root)
    accent = QPushButton("accent", root)
    accent.setProperty("accent", True)
    qtbot.addWidget(root)
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
    for button in (flat, accent):
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
    qtbot.wait(1)
    assert "rgba(" in flat.styleSheet()
    assert accent.styleSheet() == ""
    for button in (flat, accent):
        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    qtbot.wait(1)
    assert flat.styleSheet() == ""


def test_hover_does_not_repaint_background_over_backdrop(qtbot: QtBot) -> None:
    root = QWidget()
    button = QPushButton("hover", root)
    button.setStyleSheet("color: #123456;")
    qtbot.addWidget(root)
    source = BUILTIN_THEMES[0]
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
    QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
    qtbot.wait(1)
    assert "rgba(" in button.styleSheet()

    with_background = replace(source, background=replace(source.background, asset="wall.png"))
    theme.apply_appearance_theme(with_background)
    try:
        install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
        assert button.styleSheet() == "color: #123456;"
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
        qtbot.wait(1)
        assert button.styleSheet() == "color: #123456;"
    finally:
        theme.apply_appearance_theme(source)


def test_hover_installer_picks_up_dynamic_controls(qtbot: QtBot) -> None:
    root = QWidget()
    qtbot.addWidget(root)
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
    button = QPushButton("later", root)
    qtbot.wait(1)
    assert button.property("limbowaveHoverFilter")


def test_control_categories_and_tab_indicator_change_generated_qss() -> None:
    source = BUILTIN_THEMES[0]
    materials = replace_controls(source.materials, buttons=False, text_inputs=True)
    materials = replace(materials, tab_indicator_opacity=0.33)
    definition = replace(
        source,
        background=replace(source.background, asset="assets/example.png"),
        materials=materials,
        colors=replace(source.colors, tab_indicator="#12AB34"),
    )
    theme.apply_appearance_theme(definition)
    try:
        css = theme.app_stylesheet()
        # The backdrop paints/tints the panel. Child surfaces must not cover it,
        # regardless of the legacy per-control material switches.
        for selector in (
            "QWidget", "QPushButton", "QLineEdit, QPlainTextEdit", "QComboBox", "QTreeWidget",
        ):
            rule = css.split(selector, 1)[1].split("{", 1)[1].split("}", 1)[0]
            assert "background: transparent;" in rule, selector
        assert "QPushButton:hover { background: transparent;" in css
        assert "QListWidget::item:selected { background: transparent;" in css
        accent_rule = css.split('QPushButton[accent="true"] {', 1)[1].split("}", 1)[0]
        assert f"background: {theme.ACCENT};" in accent_rule
        assert f"QMenu {{ background: {theme.BG_SURFACE};" in css
        assert theme.tab_indicator_surface().startswith("rgba(18, 171, 52,")
    finally:
        theme.apply_appearance_theme(source)


def test_chrome_surfaces_keep_palette_when_no_backdrop() -> None:
    source = BUILTIN_THEMES[0]
    theme.apply_appearance_theme(source)
    css = theme.app_stylesheet()
    canvas = css.split("QWidget {", 1)[1].split("}", 1)[0]
    button = css.split("QPushButton {", 1)[1].split("}", 1)[0]
    assert f"background: {theme.BG_APP};" in canvas
    assert f"background: {theme.BG_SURFACE};" in button


def test_spin_box_hover_styles_outer_control_not_embedded_editor(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox

    root = QWidget()
    layout = QVBoxLayout(root)
    spin = QSpinBox()
    double_spin = QDoubleSpinBox()
    combo = QComboBox()
    combo.setEditable(True)
    for control in (spin, double_spin, combo):
        layout.addWidget(control)
    qtbot.addWidget(root)
    root.show()
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))

    for control in (spin, double_spin, combo):
        editor = control.findChild(QLineEdit)
        assert editor is not None
        assert not editor.property("limbowaveHoverFilter")
        QApplication.sendEvent(control, QEvent(QEvent.Type.Enter))
        QApplication.sendEvent(editor, QEvent(QEvent.Type.Enter))
        assert editor.styleSheet() == ""
    for control in (spin, double_spin):
        assert "rgba(" in control.styleSheet()


def test_all_input_styles_have_zero_margin() -> None:
    import re

    css = theme.app_stylesheet()
    for selector in (
        "QLineEdit", "QPlainTextEdit", "QTextEdit", "QSpinBox", "QDoubleSpinBox",
        "QComboBox", "QDateEdit", "QDateTimeEdit", "QTimeEdit",
    ):
        assert re.search(rf"\b{selector}\b[^{{]*\{{[^}}]*margin:\s*0px;", css), selector


def test_combo_shared_style_keeps_frame_and_open_state() -> None:
    css = theme.app_stylesheet()
    rest, emphasis_color, open_color = theme.combo_frame_colors()
    normal = css.split("QComboBox {", 1)[1].split("}", 1)[0]
    emphasis = css.split("QComboBox:hover, QComboBox:focus {", 1)[1].split("}", 1)[0]
    active = css.split("QComboBox:on {", 1)[1].split("}", 1)[0]
    drop_down = css.split("QComboBox::drop-down {", 1)[1].split("}", 1)[0]
    popup = css.split("QComboBox QAbstractItemView {", 1)[1].split("}", 1)[0]
    # The resting frame is solid so an idle combo still reads as clickable.
    assert f"border: 1px solid {rest};" in normal
    assert "padding: 7px 34px 7px 12px;" in normal
    assert f"border-color: {emphasis_color};" in emphasis
    assert "background" not in emphasis
    assert f"border-color: {open_color};" in active
    assert "border:" not in active
    assert "padding:" not in active
    assert "subcontrol-origin: padding;" in drop_down
    assert "QComboBox::drop-down:hover" not in css
    # Hovered popup items use the neutral hover surface, never the accent fill.
    assert f"selection-background-color: {theme.BG_SURFACE_HOVER};" in popup
    assert theme.ACCENT not in popup
    item_hover = css.split("QAbstractItemView::item:hover", 1)[1].split("}", 1)[0]
    assert f"background: {theme.BG_SURFACE_HOVER};" in item_hover
    assert 'QComboBox::down-arrow:on { image: url(' in css
    assert 'spin-up.svg' in css


def test_combo_hover_animates_frame_both_directions_without_surface_change(
    qtbot: QtBot,
) -> None:
    from PySide6.QtWidgets import QComboBox

    root = QWidget()
    combo = QComboBox(root)
    combo.addItems(["one", "two"])
    qtbot.addWidget(root)
    root.show()
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
    frame = combo._limbowave_combo_frame
    rest, emphasis, _ = (QColor(c) for c in theme.combo_frame_colors())
    original = combo.styleSheet()
    assert not combo.property("limbowaveHoverFilter")
    assert not frame.layer.isVisible()

    QApplication.sendEvent(combo, QEvent(QEvent.Type.Enter))
    frame._animation.setCurrentTime(frame.ENTER_MS // 2)
    assert frame.layer.isVisible()
    assert frame.layer.color not in (rest, emphasis)
    frame._animation.setCurrentTime(frame.ENTER_MS)
    assert frame.layer.color == emphasis

    QApplication.sendEvent(combo, QEvent(QEvent.Type.Leave))
    assert frame._animation.duration() == frame.RESTORE_MS
    frame._animation.setCurrentTime(frame.RESTORE_MS // 2)
    assert frame.layer.isVisible()
    assert frame.layer.color not in (rest, emphasis)
    frame._animation.setCurrentTime(frame.RESTORE_MS)
    assert not frame.layer.isVisible()
    # The animation never rewrites the combo's own stylesheet.
    assert combo.styleSheet() == original


def test_combo_open_to_closed_fades_accent_frame(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QComboBox

    root = QWidget()
    root.setStyleSheet(theme.app_stylesheet())
    combo = QComboBox(root)
    combo.addItems(["one", "two"])
    combo.setGeometry(12, 12, 180, 36)
    root.resize(204, 60)
    qtbot.addWidget(root)
    root.show()
    install_hover_suspension(root, MaterialSettings())
    frame = combo._limbowave_combo_frame
    accent = QColor(theme.ACCENT)

    combo.showPopup()
    # Windows may slide the popup open (UI_AnimateCombo); wait for the container itself.
    qtbot.waitUntil(combo.view().isVisible)
    frame._animation.setCurrentTime(frame.ENTER_MS)
    assert frame.layer.color == accent
    image = combo.grab().toImage()
    middle = image.height() // 2  # grab() images are in device pixels
    assert image.pixelColor(0, middle) == accent

    combo.hidePopup()
    combo.clearFocus()
    qtbot.waitUntil(lambda: not combo.view().isVisible())
    assert frame._animation.state() == frame._animation.State.Running
    assert frame._animation.duration() == frame.RESTORE_MS
    frame._animation.setCurrentTime(frame.RESTORE_MS // 2)
    midway = combo.grab().toImage().pixelColor(0, middle)
    assert midway != accent
    assert midway != QColor(theme.combo_frame_colors()[0])
    frame._animation.setCurrentTime(frame.RESTORE_MS)
    assert not frame.layer.isVisible()


def test_frameless_combo_fades_frame_in_from_transparent(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QComboBox

    root = QWidget()
    combo = QComboBox(root)
    combo.setProperty("comboFramelessAtRest", True)
    combo.setStyleSheet("QComboBox { border: none; padding: 3px 18px 3px 4px; }")
    qtbot.addWidget(root)
    root.show()
    install_hover_suspension(root, MaterialSettings())
    frame = combo._limbowave_combo_frame

    QApplication.sendEvent(combo, QEvent(QEvent.Type.Enter))
    frame._animation.setCurrentTime(frame.ENTER_MS // 4)
    assert 0 < frame.layer.color.alpha() < 128
    frame._animation.setCurrentTime(frame.ENTER_MS)
    assert frame.layer.color == QColor(theme.combo_frame_colors()[1])


def test_combo_frame_is_visible_on_both_sides_when_unfocused(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QComboBox

    root = QWidget()
    root.setStyleSheet(theme.app_stylesheet())
    combo = QComboBox(root)
    combo.addItems(["one", "two"])
    combo.setGeometry(12, 12, 150, 36)
    root.resize(174, 60)
    qtbot.addWidget(root)
    root.show()
    combo.clearFocus()
    QApplication.processEvents()
    image = combo.grab().toImage()
    middle = image.height() // 2  # grab() images are in device pixels
    left = image.pixelColor(0, middle)
    right = image.pixelColor(image.width() - 1, middle)
    inside = image.pixelColor(2, middle)
    assert left == right
    assert left != inside


def test_combo_open_frame_matches_click_target(qtbot: QtBot) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QComboBox

    root = QWidget()
    root.setStyleSheet(theme.app_stylesheet())
    combo = QComboBox(root)
    combo.addItems(["one", "two"])
    combo.setGeometry(12, 12, 180, 36)
    root.resize(204, 60)
    qtbot.addWidget(root)
    root.show()
    QApplication.processEvents()

    for x in (1, 30, combo.width() - 2):
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton, pos=QPoint(x, combo.height() // 2))
        # Windows may slide the popup open (UI_AnimateCombo); wait for the container itself.
        qtbot.waitUntil(combo.view().isVisible)
        image = combo.grab().toImage()
        # grab() images are in device pixels, not the combo's logical size.
        middle, last = image.height() // 2, image.width() - 1
        assert image.pixelColor(0, middle) == QColor(theme.ACCENT)
        assert image.pixelColor(last, middle) == QColor(theme.ACCENT)
        assert image.pixelColor(0, 0) != QColor(theme.ACCENT)  # rounded corners
        assert image.pixelColor(last, 0) != QColor(theme.ACCENT)
        combo.hidePopup()
        QApplication.processEvents()
        closed = combo.grab().toImage()
        assert closed.pixelColor(0, middle) != QColor(theme.ACCENT)
        assert closed.pixelColor(last, middle) != QColor(theme.ACCENT)


def test_combo_hover_and_open_do_not_move_following_widget(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QComboBox, QVBoxLayout

    root = QWidget()
    root.setStyleSheet(theme.app_stylesheet())
    layout = QVBoxLayout(root)
    combo = QComboBox()
    combo.addItems(["one", "two"])
    following = QLabel("following control")
    layout.addWidget(combo)
    layout.addWidget(following)
    qtbot.addWidget(root)
    root.show()
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
    QApplication.processEvents()
    initial = (combo.height(), following.y())

    QApplication.sendEvent(combo, QEvent(QEvent.Type.Enter))
    QApplication.processEvents()
    assert (combo.height(), following.y()) == initial
    hovered = combo.grab().toImage()
    middle = hovered.height() // 2  # grab() images are in device pixels
    # A 1px frame covers up to ceil(dpr) device pixels (anti-aliased at 1.5x); past
    # that the surface must be flat, i.e. hover/open never repaint the background.
    inside = math.ceil(hovered.devicePixelRatio()) + 1
    assert hovered.pixelColor(0, middle) != hovered.pixelColor(inside, middle)
    assert hovered.pixelColor(inside, middle) == hovered.pixelColor(inside + 1, middle)

    combo.showPopup()
    qtbot.waitUntil(combo.view().isVisible)
    assert (combo.height(), following.y()) == initial
    opened = combo.grab().toImage()
    assert opened.pixelColor(0, middle) != opened.pixelColor(inside, middle)
    assert opened.pixelColor(inside, middle) == opened.pixelColor(inside + 1, middle)

    combo.hidePopup()
    QApplication.sendEvent(combo, QEvent(QEvent.Type.Leave))
    QApplication.processEvents()
    assert (combo.height(), following.y()) == initial
