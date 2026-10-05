"""The first submenu must use the same immediate native-popup path as subsequent ones."""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from limbowave.ui.model_selector import ModelSelector, ModelSite


@pytest.fixture
def picker(qtbot):
    combo = ModelSelector()
    qtbot.addWidget(combo)
    combo.resize(220, 36)
    combo.addItem("Deepseek model", "deepseek")
    combo.addItem("Qwen model", "qwen")
    combo.addItem("GPT model", "gpt")
    combo.set_sites({"deepseek": (ModelSite("site", "Site", "deepseek"),)})
    combo.setCurrentIndex(2)
    combo.move(combo.screen().availableGeometry().center())
    combo.show()
    qtbot.wait(60)
    yield combo
    combo.hidePopup()
    qtbot.wait(180)


def _hover(menu, action):
    center = menu.actionGeometry(action).center()
    # Keep the real cursor and Qt's Enter/Leave/sloppy-menu tracking consistent
    # on Windows, then clear the opening-position/no-motion guard.
    QTest.mouseMove(menu, center)
    QTest.qWait(20)
    for delta in range(8, -1, -1):
        point = center + QPoint(delta, 0)
        QApplication.sendEvent(
            menu,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(point),
                QPointF(menu.mapToGlobal(point)),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )


@pytest.mark.parametrize("fade", [False, True], ids=["scroll", "fade"])
@pytest.mark.parametrize("opening", ["click", "hover", "keyboard"])
def test_first_submenu_opens_without_native_effect_proxy(picker, qtbot, fade, opening):
    effects = (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu)
    old = [QApplication.isEffectEnabled(effect) for effect in effects]
    QApplication.setEffectEnabled(effects[0], True)
    QApplication.setEffectEnabled(effects[1], fade)
    enabled = [QApplication.isEffectEnabled(effect) for effect in effects]
    try:
        picker.showPopup()
        root = picker._menu
        qtbot.wait(180)
        first = root.actions()[0]
        child = first.menu()
        native_effects = []
        child.aboutToShow.connect(
            lambda: native_effects.append(QApplication.isEffectEnabled(effects[0]))
        )
        if opening == "keyboard":
            qtbot.keyClick(root, Qt.Key.Key_Down)
            qtbot.keyClick(root, Qt.Key.Key_Right)
        else:
            _hover(root, first)
            if opening == "click":
                qtbot.mouseClick(
                    root, Qt.MouseButton.LeftButton, pos=root.actionGeometry(first).center()
                )
        qtbot.waitUntil(lambda: bool(native_effects), timeout=2000)
        assert native_effects == [False]  # no first-child-only QRollEffect/QAlphaWidget
        assert child.isVisible()
        assert child.windowOpacity() == 1.0
        assert root.isVisible()
        assert [QApplication.isEffectEnabled(effect) for effect in effects] == enabled
        # Move into the opened child, cancelling Qt's intentional submenu-leave timer.
        point = child.actionGeometry(child.actions()[0]).center()
        QApplication.sendEvent(
            child,
            QEnterEvent(
                QPointF(point),
                QPointF(point),
                QPointF(child.mapToGlobal(point)),
            ),
        )
        _hover(child, child.actions()[0])
        # It must still be open after hover/sloppy-menu/animation timers settle.
        qtbot.wait(450)
        assert child.isVisible()
    finally:
        picker.hidePopup()
        for effect, enabled in zip(effects, old, strict=True):
            QApplication.setEffectEnabled(effect, enabled)


def test_first_brand_can_be_clicked_repeatedly_then_selected(picker, qtbot):
    picker.showPopup()
    root = picker._menu
    qtbot.wait(180)
    first = root.actions()[0]
    _hover(root, first)
    for _ in range(3):
        qtbot.mouseClick(root, Qt.MouseButton.LeftButton, pos=root.actionGeometry(first).center())
        qtbot.waitUntil(first.menu().isVisible)
        qtbot.wait(180)
        assert root.isVisible()
        assert first.menu().isVisible()
    action = first.menu().actions()[0]
    _hover(first.menu(), action)
    qtbot.mouseClick(
        first.menu(), Qt.MouseButton.LeftButton, pos=first.menu().actionGeometry(action).center()
    )
    assert picker.currentData() == "deepseek"
    assert not root.isVisible()


def test_switching_brands_and_returning_keeps_first_submenu_usable(picker, qtbot):
    picker.showPopup()
    root = picker._menu
    qtbot.wait(180)
    first, other = root.actions()[:2]
    for action in (first, other, first):
        _hover(root, action)
        qtbot.mouseClick(root, Qt.MouseButton.LeftButton, pos=root.actionGeometry(action).center())
        qtbot.waitUntil(action.menu().isVisible)
        assert not (other if action is first else first).menu().isVisible()
    qtbot.mouseClick(
        first.menu(),
        Qt.MouseButton.RightButton,
        pos=first.menu().actionGeometry(first.menu().actions()[0]).center(),
    )
    sites = picker._site_menu
    assert sites.isVisible()
    with qtbot.waitSignal(picker.model_site_selected) as signal:
        sites.actions()[-1].trigger()
    assert signal.args == ["deepseek", "site"]
    assert not root.isVisible()


def test_submenu_suppression_restores_effect_before_root_escape(picker, qtbot, monkeypatch):
    effect = Qt.UIEffect.UI_AnimateMenu
    old = QApplication.isEffectEnabled(effect)
    QApplication.setEffectEnabled(effect, True)
    enabled = QApplication.isEffectEnabled(effect)
    collapsed = []
    monkeypatch.setattr(
        picker._menu_motion,
        "collapse",
        lambda: collapsed.append(QApplication.isEffectEnabled(effect)),
    )
    try:
        picker.showPopup()
        root = picker._menu
        qtbot.wait(180)
        root.setActiveAction(root.actions()[0])
        child = root.actions()[0].menu()
        qtbot.keyClick(child, Qt.Key.Key_Escape)
        qtbot.keyClick(root, Qt.Key.Key_Escape)
        assert not root.isVisible()
        assert collapsed == ([True] if enabled else [])
        assert QApplication.isEffectEnabled(effect) == enabled
    finally:
        QApplication.setEffectEnabled(effect, old)
