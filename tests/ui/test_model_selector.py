"""Real cascading-menu clicks preserve logical IDs and choose model/site atomically."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from limbowave.ui.chat_view import ChatView
from limbowave.ui.model_selector import ModelSite


@pytest.fixture
def selector(qtbot):
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1100, 740)
    view.show()
    view.set_logical_models(
        [
            ("gpt-5", "GPT 主力"),
            ("alias", "阶跃快模型"),
            ("deepseek-v3", "深度模型"),
            ("other", "自定义"),
            ("ds2", "DeepSeek 第二个"),
        ],
        "gpt-5",
        sites={
            "gpt-5": (
                ModelSite("a", "默认站", "gpt-5", default=True),
                ModelSite("b", "备用站", "gpt-5-high"),
            ),
            "alias": (
                ModelSite("c", "阶跃站", "step-3", default=True),
                ModelSite("d", "中转站", "step-3-fast"),
            ),
        },
        current_endpoint="a",
    )
    yield view, view._logical_model
    view._logical_model.hidePopup()
    qtbot.wait(160)
    assert QApplication.activePopupWidget() is None


def _open(combo, qtbot):
    combo.showPopup()
    menu = combo._menu
    assert menu is not None
    qtbot.waitUntil(menu.isVisible)
    qtbot.wait(180)
    return menu


def _submenu(combo, brand, qtbot):
    menu = _open(combo, qtbot)
    action = next(a for a in menu.actions() if a.text() == brand)
    # Use a real click, not a manually popped disconnected submenu.
    qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
    submenu = action.menu()
    qtbot.waitUntil(submenu.isVisible, timeout=2000)
    qtbot.wait(180)
    return submenu


def _sites(combo, brand, qtbot):
    models = _submenu(combo, brand, qtbot)
    action = models.actions()[0]
    qtbot.mouseClick(models, Qt.MouseButton.RightButton, pos=models.actionGeometry(action).center())
    menu = combo._site_menu
    assert menu is not None
    qtbot.waitUntil(menu.isVisible)
    return menu


def test_group_order_labels_selection_and_upward_position(selector, qtbot):
    _view, combo = selector
    menu = _open(combo, qtbot)
    assert [a.text() for a in menu.actions()] == ["Deepseek", "StepFun", "GPT", "其他"]
    assert [a.text() for a in menu.actions()[0].menu().actions()] == ["DeepSeek 第二个", "深度模型"]
    selected = next(a for a in menu.actions() if a.isChecked())
    assert selected.text() == "GPT"
    assert selected.menu().actions()[0].isChecked()
    assert menu.geometry().bottom() < combo.mapToGlobal(QPoint()).y()


def test_real_left_click_emits_logical_id(selector, qtbot):
    view, combo = selector
    models = _submenu(combo, "Deepseek", qtbot)
    action = next(a for a in models.actions() if a.data() == "deepseek-v3")
    with qtbot.waitSignal(view.logical_model_changed) as signal:
        qtbot.mouseClick(
            models, Qt.MouseButton.LeftButton, pos=models.actionGeometry(action).center()
        )
    assert signal.args == ["deepseek-v3"]
    assert combo.currentText() == "深度模型"
    assert not combo._menu.isVisible()


def test_right_click_does_not_select_model_and_site_emits_both_ids(selector, qtbot):
    view, combo = selector
    fired = []
    view.logical_model_changed.connect(fired.append)
    menu = _sites(combo, "StepFun", qtbot)
    assert combo.currentData() == "gpt-5"
    assert fired == []
    target = next(a for a in menu.actions() if a.data() == "d")
    assert "中转站" in target.text()
    with qtbot.waitSignal(view.model_site_selected) as signal:
        qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(target).center())
    assert signal.args == ["alias", "d"]
    assert fired == []
    assert not combo._menu.isVisible()
    assert not menu.isVisible()


def test_current_site_and_default_restore(selector, qtbot):
    view, combo = selector
    combo.set_sites(combo._sites, current_model="gpt-5", current_endpoint="b", overridden=True)
    menu = _sites(combo, "GPT", qtbot)
    current = next(a for a in menu.actions() if a.data() == "b")
    assert current.isChecked()
    assert not current.isEnabled()
    restore = next(a for a in menu.actions() if a.data() == "")
    assert "恢复默认" in restore.text()
    with qtbot.waitSignal(view.model_site_selected) as signal:
        restore.trigger()
    assert signal.args == ["gpt-5", ""]


def test_unbound_model_context_is_explanatory(selector, qtbot):
    _view, combo = selector
    menu = _sites(combo, "其他", qtbot)
    assert any("未绑定" in a.text() and not a.isEnabled() for a in menu.actions())
    assert not any(a.isEnabled() and not a.isSeparator() for a in menu.actions())


@pytest.mark.parametrize(
    "key,modifiers",
    [
        (Qt.Key.Key_Menu, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier),
    ],
)
def test_keyboard_context_menu(selector, qtbot, key, modifiers):
    _view, combo = selector
    models = _submenu(combo, "StepFun", qtbot)
    models.setActiveAction(models.actions()[0])
    qtbot.keyClick(models, key, modifiers)
    assert combo._site_menu is not None
    assert combo._site_menu.isVisible()
    qtbot.keyClick(combo._site_menu, Qt.Key.Key_Escape)
    assert not combo._site_menu.isVisible()
    assert models.isVisible()


def test_refresh_closes_all_open_popups_and_preserves_display_name(selector, qtbot):
    view, combo = selector
    sites = _sites(combo, "StepFun", qtbot)
    menu = combo._menu
    view.set_logical_models([("qwen-3", "通义新模型")], "qwen-3")
    assert not menu.isVisible()
    assert not sites.isVisible()
    assert combo.currentText() == "通义新模型"
    assert [a.text() for a in _open(combo, qtbot).actions()] == ["Qwen"]


def test_empty_list_is_disabled(selector):
    view, combo = selector
    view.set_logical_models([])
    assert not combo.isEnabled()
    assert combo.currentText() == "逻辑模型"
    combo.showPopup()
    assert combo._menu is None or not combo._menu.isVisible()


def test_bound_remote_id_classifies_generic_display_name(selector, qtbot):
    view, combo = selector
    view.set_logical_models(
        [("alias", "快速模型")],
        "alias",
        sites={
            "alias": (ModelSite("site", "站点", "step-3.5-flash", default=True),),
        },
    )
    menu = _open(combo, qtbot)
    assert [a.text() for a in menu.actions()] == ["StepFun"]
    assert menu.actions()[0].menu().actions()[0].text() == "快速模型"


def test_outside_click_and_reopen_do_not_leave_popup_grab(selector, qtbot):
    view, combo = selector
    _sites(combo, "StepFun", qtbot)
    popup = QApplication.activePopupWidget()
    assert popup is not None
    outside = popup.mapFromGlobal(view.mapToGlobal(QPoint(10, 10)))
    qtbot.mouseClick(popup, Qt.MouseButton.LeftButton, pos=outside)
    qtbot.waitUntil(lambda: not combo._menu.isVisible())
    _open(combo, qtbot)
    combo.hidePopup()
    assert QApplication.activePopupWidget() is None


def test_keyboard_navigation_selects_second_level_model(selector, qtbot):
    view, combo = selector
    menu = _open(combo, qtbot)
    menu.setActiveAction(menu.actions()[0])
    qtbot.keyClick(menu, Qt.Key.Key_Right)
    models = menu.actions()[0].menu()
    qtbot.waitUntil(models.isVisible)
    models.setActiveAction(next(a for a in models.actions() if a.data() == "ds2"))
    with qtbot.waitSignal(view.logical_model_changed) as signal:
        qtbot.keyClick(models, Qt.Key.Key_Return)
    assert signal.args == ["ds2"]


def test_duplicate_display_names_and_literal_ampersands_keep_distinct_ids(selector, qtbot):
    view, combo = selector
    view.set_logical_models([("gpt-a", "A&B"), ("gpt-b", "A&B")], "gpt-a")
    models = _submenu(combo, "GPT", qtbot)
    assert [a.text() for a in models.actions()] == ["A&&B", "A&&B"]
    assert [a.data() for a in models.actions()] == ["gpt-a", "gpt-b"]
    with qtbot.waitSignal(view.logical_model_changed) as signal:
        models.actions()[1].trigger()
    assert signal.args == ["gpt-b"]
    assert combo.currentText() == "A&B"


def test_escape_site_menu_then_reopen_after_deferred_deletion(selector, qtbot):
    _view, combo = selector
    for _ in range(3):
        sites = _sites(combo, "StepFun", qtbot)
        qtbot.keyClick(sites, Qt.Key.Key_Escape)
        combo.hidePopup()
        qtbot.wait(180)
    assert QApplication.activePopupWidget() is None


def test_long_model_list_stays_inside_available_screen(selector, qtbot):
    view, combo = selector
    view.set_logical_models([(f"qwen-{i}", f"通义模型 {i}") for i in range(100)])
    models = _submenu(combo, "Qwen", qtbot)
    assert len(models.actions()) == 100
    screen = models.screen().availableGeometry()
    assert models.geometry().height() <= screen.height()
    assert screen.contains(models.geometry().center())


def test_custom_menu_keeps_existing_open_frame_animation(selector, qtbot):
    from limbowave.domain.appearance import MaterialSettings
    from limbowave.ui.theme_effects import install_hover_suspension

    view, combo = selector
    install_hover_suspension(view, MaterialSettings())
    qtbot.wait(20)
    frame = combo._limbowave_combo_frame
    _open(combo, qtbot)
    qtbot.waitUntil(lambda: frame.levels[1] == 1.0, timeout=1000)
    combo.hidePopup()
    qtbot.waitUntil(lambda: frame.levels[1] == 0.0, timeout=1000)


def test_reopen_after_animation_setting_changes_does_not_keep_deleted_menu(selector, qtbot):
    _view, combo = selector
    effect = Qt.UIEffect.UI_AnimateMenu
    old = QApplication.isEffectEnabled(effect)
    try:
        QApplication.setEffectEnabled(effect, True)
        _open(combo, qtbot)
        combo.hidePopup()
        qtbot.wait(180)
        QApplication.setEffectEnabled(effect, False)
        _open(combo, qtbot)
        qtbot.wait(20)  # the previous menu's deleteLater has now run
        combo.hidePopup()
    finally:
        QApplication.setEffectEnabled(effect, old)
