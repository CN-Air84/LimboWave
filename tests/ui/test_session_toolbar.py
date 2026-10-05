"""会话工具栏验收（Phase 10 / §三.1、Task 8.2/8.3）。

锁住的不变量：
- 工具栏展示模型/站点/占用，思考强度可选且变更外发；
- 收起/展开切换宽度与可见性；响应式退化只在变窄时收起（不自动展开）；
- 高级栏只保留压缩、模式、步骤与记忆，不再显示详情入口；
- 站点覆盖/回退确认等动作以信号外发（业务在应用层）。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QPushButton
from pytestqt.qtbot import QtBot

from limbowave.ui import soft_shadow
from limbowave.ui.main_window import MainWindow
from limbowave.ui.session_toolbar import (
    NOTICE_MS,
    SLIDER_CONFIRM_MS,
    THINKING_LEVELS,
    TOOLBAR_WIDTH,
    RouteCandidate,
    SessionToolbar,
    ToolbarShadowLayer,
)


def test_shows_endpoint_only_in_advanced_bar(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    from PySide6.QtWidgets import QPushButton

    bar.set_route_candidates(
        [
            RouteCandidate(
                "relay-a", "中转站 A", "gpt-4o", "默认", current=True
            ),
            RouteCandidate("relay-b", "中转站 B", "gpt-4o-mini", "备用 1"),
        ]
    )
    bar.set_model_info("deepseek-chat", "中转站 A")
    # 「实际站点」是可点值；点击呼出独立悬浮窗（栏宽有限，选择在悬浮窗里做）
    endpoint = bar.findChild(QPushButton, "value-实际站点")
    assert endpoint is not None
    assert endpoint.text() == "中转站 A"  # 展示用显示名，不是 id
    endpoint.click()
    panel = bar._endpoint_panel
    assert panel is not None and panel.isVisible()
    combo = panel.findChild(QComboBox)
    assert combo is not None
    # 悬浮窗里是全宽下拉框，当前项带完整绑定信息与勾选；展示用显示名
    assert "中转站 A → gpt-4o" in combo.currentText()
    assert "relay-a" not in combo.currentText()
    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    assert model.item(0).checkState() == Qt.CheckState.Checked
    panel.close_panel()
    # 关窗即清引用：之后还能再开（死包装引用曾让按钮永久失灵）
    assert bar._endpoint_panel is None
    endpoint.click()
    panel2 = bar._endpoint_panel
    assert panel2 is not None and panel2.isVisible() and panel2 is not panel
    panel2.close_panel()
    assert bar.findChild(QPushButton, "value-逻辑模型") is None


def test_advanced_bar_uses_composer_soft_shadow(qtbot: QtBot) -> None:
    """高级栏的投影与输入框同款：栏上不再挂 QGraphicsDropShadowEffect，
    柔和晕染由外置投影层绘制，参数与观感都来自 soft_shadow。"""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

    bar = SessionToolbar()
    qtbot.addWidget(bar)
    assert bar.graphicsEffect() is None or not isinstance(
        bar.graphicsEffect(), QGraphicsDropShadowEffect
    )

    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    bar.setParent(host)
    bar.setGeometry(100, 100, TOOLBAR_WIDTH, 200)
    layer = ToolbarShadowLayer(host)
    qtbot.addWidget(layer)
    layer.sync_geometry(bar, 1.0)

    assert layer.isVisibleTo(host)
    # 投影层的几何 = 栏几何外扩 soft_shadow 的扩散范围
    expected = QRect(100, 100, TOOLBAR_WIDTH, 200).adjusted(
        -soft_shadow.SHADOW_SPREAD,
        soft_shadow.SHADOW_OFFSET - soft_shadow.SHADOW_SPREAD,
        soft_shadow.SHADOW_SPREAD,
        soft_shadow.SHADOW_OFFSET + soft_shadow.SHADOW_SPREAD,
    )
    assert layer.geometry() == expected

    # 展开到一半：投影跟着当前露出的宽度，右缘也保留柔和扩散。
    bar.setMask(QRect(0, 0, TOOLBAR_WIDTH // 2, 200))
    layer.sync_geometry(bar, 0.5)
    assert layer.geometry() == soft_shadow.shadow_bounds(QRect(100, 100, TOOLBAR_WIDTH // 2, 200))


def test_advanced_shadow_fades_and_retracts_with_progress(qtbot: QtBot) -> None:
    """实际像素随进度渐显 / 渐隐，而不只检查控件的动画属性。"""
    from PySide6.QtGui import QImage, QRegion
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    qtbot.addWidget(host)
    host.resize(600, 400)
    bar = SessionToolbar(host)
    bar.setGeometry(100, 100, TOOLBAR_WIDTH, 200)
    layer = ToolbarShadowLayer(host)
    host.show()
    alphas = []
    for progress in (0.25, 0.5, 1.0, 0.5, 0.25):
        layer.sync_geometry(bar, progress)
        image = QImage(layer.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        layer.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        # 下沿中央：避开圆角，几何宽度变化不应改变采样点的叠层数。
        alphas.append(image.pixelColor(image.width() // 2, image.height() - 6).alpha())
        assert layer.width() == round(TOOLBAR_WIDTH * progress) + 2 * soft_shadow.SHADOW_SPREAD
        # 投影只在表面之外；右边缘仍有柔和晕染，不被垂直切断。
        assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() == 0
        assert image.pixelColor(image.width() - 6, image.height() // 2).alpha() > 0

    assert 0 < alphas[0] < alphas[1] < alphas[2]
    assert alphas[3:] == alphas[1::-1]
    layer.sync_geometry(bar, 0.0)
    assert layer.isHidden()


def test_thinking_levels_are_contract_values(qtbot: QtBot) -> None:
    """等级取值来自合同 §1.2（off/minimal/low/medium/high/xhigh/max）。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    assert bar._thinking.count() == len(THINKING_LEVELS)
    assert bar._thinking.itemText(0) == "off"
    assert bar._thinking.itemText(len(THINKING_LEVELS) - 1) == "max"


