"""Reveal lighting follows hover without changing input or control styles."""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QHoverEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.cursor_reveal import _RevealLayer, install_cursor_reveal


def hover(widget, x=24, y=18, kind=QEvent.Type.HoverMove):
    point = QPointF(x, y)
    QApplication.sendEvent(widget, QHoverEvent(kind, point, point, point))


def settle(layer):
    layer.animation.setCurrentTime(layer.animation.duration())


def test_dynamic_button_tracks_cursor_and_installs_once(qtbot):
    root = QWidget()
    root.resize(440, 180)
    qtbot.addWidget(root)
    install_cursor_reveal(root)
    controller = root._limbowave_cursor_reveal
    install_cursor_reveal(root)
    assert root._limbowave_cursor_reveal is controller
    button = QPushButton("Hover", root)
    button.setGeometry(20, 20, 200, 50)
    button.setStyleSheet("color: #8899aa;")
    other = QPushButton("Other", root)
    other.move(250, 20)
    root.show()
    assert button.testAttribute(Qt.WidgetAttribute.WA_Hover)
    hover(button, 35, 22)
    layer = button.findChild(_RevealLayer)
    assert layer is not None and layer.isVisible()
    assert layer.origin + QPointF(layer.pos()) == QPointF(35, 22)
    assert button.rect().contains(layer.geometry())
    hover(button, 172, 25)
    assert button.findChildren(_RevealLayer) == [layer]
    assert layer.origin + QPointF(layer.pos()) == QPointF(172, 25)
    assert button.styleSheet() == "color: #8899aa;"
    assert not other.findChildren(_RevealLayer)
    assert layer.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert layer.focusPolicy() == Qt.FocusPolicy.NoFocus
    clicks = []
    button.clicked.connect(lambda: clicks.append(True))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(40, 20))
    assert clicks == [True]
    hover(button, kind=QEvent.Type.HoverLeave)
    settle(layer)
    assert not layer.isVisible()
    assert layer.animation.state() == layer.animation.State.Stopped


def test_rows_clip_and_clear_on_blank_disabled_and_scroll(qtbot):
    view = QListWidget()
    qtbot.addWidget(view)
    view.resize(250, 160)
    view.addItems([f"Item {i}" for i in range(30)])
    view.item(2).setFlags(Qt.ItemFlag.NoItemFlags)
    install_cursor_reveal(view)
    view.show()
    viewport = view.viewport()
    for row in (0, 1):
        bounds = view.visualItemRect(view.item(row))
        hover(viewport, bounds.center().x(), bounds.center().y())
        layer = viewport.findChild(_RevealLayer)
        assert layer is not None
        assert bounds.contains(layer.geometry())
        assert layer.corner_radius == 0
    bounds = view.visualItemRect(view.item(2))
    hover(viewport, bounds.center().x(), bounds.center().y())
    settle(layer)
    assert not layer.isVisible()
    hover(viewport)
    assert layer.isVisible()
    view.verticalScrollBar().setValue(5)
    assert not layer.isVisible()
    view.clear()
    hover(viewport, 100, 110)
    assert not layer.isVisible()


@pytest.mark.parametrize("kind", ["disabled", "theme", "reveal", "checkbox", "label"])
def test_noninteractive_or_opted_out_surfaces_do_not_glow(qtbot, kind):
    root = QWidget()
    qtbot.addWidget(root)
    types = {"checkbox": QCheckBox, "label": QLabel}
    widget = types.get(kind, QPushButton)("No light", root)
    widget.resize(120, 40)
    if kind == "disabled":
        widget.setEnabled(False)
    elif kind in ("theme", "reveal"):
        widget.setProperty(f"{kind}EffectDisabled" if kind == "theme" else "revealDisabled", True)
    install_cursor_reveal(root)
    root.show()
    hover(widget)
    assert not root.findChildren(_RevealLayer)


def test_nested_inputs_and_large_surfaces_have_bounded_light(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(1000, 800)
    combo = QComboBox(root)
    combo.setEditable(True)
    combo.setGeometry(10, 10, 240, 40)
    editor = QPlainTextEdit(root)
    editor.setGeometry(10, 70, 950, 700)
    install_cursor_reveal(root)
    root.show()
    hover(combo.lineEdit())
    layer = combo.findChild(_RevealLayer)
    assert layer is not None and layer.parentWidget() is combo
    hover(editor.viewport(), 400, 300)
    editor_layer = editor.findChild(_RevealLayer)
    assert editor_layer is not None and editor_layer.parentWidget() is editor
    assert editor_layer.width() <= 200 and editor_layer.height() <= 200
    settle(layer)
    assert not layer.isVisible()


def test_resize_hide_disable_and_deactivation_clear_light(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    button = QPushButton("Hover", root)
    button.setGeometry(10, 10, 160, 50)
    install_cursor_reveal(root)
    root.show()
    hover(button)
    layer = button.findChild(_RevealLayer)
    settle(layer)
    button.resize(180, 50)
    assert not layer.isVisible()
    hover(button)
    button.setEnabled(False)
    assert not layer.isVisible()
    button.setEnabled(True)
    hover(button)
    QApplication.sendEvent(root, QEvent(QEvent.Type.WindowDeactivate))
    assert not layer.isVisible()
    hover(button)
    root.hide()
    assert not layer.isVisible()
    root.show()
    hover(button)
    button.deleteLater()
    qtbot.wait(1)
    hover(root)


def test_live_palette_changes_render_soft_light_without_solid_background(qtbot):
    previous = theme.current_palette().name
    button = QPushButton("")
    qtbot.addWidget(button)
    button.resize(320, 140)
    install_cursor_reveal(button)
    button.show()
    colors = []
    try:
        for palette in ("dark", "light"):
            theme.set_palette(palette)
            button.setStyleSheet(f"background: {theme.BG_SURFACE}; border: none;")
            hover(button, 100, 70)
            layer = button.findChild(_RevealLayer)
            settle(layer)
            image = button.grab().toImage()
            scale = image.devicePixelRatio()
            center = image.pixelColor(round(100 * scale), round(70 * scale))
            far = image.pixelColor(round(290 * scale), round(70 * scale))
            assert center != far
            assert abs(center.lightnessF() - far.lightnessF()) < 0.18
            colors.append(center)
        assert colors[0] != colors[1]
    finally:
        theme.set_palette(previous)


def test_fades_reverse_smoothly_and_stop_when_idle(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", lambda effect: True)
    button = QPushButton("Hover")
    qtbot.addWidget(button)
    install_cursor_reveal(button)
    button.show()
    hover(button)
    layer = button.findChild(_RevealLayer)
    layer.animation.setCurrentTime(60)
    partial = layer.opacity
    assert 0 < partial < 1
    hover(button, 32, 18)
    assert layer.animation.currentTime() >= 60  # Movement must not restart the fade.
    hover(button, kind=QEvent.Type.HoverLeave)
    assert layer.animation.startValue() == partial
    layer.animation.setCurrentTime(90)
    fading = layer.opacity
    assert 0 < fading < partial
    hover(button)
    assert layer.animation.startValue() == fading
    settle(layer)
    assert layer.opacity == 1
    assert layer.animation.state() == layer.animation.State.Stopped
    hover(button, kind=QEvent.Type.HoverLeave)
    settle(layer)
    assert layer.opacity == 0 and not layer.isVisible()


def test_reduced_motion_uses_static_light(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", lambda effect: False)
    button = QPushButton("Hover")
    qtbot.addWidget(button)
    install_cursor_reveal(button)
    button.show()
    hover(button)
    layer = button.findChild(_RevealLayer)
    assert layer.opacity == 1
    assert layer.animation.state() == layer.animation.State.Stopped
    hover(button, kind=QEvent.Type.HoverLeave)
    assert layer.opacity == 0 and not layer.isVisible()


def test_real_mouse_movement_and_click_ripples_coexist(qtbot):
    from limbowave.ui.click_ripple import _RippleLayer, install_click_ripples

    root = QWidget()
    qtbot.addWidget(root)
    root.resize(350, 180)
    button = QPushButton("Hover", root)
    button.setGeometry(30, 30, 200, 70)
    install_cursor_reveal(root)
    install_click_ripples(root)
    root.show()
    qtbot.mouseMove(root, QPoint(300, 140))
    qtbot.mouseMove(button, QPoint(35, 20))
    qtbot.waitUntil(lambda: button.findChild(_RevealLayer) is not None)
    layer = button.findChild(_RevealLayer)
    # Native pointer coordinates can round by one logical pixel per axis at fractional DPI.
    actual = layer.origin + QPointF(layer.pos())
    assert actual.x() == pytest.approx(35, abs=1)
    assert actual.y() == pytest.approx(20, abs=1)
    qtbot.mouseMove(button, QPoint(80, 40))
    actual = layer.origin + QPointF(layer.pos())
    assert actual.x() == pytest.approx(80, abs=1)
    assert actual.y() == pytest.approx(40, abs=1)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(80, 40))
    assert len(button.findChildren(_RippleLayer)) == 1
    assert layer.isVisible()
    qtbot.mouseMove(root, QPoint(300, 140))
    settle(layer)
    assert not layer.isVisible()


def test_hover_does_not_escape_root_or_ignore_view_opt_out(qtbot):
    root = QWidget()
    foreign = QPushButton("Foreign")
    qtbot.addWidget(root)
    qtbot.addWidget(foreign)
    view = QListWidget(root)
    view.addItem("Opt out")
    view.setProperty("revealDisabled", True)
    install_cursor_reveal(root)
    root.show()
    foreign.show()
    hover(foreign)
    hover(view.viewport())
    assert not foreign.findChildren(_RevealLayer)
    assert not view.findChildren(_RevealLayer)


def test_model_changes_remove_stale_row_light(qtbot):
    view = QListWidget()
    qtbot.addWidget(view)
    view.addItems(["First", "Second"])
    install_cursor_reveal(view)
    view.show()
    hover(view.viewport(), 24, 8)
    layer = view.viewport().findChild(_RevealLayer)
    assert layer is not None and layer.isVisible()
    view.takeItem(0)
    assert not layer.isVisible()
    hover(view.viewport(), 24, 8)
    assert layer.isVisible()
    view.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
    assert not layer.isVisible()
    view.clear()
    assert not layer.isVisible()


def test_render_stays_inside_rounded_surface_and_radial_bounds(qtbot):
    root = QWidget()
    qtbot.addWidget(root)
    root.resize(360, 180)
    root.setStyleSheet("background: #12151b;")
    button = QPushButton("", root)
    button.setGeometry(20, 20, 300, 130)
    button.setStyleSheet("background: #30343b; border: none; border-radius: 10px;")
    install_cursor_reveal(root)
    root.show()
    before = root.grab().toImage()
    hover(button, 25, 25)
    layer = button.findChild(_RevealLayer)
    settle(layer)
    after = root.grab().toImage()
    scale = after.devicePixelRatio()

    def pixel(image, x, y):
        return image.pixelColor(round(x * scale), round(y * scale))

    assert pixel(after, 45, 45) != pixel(before, 45, 45)
    # The control's rounded corner, distant surface and outside area stay intact.
    for x, y in ((20, 20), (300, 80), (16, 45), (45, 16)):
        assert pixel(after, x, y) == pixel(before, x, y)


def test_root_can_be_collected_without_dangling_global_filter(qtbot):
    import gc
    import weakref

    from shiboken6 import isValid

    root = QWidget()
    button = QPushButton("Temporary", root)
    install_cursor_reveal(root)
    root.show()
    hover(button)
    controller = root._limbowave_cursor_reveal
    layer = button.findChild(_RevealLayer)
    root_ref = weakref.ref(root)
    root.close()
    del button, root
    gc.collect()
    qtbot.wait(1)
    assert root_ref() is None
    assert not isValid(controller)
    assert not isValid(layer)


@pytest.mark.parametrize("palette", ["dark", "light"])
@pytest.mark.parametrize("kind", ["conversation", "branch", "hit"])
@pytest.mark.parametrize("selected", [False, True])
def test_sidebar_row_reveal_matches_rounded_background(qtbot, palette, kind, selected):
    from limbowave.ui.sidebar import Sidebar

    previous = theme.current_palette().name
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.resize(300, 400)
    try:
        theme.set_palette(palette)
        sidebar.setStyleSheet(theme.app_stylesheet())
        sidebar.show_conversations([("c1", "Conversation", 3)])
        if kind == "branch":
            sidebar.set_branches("c1", [("b1", "Branch", 2)])
            sidebar.toggle_branches("c1")
            qtbot.waitUntil(lambda: "c1" not in sidebar._branch_animations)
        elif kind == "hit":
            sidebar.show_search_results([("c1", "Search result")])
        install_cursor_reveal(sidebar)
        sidebar.show()
        view = sidebar._list
        item = view.item(1 if kind == "branch" else 0)
        view.setCurrentItem(item)
        item.setSelected(selected)
        viewport = view.viewport()
        bounds = view.visualItemRect(item).adjusted(2, 1, -2, -1)
        before = viewport.grab().toImage()
        hover(viewport, bounds.left() + 2, bounds.top() + 2)
        layer = viewport.findChild(_RevealLayer)
        assert layer is not None and layer.isVisible()
        settle(layer)
        assert layer.corner_radius == theme.RADIUS_MD
        assert layer.surface_rect.translated(QPointF(layer.pos())).toRect() == bounds

        after = viewport.grab().toImage()
        scale = after.devicePixelRatio()
        corners = (bounds.topLeft(), bounds.topRight(), bounds.bottomLeft(), bounds.bottomRight())
        for corner in corners:
            x, y = round(corner.x() * scale), round(corner.y() * scale)
            assert after.pixelColor(x, y) == before.pixelColor(x, y)
        x, y = round((bounds.left() + 12) * scale), round((bounds.top() + 6) * scale)
        assert after.pixelColor(x, y) != before.pixelColor(x, y)
    finally:
        theme.set_palette(previous)


def test_dynamic_tree_views_with_global_effects_do_not_reenter_construction():
    """Reproduce the request-log crash in a fresh process with real global filters."""
    import os
    import subprocess
    import sys
    import textwrap

    source = textwrap.dedent('''
        import sys
        from PySide6.QtCore import QCoreApplication, QEvent, Qt
        from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QTreeWidget
        from PySide6.QtTest import QTest
        from limbowave.ui import theme
        from limbowave.ui.checkbox_style import CheckBoxStyle
        from limbowave.ui.click_ripple import install_click_ripples
        from limbowave.ui.cursor_reveal import install_cursor_reveal
        from limbowave.ui.smooth_scroll import install_smooth_scrolling

        errors = []
        sys.excepthook = lambda typ, value, tb: errors.append(str(value)[-300:])
        app = QApplication([])
        app.setStyle(CheckBoxStyle())
        app.setStyleSheet(theme.app_stylesheet())
        install_smooth_scrolling(app)
        root = QWidget()
        layout = QVBoxLayout(root)
        install_click_ripples(root)
        install_cursor_reveal(root)
        root.show()
        for _ in range(5):
            tree = QTreeWidget(root)
            tree.setHeaderLabels(['field', 'value'])
            layout.addWidget(tree)
        QTest.qWait(50)
        assert tree.viewport().testAttribute(Qt.WidgetAttribute.WA_Hover)
        tree.setViewport(QWidget())
        QTest.qWait(10)
        assert tree.viewport().testAttribute(Qt.WidgetAttribute.WA_Hover)
        temporary = QTreeWidget(root)
        temporary.show()
        temporary.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(10)
        root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(10)
        assert not errors, errors
        print('dynamic views and teardown OK')
    ''')
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=20,
    )
    assert result.returncode == 0, (result.stdout, result.stderr[-2000:])
    assert "dynamic views and teardown OK" in result.stdout


@pytest.mark.parametrize("kind", [QEvent.Type.Show, QEvent.Type.Polish, QEvent.Type.HoverMove])
def test_reveal_ignores_destroyed_watched_widget(qtbot, kind):
    from shiboken6 import delete

    root = QWidget()
    qtbot.addWidget(root)
    install_cursor_reveal(root)
    watched = QPushButton("deleted", root)
    delete(watched)
    # Consume it: returning false would let Qt deliver to an already-deleted receiver.
    assert root._limbowave_cursor_reveal.eventFilter(watched, QEvent(kind))


def test_reveal_skips_widget_deleted_while_resolving_surface(qtbot, monkeypatch):
    from shiboken6 import delete

    from limbowave.ui import cursor_reveal

    root = QWidget()
    qtbot.addWidget(root)
    install_cursor_reveal(root)
    widget = QPushButton("temporary viewport", root)

    def replaced(surface):
        delete(surface)
        return surface

    monkeypatch.setattr(cursor_reveal, "_surface", replaced)
    root._limbowave_cursor_reveal._prepare(widget)