def test_thinking_change_emits_signal(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    with qtbot.waitSignal(bar.thinking_level_changed, timeout=1000) as blocker:
        bar._thinking.setCurrentText("high")
    assert blocker.args == ["high"]


def test_programmatic_level_set_does_not_emit(qtbot: QtBot) -> None:
    """程序化同步当前等级（如从内核读回）不该被当成用户操作。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    fired: list[str] = []
    bar.thinking_level_changed.connect(fired.append)
    bar.set_thinking_level("medium")
    assert fired == []
    assert bar._thinking.currentText() == "medium"


def test_collapse_and_expand(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    assert bar.width() == TOOLBAR_WIDTH
    assert not bar.collapsed

    bar.set_collapsed(True)
    assert bar.collapsed
    assert bar.width() < TOOLBAR_WIDTH
    assert not bar._collapse_btn.isVisible()
    assert bar._expand_btn.isVisible()

    bar.set_collapsed(False)
    assert bar.width() == TOOLBAR_WIDTH
    assert bar._collapse_btn.isVisible()


def test_auto_collapse_only_narrows(qtbot: QtBot) -> None:
    """窄窗口自动收起；变宽**不**自动展开（否则用户手动收起会被覆盖）。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    bar.maybe_auto_collapse(600)
    assert bar.collapsed

    bar.maybe_auto_collapse(1600)
    assert bar.collapsed  # 保持用户/自动的收起状态


def test_context_usage_text(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_context_usage("约 45%（估算）")
    assert "45%" in bar._compress_btn.toolTip()


def test_advanced_actions_exclude_details(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)
    bar.show()

    assert [button.text() for button in bar._action_buttons] == ["压缩", "模式", "步骤", "记忆"]
    assert all(button.text() != "详情" for button in bar.findChildren(QPushButton))
    assert all(button.isVisible() and button.isEnabled() for button in bar._action_buttons)


def test_action_signals(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    for signal in (
        bar.compress_requested,
        bar.attach_requested,
    ):
        with qtbot.waitSignal(signal, timeout=1000):
            signal.emit()


def test_session_controls_disabled_without_session(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(False)
    assert not bar._thinking.isEnabled()
    bar.set_session_available(True)
    assert bar._thinking.isEnabled()


# ---------- 实际站点悬浮窗（FloatingPanel + 全宽下拉框） ----------


def _endpoint_panel_combo(bar: SessionToolbar) -> QComboBox:
    panel = bar._endpoint_panel
    assert panel is not None
    combo = panel.findChild(QComboBox)
    assert combo is not None
    return combo


def _popup_row_click(combo: QComboBox, row: int, *, glyph: bool = False) -> None:
    """真实点击弹层行（行文本，或勾选块）。"""
    view = combo.view()
    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    rect = view.visualRect(model.index(row, 0))
    pos = (
        QPoint(rect.left() + 8, rect.center().y()) if glyph else rect.center()
    )
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)


def test_endpoint_panel_combo_select_emits_override_and_closes(qtbot: QtBot) -> None:
    """真实点击弹层行：外发 endpoint_id、勾选跟选择走、选完即关窗。

    用真实鼠标事件而不是 setCurrentIndex——Qt 对带勾选项的弹层，
    setCurrentIndex 语义与真实点击完全不同（后者默认不产生选中，探针实测）。
    """
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    bar.set_route_candidates(
        [
            RouteCandidate("relay-a", "中转站 A", "gpt-4o", "默认", current=True),
            RouteCandidate("relay-b", "中转站 B", "gpt-4o-mini", "备用 1"),
            RouteCandidate("relay-c", "中转站 A", "gpt-4o", "默认", restore=True),
        ]
    )
    from PySide6.QtWidgets import QPushButton

    bar.findChild(QPushButton, "value-实际站点").click()
    combo = _endpoint_panel_combo(bar)
    panel = bar._endpoint_panel
    assert panel is not None
    model = combo.model()
    assert isinstance(model, QStandardItemModel)

    closed: list[bool] = []
    panel.closed.connect(lambda: closed.append(True))
    with qtbot.waitSignal(bar.endpoint_override_requested, timeout=1000) as blocker:
        _popup_row_click(combo, 1)
    assert blocker.args == ["relay-b"]  # 选中值仍是站点 id，不是显示名
    assert "中转站 B" in model.item(1).text()
    assert model.item(1).checkState() == Qt.CheckState.Checked  # 勾选跟选择走
    # 选完即关窗（closed 在选中处理里同步发出）
    assert closed == [True]

    # 再开一次：恢复默认行外发空串（清除覆盖回默认）
    bar.findChild(QPushButton, "value-实际站点").click()
    combo2 = _endpoint_panel_combo(bar)
    assert combo2 is not combo
    with qtbot.waitSignal(bar.endpoint_override_requested, timeout=1000) as blocker:
        _popup_row_click(combo2, 2)
    assert blocker.args == [""]


def test_endpoint_panel_checkbox_glyph_click_also_picks(qtbot: QtBot) -> None:
    """点勾选块同样换站点：默认语义只翻勾选不选中，接管后与行点击一致。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    bar.set_route_candidates(
        [
            RouteCandidate("relay-a", "中转站 A", "gpt-4o", "默认", current=True),
            RouteCandidate("relay-b", "中转站 B", "gpt-4o-mini", "备用 1"),
        ]
    )
    from PySide6.QtWidgets import QPushButton

    bar.findChild(QPushButton, "value-实际站点").click()
    combo = _endpoint_panel_combo(bar)
    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    with qtbot.waitSignal(bar.endpoint_override_requested, timeout=1000) as blocker:
        _popup_row_click(combo, 1, glyph=True)
    assert blocker.args == ["relay-b"]
    assert model.item(1).checkState() == Qt.CheckState.Checked


def test_endpoint_panel_close_closes_combo_popup_first(qtbot: QtBot) -> None:
    """关面板前先收掉还开着的下拉弹层：带着激活 popup 销毁面板会留下
    悬空的弹出链和鼠标抓取，之后「实际站点」按钮点不开（用户实测）。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    bar.set_route_candidates(
        [
            RouteCandidate("relay-a", "中转站 A", "gpt-4o", "默认", current=True),
            RouteCandidate("relay-b", "中转站 B", "gpt-4o-mini", "备用 1"),
        ]
    )
    from PySide6.QtWidgets import QPushButton

    bar.findChild(QPushButton, "value-实际站点").click()
    combo = _endpoint_panel_combo(bar)
    combo.showPopup()
    popup_window = combo.view().window()
    assert popup_window.isVisible()
    panel = bar._endpoint_panel
    assert panel is not None
    panel.close_panel()
    # closed 在面板销毁前同步发出：弹层先收掉，不留悬空抓取
    assert not popup_window.isVisible()


def test_endpoint_panel_without_candidates_shows_note_only(qtbot: QtBot) -> None:
    """无候选（未配置模型等）时下拉框只剩状态说明且不可选。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    bar.set_route_candidates((), note="（未配置模型）")
    from PySide6.QtWidgets import QPushButton

    bar.findChild(QPushButton, "value-实际站点").click()
    combo = _endpoint_panel_combo(bar)
    assert combo.currentText() == "（未配置模型）"
    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    placeholder = model.item(0)
    assert placeholder is not None and not placeholder.isEnabled()


def test_endpoint_candidates_update_does_not_emit(qtbot: QtBot) -> None:
    """候选灌入/展示更新（内核读回、配置热更、失败回弹）不该被当成用户操作。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    fired: list[str] = []
    bar.endpoint_override_requested.connect(fired.append)
    bar.set_route_candidates(
        [RouteCandidate("relay-a", "中转站 A", "gpt-4o", "默认", current=True)]
    )
    bar.set_model_info("deepseek-chat", "中转站 A")
    bar.set_route_candidates(
        [
            RouteCandidate(
                "relay-b", "中转站 B", "gpt-4o-mini", "备用 1", current=True, override=True
            )
        ]
    )
    bar.set_model_info("deepseek-chat", "中转站 B（会话覆盖）")
    assert fired == []
    from PySide6.QtWidgets import QPushButton

    assert bar.findChild(QPushButton, "value-实际站点").text() == "中转站 B（会话覆盖）"
    # 悬浮窗还没开过：不存在残留面板
    assert bar._endpoint_panel is None


def test_detected_thinking_capability_controls_selector(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)

    bar.set_thinking_capability(False)
    assert bar._thinking.isEnabled()  # 仍可展开并长按不可用项
    assert not bar._thinking.model().item(4).isEnabled()
    assert "不支持思考" in bar._thinking.toolTip()

    bar.set_thinking_capability(True, locked_level="xhigh")
    assert bar._thinking.isEnabled()
    assert not bar._thinking.model().item(4).isEnabled()
    assert bar._thinking.currentText() == "xhigh"
    assert "不可切换" in bar._thinking.toolTip()

    bar.set_thinking_capability(True)
    assert bar._thinking.isEnabled()


def test_verified_levels_disable_unavailable_choices(qtbot: QtBot) -> None:
    from PySide6.QtGui import QStandardItemModel

    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)
    bar.set_thinking_capability(True, available_levels=("low", "high", "max"))
    model = bar._thinking.model()
    assert isinstance(model, QStandardItemModel)
    assert [model.item(i).isEnabled() for i in range(bar._thinking.count())] == [
        True,
        False,
        True,
        False,
        True,
        False,
        True,
    ]
    bar.set_thinking_capability(None)
    assert all(model.item(i).isEnabled() for i in range(bar._thinking.count()))


def _slot_x(bar: SessionToolbar) -> list[int]:
    return [slot.x() for slot in bar._action_row._slots()]


def _settle(qtbot: QtBot, bar: SessionToolbar) -> None:
    qtbot.waitUntil(lambda: not bar._action_row.animating, timeout=2000)


def test_mode_notice_slides_mode_button_first_then_restores(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = _slot_x(bar)
    assert [b.x() for b in bar._action_buttons] == slots

    bar.show_mode_notice("终端模式")
    # 动画进行中：按钮还在路上，其余按钮仍可见（正在渐隐）
    assert bar._action_row.animating
    assert bar._compress_btn.isVisible()
    _settle(qtbot, bar)

    assert bar.notice_text == "终端模式"
    assert bar._mode_btn.x() == slots[0]
    assert bar._mode_btn.isVisible()
    for button in (bar._compress_btn, bar._steps_btn, bar._memory_btn):
        assert not button.isVisible()
    assert bar._action_row.notice.x() > bar._mode_btn.geometry().right()

    bar._end_notice()
    _settle(qtbot, bar)

    assert bar.notice_text == ""
    assert not bar._action_row.notice.isVisible()
    assert [b.x() for b in bar._action_buttons] == slots
    assert all(button.isVisible() for button in bar._action_buttons)
    assert all(b.graphicsEffect().opacity() == 1.0 for b in bar._action_buttons)


def test_steps_notice_uses_steps_button_and_expires(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = _slot_x(bar)

    bar.show_steps_notice("已隐藏工具步骤")
    _settle(qtbot, bar)

    assert bar.notice_text == "已隐藏工具步骤"
    assert bar._steps_btn.x() == slots[0]
    assert not bar._mode_btn.isVisible()
    qtbot.waitUntil(lambda: bar.notice_text == "", timeout=NOTICE_MS + 1500)
    _settle(qtbot, bar)
    assert all(button.isVisible() for button in bar._action_buttons)
    assert bar._steps_btn.x() == slots[2]


def test_switching_notice_source_mid_animation_ends_consistent(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = _slot_x(bar)

    bar.show_mode_notice("终端模式")
    bar.show_steps_notice("已显示工具步骤")  # 进场动画还没跑完就换了来源
    _settle(qtbot, bar)

    assert bar.notice_text == "已显示工具步骤"
    assert bar._steps_btn.x() == slots[0]
    assert not bar._mode_btn.isVisible()
    assert not bar._compress_btn.isVisible()


def test_action_buttons_fit_text_without_clipping(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = bar._action_row._slots()
    # 格与格之间不留缝，四格铺满整行
    assert slots[-1].right() + 1 >= bar._action_row.width() - len(slots)
    for button, slot in zip(bar._action_buttons, slots, strict=True):
        text_width = button.fontMetrics().horizontalAdvance(button.text())
        assert text_width + 2 <= slot.width()  # 2 = 左右各 1px 边框


def test_reclick_crossfades_notice_text(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    row = bar._action_row
    bar.show_mode_notice("终端模式")
    _settle(qtbot, bar)

    bar.show_mode_notice("内置工具模式")

    # 换字走动画：先渐隐，旧字还在，按钮不动
    assert row.animating
    assert row.notice.text() == "终端模式"
    qtbot.waitUntil(lambda: row.notice.text() == "内置工具模式", timeout=2000)
    _settle(qtbot, bar)
    assert bar.notice_text == "内置工具模式"
    assert row.notice.graphicsEffect().opacity() == 1.0
    assert bar._mode_btn.x() == _slot_x(bar)[0]
    assert not bar._compress_btn.isVisible()


def test_long_notice_is_elided_with_full_tooltip(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    row = bar._action_row
    text = "终端模式（Powershell5.1兼容）" * 3  # 保证放不下

    bar.show_mode_notice(text)
    _settle(qtbot, bar)

    assert bar.notice_text == text
    assert row.notice.text() != text
    assert row.notice.text().endswith("…")
    assert row.notice.toolTip() == text


def test_fallback_shell_is_named_in_mode_tooltip(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_execution_mode("terminal", is_fallback_shell=True)
    assert "Powershell5.1兼容" in bar._mode_btn.toolTip()


# ---------- 压缩滑动确认 ----------


def _enter_slider(qtbot: QtBot, bar: SessionToolbar) -> None:
    bar._compress_btn.click()
    _settle(qtbot, bar)


def _drag_slider(qtbot: QtBot, bar: SessionToolbar, fraction: float, *, release: bool) -> None:
    """在滑块上模拟一次拖动：按下手柄、平移到 ``fraction`` 处，按需松开。"""
    slider = bar._action_row.slider
    y = slider.height() // 2
    start_x = int(slider._INSET + slider._diameter() / 2)
    end_x = start_x + int(slider._span() * fraction) + 2  # +2 补整数取整的舍入
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(start_x, y))
    qtbot.mouseMove(slider, QPoint(end_x, y))
    if release:
        qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(end_x, y))


def test_compress_click_enters_slider_confirm_without_compressing(qtbot: QtBot) -> None:
    """点压缩按钮不直接压缩：其余按钮渐隐、滑块出现在右侧，超时后原路退回。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = _slot_x(bar)

    with qtbot.assertNotEmitted(bar.compress_requested):
        _enter_slider(qtbot, bar)

    assert bar._action_row.slider_active
    assert bar._action_row.slider.isVisible()
    assert bar.notice_text == ""
    assert not bar._action_row.notice.isVisible()
    assert bar._compress_btn.x() == slots[0]
    for button in (bar._mode_btn, bar._steps_btn, bar._memory_btn):
        assert not button.isVisible()
    assert bar._action_row.slider.graphicsEffect().opacity() == 1.0

    bar._on_slider_timeout()  # 5 秒到点：取消并退场
    _settle(qtbot, bar)
    assert not bar._action_row.slider_active
    assert not bar._action_row.slider.isVisible()
    assert [b.x() for b in bar._action_buttons] == slots
    assert all(button.isVisible() for button in bar._action_buttons)
    assert all(b.graphicsEffect().opacity() == 1.0 for b in bar._action_buttons)


def test_slider_complete_emits_compress_once_and_restores(qtbot: QtBot) -> None:
    """滑块拖到最右 = 确认：只发一次 compress_requested，随后按钮复位。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = _slot_x(bar)
    _enter_slider(qtbot, bar)

    emissions: list[int] = []
    bar.compress_requested.connect(lambda: emissions.append(1))
    with qtbot.waitSignal(bar.compress_requested, timeout=1000):
        _drag_slider(qtbot, bar, 1.0, release=True)

    assert emissions == [1]
    _settle(qtbot, bar)
    assert not bar._action_row.slider_active
    assert not bar._action_row.slider.isVisible()
    assert [b.x() for b in bar._action_buttons] == slots
    assert all(button.isVisible() for button in bar._action_buttons)


def test_slider_partial_release_snaps_back_and_keeps_waiting(qtbot: QtBot) -> None:
    """中途松开不算确认：手柄弹回起点，确认态继续等（计时器不重置）。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    with qtbot.assertNotEmitted(bar.compress_requested):
        _enter_slider(qtbot, bar)
        _drag_slider(qtbot, bar, 0.5, release=True)
        qtbot.waitUntil(lambda: bar._action_row.slider.progress == 0.0, timeout=1000)
    assert bar._action_row.slider_active
    assert bar._slider_timer.isActive()


def test_slider_timeout_cancels_without_compressing(qtbot: QtBot) -> None:
    """5 秒内没滑到最右按取消处理：退场动画收掉，不发压缩意图。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    assert bar._slider_timer.interval() == SLIDER_CONFIRM_MS == 5000
    _enter_slider(qtbot, bar)

    with qtbot.assertNotEmitted(bar.compress_requested):
        bar._on_slider_timeout()
        _settle(qtbot, bar)

    assert not bar._action_row.slider_active
    assert not bar._action_row.slider.isVisible()
    assert all(button.isVisible() for button in bar._action_buttons)


def test_compress_reclick_cancels_slider_confirm(qtbot: QtBot) -> None:
    """确认态里再点一次压缩按钮 = 取消。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    _enter_slider(qtbot, bar)

    with qtbot.assertNotEmitted(bar.compress_requested):
        bar._compress_btn.click()  # 再点一次
        _settle(qtbot, bar)

    assert not bar._action_row.slider_active
    assert not bar._action_row.slider.isVisible()
    assert all(button.isVisible() for button in bar._action_buttons)


def test_mode_notice_takes_over_from_slider_confirm(qtbot: QtBot) -> None:
    """确认态被模式提示接管：滑块无动画退场，提示照常进场。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.show()
    slots = _slot_x(bar)
    _enter_slider(qtbot, bar)

    bar.show_mode_notice("终端模式")
    _settle(qtbot, bar)

    assert not bar._action_row.slider_active
    assert not bar._action_row.slider.isVisible()
    assert not bar._slider_timer.isActive()
    assert bar.notice_text == "终端模式"
    assert bar._mode_btn.x() == slots[0]
    assert not bar._compress_btn.isVisible()


def test_compress_tooltip_reflects_confirm_state(qtbot: QtBot) -> None:
    """提示跟随状态：常态说明两段式用法，确认态提示取消方式，占用详情不丢。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_context_usage("约 45%（估算）")
    assert "45%" in bar._compress_btn.toolTip()
    assert "滑动确认" in bar._compress_btn.toolTip()

    _enter_slider(qtbot, bar)
    assert "取消" in bar._compress_btn.toolTip()

    bar._on_slider_timeout()
    assert "45%" in bar._compress_btn.toolTip()


# ---------- 像素级回归：按钮视觉隐形 ----------


def test_action_buttons_start_fully_opaque(qtbot: QtBot) -> None:
    """按钮的透明度效果初始就得是 1.0（QGraphicsOpacityEffect 默认 0.7，
    首次提示动画前按钮会整体偏淡三成）。"""
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    for button in bar._action_buttons:
        effect = button.graphicsEffect()
        assert effect is not None and effect.opacity() == 1.0


def test_toolbar_never_nests_parent_graphics_effect(qtbot: QtBot) -> None:
    """嵌套图形效果是「动画后按钮视觉隐形」的根因：工具栏（父）一旦挂上
    QGraphicsDropShadowEffect 之类，子按钮自己的 QGraphicsOpacityEffect 在
    hide/show 之后不再触发父级重绘，按钮 isVisible()=True 却画不出来。
    投影由外置 ToolbarShadowLayer 绘制，栏上永远不许有图形效果。"""
    from PySide6.QtWidgets import QGraphicsEffect

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message("hi")
    qtbot.waitUntil(
        lambda: window.chat.composer_lift == 0 and window.chat.advanced_progress == 1.0,
        timeout=2000,
    )

    assert window.toolbar.graphicsEffect() is None or not isinstance(
        window.toolbar.graphicsEffect(), QGraphicsEffect
    )


def test_buttons_render_pixels_after_notice_animation(qtbot: QtBot) -> None:
    """端到端像素守卫：真实主窗口里点四个按钮、跑完业务与全部动画后，
    每个按钮区域的渲染对比度必须非零（isVisible() 在嵌套图形效果下
    依旧为 True，只有像素层面能查出「隐形」）。"""
    import math

    from PySide6.QtCore import QPoint, QRect

    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    chat = window.chat
    chat.add_user_message("看下部署顺序")
    chat.begin_assistant()
    qtbot.waitUntil(lambda: chat.composer_lift == 0 and chat.advanced_progress == 1.0, timeout=2000)
    chat.end_assistant("先起数据库，再起应用。")
    toolbar = window.toolbar

    def contrast(button) -> float:
        image = window.grab().toImage()
        dpr = image.devicePixelRatioF() or 1.0
        top_left = button.mapTo(window, QPoint(0, 0))
        crop = image.copy(
            QRect(
                round(top_left.x() * dpr),
                round(top_left.y() * dpr),
                max(1, round(button.width() * dpr)),
                max(1, round(button.height() * dpr)),
            )
        )
        lum = [
            0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            for y in range(crop.height())
            for x in range(crop.width())
            for c in (crop.pixelColor(x, y),)
        ]
        if not lum:
            return 0.0
        mean = sum(lum) / len(lum)
        return math.sqrt(sum((v - mean) ** 2 for v in lum) / len(lum))

    def all_buttons_visible() -> bool:
        return all(contrast(b) > 1.0 for b in toolbar._action_buttons)

    assert all_buttons_visible()  # 基线

    for notice in (
        lambda: toolbar.show_mode_notice("终端模式"),
        lambda: toolbar.show_steps_notice("已隐藏工具步骤"),
    ):
        notice()
        qtbot.waitUntil(lambda: not toolbar._action_row.animating, timeout=2000)
        qtbot.wait(300)  # NOTICE_MS 由单-shot 定时器收尾，这里直接手动收回
        toolbar._end_notice()
        qtbot.waitUntil(lambda: not toolbar._action_row.animating, timeout=2000)
        assert all_buttons_visible()

    # 站点/记忆：弹悬浮面板再关掉，动画结束后按钮必须还在
    from PySide6.QtWidgets import QLabel

    from limbowave.ui.floating import FloatingPanel

    for title in ("实际站点", "记忆"):
        panel = FloatingPanel(window, title, width=420)
        panel.content_layout.addWidget(QLabel("内容", panel), 1)
        panel.popup()
        qtbot.wait(250)
        panel.close_panel()
        qtbot.wait(250)
        assert all_buttons_visible()


def test_export_button_emits_request(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    button = next(b for b in bar.findChildren(QPushButton) if b.text() == "导出会话")
    with qtbot.waitSignal(bar.export_requested):
        button.click()


def test_unavailable_thinking_levels_have_gray_overlay(qtbot: QtBot) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)
    bar.set_thinking_capability(True, available_levels=("low", "high"))
    combo = bar._thinking
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 160, 30)
    option.state = QStyle.StateFlag.State_Active
    baseline = QStyledItemDelegate(combo)

    def render(delegate, row):
        image = QImage(160, 30, QImage.Format.Format_ARGB32)
        image.fill(QColor(40, 40, 40))
        painter = QPainter(image)
        delegate.paint(painter, option, combo.model().index(row, 0))
        painter.end()
        return image

    # 不可用行覆盖浅灰蒙版；可用行保持原绘制结果。
    assert render(combo.itemDelegate(), 1) != render(baseline, 1)
    assert render(combo.itemDelegate(), 2) == render(baseline, 2)
    assert "按住 3 秒" in combo.itemData(1, Qt.ItemDataRole.ToolTipRole)
    bar.set_thinking_capability(True, available_levels=("minimal", "low", "high"))
    assert render(combo.itemDelegate(), 1) == render(baseline, 1)
    assert not combo.itemData(1, Qt.ItemDataRole.ToolTipRole)


def test_keyboard_skips_unavailable_thinking_levels(qtbot: QtBot) -> None:
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)
    bar.set_thinking_capability(True, available_levels=("low", "high"))
    bar.set_thinking_level("low")
    fired = []
    bar.thinking_level_changed.connect(fired.append)
    qtbot.keyClick(bar._thinking, Qt.Key.Key_Down)
    assert bar._thinking.currentText() == "high"
    assert fired == ["high"]


@pytest.mark.parametrize(
    ("declared", "runtime", "expected"),
    [
        ((), ("off",), ("off",)),
        ((), ("off", "minimal", "low", "medium", "high"),
         ("off", "minimal", "low", "medium", "high")),
        (("low", "high", "max"), ("off", "low", "high", "max"),
         ("off", "low", "high", "max")),
        (("low", "high", "max"), ("off", "minimal", "low", "medium", "high"),
         ("off", "low", "high")),
    ],
)
def test_thinking_choices_intersect_runtime_capabilities(qtbot, declared, runtime, expected):
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)
    bar.set_thinking_capability(None, available_levels=declared, runtime_levels=runtime)
    model = bar._thinking.model()
    assert tuple(
        level for i, level in enumerate(THINKING_LEVELS) if model.item(i).isEnabled()
    ) == expected
    # 切换到另一模型时，旧运行时范围不能残留。
    bar.set_thinking_capability(True, runtime_levels=("off", "low"))
    assert not model.item(THINKING_LEVELS.index("high")).isEnabled()
