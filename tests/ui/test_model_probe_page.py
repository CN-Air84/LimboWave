from __future__ import annotations

from itertools import pairwise

import pytest
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyleOptionButton,
)
from pytestqt.qtbot import QtBot

from limbowave.application.services.model_probe import (
    DiscoveredModel,
    DiscoveryResult,
    ModelProbeResult,
    StreamProbeResult,
    ThinkingProbeResult,
    ToolProbeResult,
)
from limbowave.domain.models import ActualModel, ModelCapability
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.ui import theme
from limbowave.ui.model_probe_page import (
    CAPABILITIES,
    ActualModelRow,
    ActualModelsPage,
    ModelCapabilityChange,
    ModelProbeTask,
)


def _endpoint(id: str = "relay") -> EndpointConfig:
    return EndpointConfig(
        id=id, name=id, base_url=f"https://{id}.example/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )


def _page(qtbot: QtBot, *ids: str) -> ActualModelsPage:
    page = ActualModelsPage()
    qtbot.addWidget(page)
    page.activate_endpoint(_endpoint(), discover=False)
    page.apply_discovery("relay", DiscoveryResult(
        tuple(DiscoveredModel(id, id.upper()) for id in ids), "ok",
    ))
    return page


def _row(page: ActualModelsPage, id: str = "m") -> ActualModelRow:
    row = page._item(id)
    assert row is not None
    return row


def _displayed_ids(page: ActualModelsPage) -> list[str]:
    widgets = [
        page._model_layout.itemAt(index).widget()
        for index in range(page._model_layout.count())
    ]
    return [widget.model_id for widget in widgets if isinstance(widget, ActualModelRow)]


def _result(id: str = "m") -> ModelProbeResult:
    return ModelProbeResult(
        id, StreamProbeResult(True, "stream ok", "m-high"),
        ThinkingProbeResult(True, "high", "thinking ok", ("low", "high", "max")),
        ToolProbeResult(True, True, "tools ok"),
    )


def test_list_is_custom_widgets_not_an_item_view(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    assert isinstance(page._models, QScrollArea)
    assert not page.findChildren(QAbstractItemView)
    row = _row(page)
    assert len(row.findChildren(QCheckBox)) == 4
    assert row.display_name == "M"
    assert row.id_label.text() == "m"
    assert all(c.checkState() == Qt.CheckState.PartiallyChecked for c in row.checkboxes.values())


def test_checking_model_waits_for_run_button(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    row = _row(page)
    emitted: list[ModelProbeTask] = []
    page.probe_requested.connect(emitted.append)
    assert not page._run.isEnabled()
    row.selected.setChecked(True)
    assert not emitted
    assert all(d.accessibleDescription() == "未测" for d in row.checkboxes.values())
    assert page._run.text() == "批量检测（1）"
    page._run.click()
    assert emitted == [ModelProbeTask(_endpoint(), "m", "M")]
    assert "检测中" in row.status_text
    assert not page._run.isEnabled()
    assert not row.detect.isEnabled()
    assert all(not c.isEnabled() for c in row.checkboxes.values())


def test_probe_result_fills_checkboxes_without_saving_again(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    saved: list[object] = []
    page.capability_save_requested.connect(saved.append)
    page.apply_probe_result("relay", _result())
    row = _row(page)
    assert all(c.checkState() == Qt.CheckState.Checked for c in row.checkboxes.values())
    assert [d.accessibleDescription() for d in row.checkboxes.values()] == [
        "通过", "low、high、max", "支持",
    ]
    assert [c.toolTip().splitlines()[1] for c in row.checkboxes.values()] == [
        "stream ok", "thinking ok", "tools ok",
    ]
    assert all(d.property("capabilityTone") == "success" for d in row.checkboxes.values())
    assert not saved
    assert row.detect.isEnabled()


@pytest.mark.parametrize("inconclusive", [True, False])
def test_inconclusive_and_unsupported_remain_distinct(qtbot: QtBot, inconclusive: bool) -> None:
    page = _page(qtbot, "m")
    page.apply_probe_result("relay", ModelProbeResult(
        "m", StreamProbeResult(True, "ok"),
        ThinkingProbeResult(False, None, "no evidence", inconclusive=inconclusive),
        ToolProbeResult(False, True, "no call", inconclusive),
    ))
    row = _row(page)
    for key in ("supports_thinking", "supports_tools"):
        assert row.checkboxes[key].checkState() == (
            Qt.CheckState.PartiallyChecked if inconclusive else Qt.CheckState.Unchecked
        )
        assert row.checkboxes[key].accessibleDescription() == (
            "未确认" if inconclusive else "不支持"
        )
        assert row.checkboxes[key].property("capabilityTone") == (
            "warning" if inconclusive else "danger"
        )


def test_budget_mode_is_not_labelled_as_high(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    page.apply_probe_result("relay", ModelProbeResult(
        "m", StreamProbeResult(True, "ok"),
        ThinkingProbeResult(True, None, "budget verified"),
        ToolProbeResult(False, True, "no call", True),
    ))
    assert _row(page).checkboxes["supports_thinking"].accessibleDescription() == "支持（预算模式）"


@pytest.mark.parametrize("capability", [key for key, _ in CAPABILITIES])
def test_manual_capability_is_independent_of_selection_and_probe(
    qtbot: QtBot, capability: ModelCapability,
) -> None:
    page = _page(qtbot, "m")
    row = _row(page)
    probes: list[object] = []
    saves: list[ModelCapabilityChange] = []
    page.probe_requested.connect(probes.append)
    page.capability_save_requested.connect(saves.append)
    row.checkboxes[capability].click()
    assert len(saves) == 1
    change = saves[0]
    assert change == ModelCapabilityChange(_endpoint(), "m", "M", capability, True)
    assert not probes
    assert not row.selected.isChecked()
    assert not page._run.isEnabled()
    assert "正在保存" in row.status_text
    actual = ActualModel.model_validate({"endpoint_id": "relay", "model_id": "m", capability: True})
    page.apply_capability_save(change, actual)
    assert row.checkboxes[capability].isChecked()
    assert "手动设置" in row.status_text
    assert all(c.isEnabled() for c in row.checkboxes.values())
    assert len(saves) == 1
    row.checkboxes[capability].click()
    assert saves[-1].supported is False


def test_manual_and_automatic_capabilities_can_replace_each_other(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    row = _row(page)
    change = ModelCapabilityChange(_endpoint(), "m", "M", "supports_tools", False)
    page.apply_capability_save(
        change, ActualModel(endpoint_id="relay", model_id="m", supports_tools=False),
    )
    saves: list[ModelCapabilityChange] = []
    page.capability_save_requested.connect(saves.append)
    row.detect.click()
    page.apply_probe_result("relay", _result())
    assert row.checkboxes["supports_tools"].isChecked()
    assert not saves
    row.checkboxes["supports_tools"].click()
    assert saves == [change]
    page.apply_capability_save(
        change, ActualModel(endpoint_id="relay", model_id="m", supports_tools=False),
    )
    assert row.checkboxes["supports_tools"].checkState() == Qt.CheckState.Unchecked
    assert not row.probe_succeeded


def test_custom_row_preserves_animated_checkbox_indicator(qtbot: QtBot) -> None:
    from PySide6.QtCore import QObject

    from limbowave.ui.checkbox_style import CheckBoxStyle, _CheckTransition

    page = _page(qtbot, "m")
    page.setStyleSheet(theme.app_stylesheet())
    checkbox = _row(page).checkboxes["supports_tools"]
    style = CheckBoxStyle()
    checkbox.setStyle(style)
    page.show()
    checkbox.grab()
    transition = checkbox.findChild(QObject, "limbowaveCheckTransition")
    assert isinstance(transition, _CheckTransition)
    assert transition.glyph == Qt.CheckState.PartiallyChecked
    checkbox.setStyle(None)


def test_save_error_rolls_back_to_unknown_and_allows_retry(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    row = _row(page)
    with qtbot.waitSignal(page.capability_save_requested) as signal:
        row.checkboxes["supports_streaming"].click()
    page.show_capability_save_error(signal.args[0], "磁盘不可写")
    assert row.checkboxes["supports_streaming"].checkState() == Qt.CheckState.PartiallyChecked
    assert row.checkboxes["supports_streaming"].isEnabled()
    assert "保存失败" in row.status_text
    assert not row.saved
    assert not page._saving


def test_keyboard_changes_unknown_to_checked_then_unchecked(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    row = _row(page)
    checkbox = row.checkboxes["supports_tools"]
    with qtbot.waitSignal(page.capability_save_requested) as signal:
        qtbot.keyClick(checkbox, Qt.Key.Key_Space)
    change = signal.args[0]
    assert change.supported is True
    page.apply_capability_save(
        change, ActualModel(endpoint_id="relay", model_id="m", supports_tools=True),
    )
    with qtbot.waitSignal(page.capability_save_requested) as signal:
        qtbot.keyClick(checkbox, Qt.Key.Key_Space)
    assert signal.args[0].supported is False


def test_manual_and_detected_values_survive_refresh(qtbot: QtBot) -> None:
    page = _page(qtbot, "m", "detected")
    row = _row(page)
    row.selected.setChecked(True)
    change = ModelCapabilityChange(_endpoint(), "m", "M", "supports_streaming", False)
    actual = ActualModel(endpoint_id="relay", model_id="m", supports_streaming=False)
    page.apply_capability_save(change, actual)
    page.apply_probe_result("relay", _result("detected"))
    page.apply_discovery("relay", DiscoveryResult((DiscoveredModel("new", "New"),), "ok"))
    assert _row(page) is row
    assert row.selected.isChecked()
    assert row.checkboxes["supports_streaming"].checkState() == Qt.CheckState.Unchecked
    assert _row(page, "detected").checkboxes["supports_tools"].isChecked()
    page.apply_discovery("relay", DiscoveryResult((), "failed"))
    assert set(page._rows) == {"new", "m", "detected"}


def test_saved_catalog_restores_without_network_and_is_endpoint_scoped(qtbot: QtBot) -> None:
    page = _page(qtbot)
    saved: list[object] = []
    page.capability_save_requested.connect(saved.append)
    actual = ActualModel(
        endpoint_id="relay", model_id="m", supports_streaming=False,
        supports_thinking=True, available_thinking_levels=("low", "high"), supports_tools=True,
    )
    other = ActualModel(endpoint_id="other", model_id="other-model")
    page.activate_endpoint(_endpoint(), discover=False, actual_models=(actual, other))
    row = _row(page)
    assert set(page._rows) == {"m"}
    assert not row.selected.isChecked()
    assert row.checkboxes["supports_streaming"].checkState() == Qt.CheckState.Unchecked
    assert row.checkboxes["supports_thinking"].isChecked()
    assert row.checkboxes["supports_thinking"].accessibleDescription() == "low、high"
    assert row.checkboxes["supports_tools"].isChecked()
    assert not saved
    page.activate_endpoint(_endpoint("other"), discover=False, actual_models=(actual, other))
    page.apply_discovery("relay", DiscoveryResult((DiscoveredModel("stale", "Stale"),), "ok"))
    page.apply_probe_result("relay", _result())
    page.apply_capability_save(
        ModelCapabilityChange(_endpoint(), "m", "M", "supports_tools", True), actual,
    )
    assert set(page._rows) == {"other-model"}


def test_saved_models_do_not_select_detection_on_reentry_or_refresh(
    qtbot: QtBot,
) -> None:
    page = _page(qtbot)
    actual = ActualModel(endpoint_id="relay", model_id="m", supports_tools=True)
    emitted: list[object] = []
    page.probe_requested.connect(emitted.append)
    page.manual_save_requested.connect(emitted.append)
    page.capability_save_requested.connect(emitted.append)
    page.activate_endpoint(_endpoint(), discover=False, actual_models=(actual,))
    row = _row(page)
    assert not row.selected.isChecked()

    discovery = DiscoveryResult((DiscoveredModel("m", "M"), DiscoveredModel("new", "New")), "ok")
    page.apply_discovery("relay", discovery)
    assert _row(page) is row
    assert not row.selected.isChecked()
    assert not _row(page, "new").selected.isChecked()

    row.selected.setChecked(False)
    updated = actual.model_copy(update={"supports_streaming": True})
    page.apply_saved_models((updated,))
    page.apply_discovery("relay", discovery)
    assert not row.selected.isChecked()
    assert row.checkboxes["supports_streaming"].checkState() == Qt.CheckState.Checked
    assert not page._run.isEnabled()

    page.activate_endpoint(_endpoint("other"), discover=False, actual_models=(updated,))
    assert not page._rows
    page.activate_endpoint(_endpoint(), discover=False, actual_models=(updated,))
    assert not _row(page).selected.isChecked()
    assert not emitted


def test_checked_models_move_to_top_and_keep_catalog_order(qtbot: QtBot) -> None:
    page = _page(qtbot, "a", "b", "c", "d")
    emitted: list[object] = []
    page.probe_requested.connect(emitted.append)
    page.capability_save_requested.connect(emitted.append)
    assert _displayed_ids(page) == ["a", "b", "c", "d"]
    _row(page, "d").selected.click()
    assert _displayed_ids(page) == ["d", "a", "b", "c"]
    _row(page, "b").selected.click()
    assert _displayed_ids(page) == ["b", "d", "a", "c"]
    _row(page, "b").selected.click()
    assert _displayed_ids(page) == ["d", "a", "b", "c"]
    page._select_all.click()
    assert _displayed_ids(page) == ["d", "a", "b", "c"]
    assert [row.model_id for row in page._items() if row.selected.isChecked()] == ["d"]
    assert not emitted


def _animated_page(qtbot: QtBot) -> ActualModelsPage:
    page = _page(qtbot, "a", "b", "c", "d")
    page.setStyleSheet(theme.app_stylesheet())
    page.resize(900, 640)
    page.show()
    qtbot.waitExposed(page)
    assert all(
        row._move_animation.state() == QAbstractAnimation.State.Stopped for row in page._items()
    )
    return page


def _wait_for_row_moves(qtbot: QtBot, page: ActualModelsPage) -> None:
    qtbot.waitUntil(lambda: all(
        row._move_animation.state() == QAbstractAnimation.State.Stopped for row in page._items()
    ), timeout=1500)
    rows = [_row(page, id) for id in _displayed_ids(page)]
    assert all(row.pos() == row._layout_position for row in rows)
    assert all(
        first.geometry().bottom() < second.y()
        for first, second in pairwise(rows)
    )


def test_checked_row_and_displaced_rows_animate_to_their_new_positions(qtbot: QtBot) -> None:
    page = _animated_page(qtbot)
    first, last = _row(page, "a"), _row(page, "d")
    start = {row.model_id: row.pos() for row in page._items()}
    last.selected.click()
    page._model_layout.activate()
    assert last.pos() == start["d"]
    assert first.pos() == start["a"]
    qtbot.waitUntil(lambda: start["a"].y() < last.y() < start["d"].y())
    assert start["a"].y() < first.y() < start["b"].y()
    qtbot.waitUntil(lambda: last.pos() == start["a"] and first.pos() == start["b"])
    assert _row(page, "b").pos() == start["c"]
    assert _row(page, "c").pos() == start["d"]


@pytest.mark.parametrize("action", ["uncheck", "check_another", "select_all", "refresh"])
def test_row_moves_retarget_from_the_current_frame(qtbot: QtBot, action: str) -> None:
    page = _animated_page(qtbot)
    _row(page, "d").selected.click()
    page._model_layout.activate()
    for row in page._items():
        row._move_animation.setCurrentTime(95)
    positions = {row.model_id: row.pos() for row in page._items()}
    if action == "uncheck":
        _row(page, "d").selected.click()
    elif action == "check_another":
        _row(page, "c").selected.click()
    elif action == "select_all":
        page._select_all.click()
    else:
        page.apply_discovery("relay", DiscoveryResult(
            tuple(DiscoveredModel(id, id) for id in ("c", "d", "a", "b")), "ok",
        ))
    page._model_layout.activate()
    assert {row.model_id: row.pos() for row in page._items()} == positions
    _wait_for_row_moves(qtbot, page)


def test_repeated_layout_does_not_restart_row_moves(qtbot: QtBot) -> None:
    page = _animated_page(qtbot)
    _row(page, "d").selected.click()
    page._model_layout.activate()
    for row in page._items():
        row._move_animation.setCurrentTime(95)
    positions = {row.model_id: row.pos() for row in page._items()}
    page._update_actions()
    page._model_layout.invalidate()
    page._model_layout.activate()
    assert {row.model_id: row.pos() for row in page._items()} == positions
    assert all(row._move_animation.currentTime() == 95 for row in page._items())
    _wait_for_row_moves(qtbot, page)


@pytest.mark.parametrize("action", ["hide", "resize", "switch_endpoint"])
def test_row_moves_settle_when_page_context_changes(qtbot: QtBot, action: str) -> None:
    page = _animated_page(qtbot)
    _row(page, "d").selected.click()
    page._model_layout.activate()
    for row in page._items():
        row._move_animation.setCurrentTime(95)
    if action == "hide":
        page.hide()
        _wait_for_row_moves(qtbot, page)
        page.show()
        qtbot.waitExposed(page)
    elif action == "resize":
        page.resize(1000, 700)
        page._model_layout.activate()
    else:
        page.activate_endpoint(_endpoint("other"), discover=False, actual_models=(
            ActualModel(endpoint_id="other", model_id="new"),
        ))
        qtbot.waitUntil(lambda: _row(page, "new")._layout_position is not None)
        assert _displayed_ids(page) == ["new"]
    _wait_for_row_moves(qtbot, page)
    assert all(row.width() <= page._models.viewport().width() for row in page._items())


def test_saved_models_follow_catalog_order_without_detection_selection(qtbot: QtBot) -> None:
    page = _page(qtbot)
    actuals = tuple(ActualModel(endpoint_id="relay", model_id=id) for id in ("b", "local"))
    page.activate_endpoint(_endpoint(), discover=False, actual_models=actuals)
    discovery = DiscoveryResult(tuple(DiscoveredModel(id, id) for id in ("a", "b", "c")), "ok")
    page.apply_discovery("relay", discovery)
    assert _displayed_ids(page) == ["a", "b", "c", "local"]
    page.apply_saved_models((*actuals, ActualModel(endpoint_id="relay", model_id="late")))
    assert _displayed_ids(page) == ["a", "b", "c", "local", "late"]
    page.apply_discovery("relay", discovery)
    assert _displayed_ids(page) == ["a", "b", "c", "local", "late"]


def test_detected_model_can_be_retested_and_failed_retest_keeps_declarations(qtbot: QtBot) -> None:
    page = _page(qtbot, "m", "other")
    page.apply_probe_result("relay", _result())
    row = _row(page)
    with qtbot.waitSignal(page.probe_requested):
        row.detect.click()
    assert all(not c.isEnabled() for c in row.checkboxes.values())
    assert all(c.isEnabled() for c in _row(page, "other").checkboxes.values())
    page.show_probe_error("relay", "m", "连接超时")
    assert "检测失败" in row.status_text
    assert all(c.isChecked() for c in row.checkboxes.values())
    assert all(c.isEnabled() for c in row.checkboxes.values())
    assert row.detect.isEnabled()


def test_empty_discovery_allows_manual_add_without_probing(qtbot: QtBot) -> None:
    page = _page(qtbot)
    page.apply_discovery("relay", DiscoveryResult((), "模型清单请求失败（HTTP 404）"))
    assert "手动输入" in page._status.text()
    probes: list[object] = []
    page.probe_requested.connect(probes.append)
    page._manual_id.setText("  custom-model-v1  ")
    with qtbot.waitSignal(page.manual_save_requested) as signal:
        page._manual_add.click()
    assert signal.args[0] == ModelProbeTask(_endpoint(), "custom-model-v1", "custom-model-v1")
    assert not _row(page, "custom-model-v1").selected.isChecked()
    _row(page, "custom-model-v1").selected.setChecked(True)
    assert not probes
    page.apply_manual_save("relay", "custom-model-v1")
    assert page._manual_id.text() == ""
    with qtbot.waitSignal(page.probe_requested):
        page._run.click()


def test_manual_model_survives_late_discovery_and_can_retry(qtbot: QtBot) -> None:
    page = _page(qtbot)
    page._manual_id.setText("manual-model")
    with qtbot.waitSignal(page.manual_save_requested):
        page._manual_id.returnPressed.emit()
    page.apply_manual_save("relay", "manual-model")
    row = _row(page, "manual-model")
    row.selected.setChecked(True)
    row.detect.click()
    page.show_probe_error("relay", "manual-model", "连接超时")
    page.apply_discovery("relay", DiscoveryResult((DiscoveredModel("listed", "Listed"),), "ok"))
    assert len(page._rows) == 2
    assert _row(page, "manual-model") is row
    assert _displayed_ids(page) == ["manual-model", "listed"]
    assert "失败" in row.status_text
    with qtbot.waitSignal(page.probe_requested):
        page._run.click()
    assert "检测中" in row.status_text
    assert len(page._rows) == 2
    page.apply_discovery("relay", DiscoveryResult((), "清单不可用"))
    assert len(page._rows) == 2


def test_manual_duplicate_is_not_added_and_endpoint_change_resets_rows(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    page._manual_id.setText("m")
    with qtbot.waitSignal(page.manual_save_requested):
        page._manual_add.click()
    assert len(page._rows) == 1
    assert not _row(page).selected.isChecked()
    page.activate_endpoint(_endpoint("other"), discover=False)
    assert not page._rows
    assert page._manual_id.text() == ""
    assert not page._manual_add.isEnabled()
    assert not page._probing


def test_failed_probe_can_be_added_without_faking_capabilities(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    _row(page).detect.click()
    page.show_probe_error("relay", "m", "流式请求失败")
    page._manual_id.setText("m")
    with qtbot.waitSignal(page.manual_save_requested) as signal:
        page._manual_add.click()
    assert signal.args[0].model_id == "m"
    page.apply_manual_save("relay", "m")
    row = _row(page)
    assert "未验证" in row.status_text
    assert all(c.checkState() == Qt.CheckState.PartiallyChecked for c in row.checkboxes.values())
    assert all(d.accessibleDescription() == "未测" for d in row.checkboxes.values())
    assert page._manual_id.text() == ""


def test_manual_add_waits_for_inflight_probe(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    _row(page).detect.click()
    page._manual_id.setText("m")
    emitted: list[object] = []
    page.manual_save_requested.connect(emitted.append)
    page._manual_add.click()
    assert not emitted
    assert "正在检测" in page._status.text()


def test_add_all_and_batch_run_only_affect_pending_rows(qtbot: QtBot) -> None:
    page = _page(qtbot, "a", "b", "c")
    emitted: list[ModelProbeTask] = []
    page.probe_requested.connect(emitted.append)
    page.manual_save_requested.connect(
        lambda task: page.apply_manual_save(task.endpoint.id, task.model_id)
    )
    page._select_all.click()
    assert not emitted
    assert not any(row.selected.isChecked() for row in page._items())
    for row in page._items():
        row.selected.setChecked(True)
    assert not page._select_all.isEnabled()
    assert page._run.text() == "批量检测（3）"
    page._run.click()
    assert [task.model_id for task in emitted] == ["a", "b", "c"]
    assert not page._run.isEnabled()
    page._run.click()
    assert len(emitted) == 3
    page.apply_probe_result("relay", _result("a"))
    page.show_probe_error("relay", "b", "连接超时")
    assert page._run.text() == "批量检测（1）"
    emitted.clear()
    page._run.click()
    assert [task.model_id for task in emitted] == ["b"]
    _row(page, "a").selected.setChecked(False)
    assert not page._select_all.isEnabled()


def test_add_all_disabled_for_empty_or_already_added_and_checked_list(qtbot: QtBot) -> None:
    page = _page(qtbot)
    assert not page._select_all.isEnabled()
    page._manual_id.setText("manual-model")
    page._manual_add.click()
    assert len(page._rows) == 1
    page.apply_manual_save("relay", "manual-model")
    assert not page._select_all.isEnabled()


def test_theme_switch_recolors_existing_status_labels(qtbot: QtBot) -> None:
    from PySide6.QtGui import QColor, QPalette

    page = _page(qtbot, "m", "idle")
    page.apply_probe_result("relay", _result())
    try:
        for name in ("dark", "light"):
            theme.set_palette(name)
            page.setStyleSheet(theme.app_stylesheet())
            page.show()
            for key, _ in CAPABILITIES:
                label = _row(page).checkboxes[key]
                assert label.palette().color(QPalette.ColorRole.WindowText) == QColor(
                    theme.status_text_color("success")
                )
            idle = _row(page, "idle").checkboxes["supports_streaming"]
            assert idle.palette().color(QPalette.ColorRole.WindowText) == QColor(
                theme.TEXT_SECONDARY
            )
    finally:
        theme.set_palette("dark")


def test_long_names_fit_the_scroll_view_without_horizontal_scroll(qtbot: QtBot) -> None:
    page = _page(qtbot, "a-very-long-model-id-" * 15)
    page.setStyleSheet(theme.app_stylesheet())
    page.resize(600, 640)
    page.show()
    qtbot.waitExposed(page)
    row = page._items()[0]
    assert row.width() <= page._models.viewport().width()
    assert page._models.horizontalScrollBar().maximum() == 0
    assert all(c.width() >= c.sizeHint().width() for c in row.checkboxes.values())


@pytest.mark.parametrize("width", [600, 1000])
def test_model_id_and_capabilities_share_one_compact_row(qtbot: QtBot, width: int) -> None:
    page = _page(qtbot, "m", "model-with-a-much-longer-id")
    page.setStyleSheet(theme.app_stylesheet())
    page.resize(width, 640)
    page.show()
    qtbot.waitExposed(page)
    first, second = page._items()
    for row in (first, second):
        controls = [row.selected, row.id_label, *row.checkboxes.values(), row.add, row.detect]
        centers = [control.geometry().center().y() for control in controls]
        assert max(centers) - min(centers) <= 1
        assert row.height() <= row.detect.sizeHint().height() + 14
        assert row.id_label.isVisible()
        assert row.model_id in row.id_label.toolTip()
        assert row.display_name in row.id_label.toolTip()
        assert row.id_label.width() > 0
    for key, _ in CAPABILITIES:
        assert first.checkboxes[key].x() == second.checkboxes[key].x()
    page.apply_probe_result("relay", _result())
    assert "low、high、max" in first.checkboxes["supports_thinking"].toolTip()
    assert "检测完成" in first.detect.toolTip()


@pytest.mark.parametrize("width", [600, 1000])
def test_model_table_header_and_action_positions(qtbot: QtBot, width: int) -> None:
    page = _page(qtbot, "a", "b", *[f"model-{i}" for i in range(20)])
    page.setStyleSheet(theme.app_stylesheet())
    page.resize(width, 640)
    page.show()
    qtbot.waitExposed(page)
    assert page._select_all.text() == "全部添加"
    assert page._manual_add.text() == "添加到模型列表"
    assert page._run.text() == "批量检测"
    assert not any("仅保存" in b.text() for b in page.findChildren(QPushButton))
    assert abs(
        page._run.geometry().center().y() - page._manual_add.geometry().center().y()
    ) <= 1
    assert page._run.x() > page._manual_add.x()
    assert page._select_all.y() < page._run.y()
    header = page._model_header
    labels = header.findChildren(QLabel)
    assert [label.text() for label in labels] == ["模型id", "能力", "添加"]
    row = _row(page, "a")
    assert header.isVisible()
    assert header.geometry().bottom() < row.y()
    assert abs(labels[0].x() - row.id_label.x()) <= 1
    first = row.checkboxes["supports_streaming"]
    last = row.checkboxes["supports_tools"]
    assert abs(labels[1].x() - first.x()) <= 1
    assert abs(labels[1].geometry().right() - last.geometry().right()) <= 1
    assert abs(labels[2].x() - row.add.x()) <= 1
    row.selected.click()
    _wait_for_row_moves(qtbot, page)
    assert page._model_layout.itemAt(0).widget() is header
    assert page._models.horizontalScrollBar().maximum() == 0


def test_header_remains_visible_for_empty_catalog(qtbot: QtBot) -> None:
    page = _page(qtbot)
    page.show()
    assert page._model_header.isVisible()
    assert page._empty.isVisible()
    assert not page._run.isEnabled()


def test_add_all_saves_unsaved_models_without_probing_or_overwriting(qtbot: QtBot) -> None:
    page = _page(qtbot, "saved", "checked", "new", "busy")
    actual = ActualModel(endpoint_id="relay", model_id="saved", supports_tools=True)
    page.apply_saved_models((actual,))
    _row(page, "checked").selected.setChecked(True)
    page.mark_probe_started("busy")
    probes: list[object] = []
    saves: list[ModelProbeTask] = []
    page.probe_requested.connect(probes.append)

    def save(task: ModelProbeTask) -> None:
        saves.append(task)
        page.apply_manual_save(task.endpoint.id, task.model_id)

    page.manual_save_requested.connect(save)
    page._select_all.click()
    assert [task.model_id for task in saves] == ["checked", "new"]
    assert not probes
    assert [row.model_id for row in page._items() if row.selected.isChecked()] == ["checked"]
    assert _row(page, "saved").actual == actual
    assert _row(page, "saved").checkboxes["supports_tools"].isChecked()
    assert not page._select_all.isEnabled()
    page._select_all.click()
    assert len(saves) == 2
    page.show_probe_error("relay", "busy", "timeout")
    assert page._select_all.isEnabled()
    page._select_all.click()
    assert saves[-1].model_id == "busy"


def test_adding_saved_model_does_not_select_it_or_overwrite_capabilities(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    actual = ActualModel(endpoint_id="relay", model_id="m", supports_tools=True)
    page.apply_saved_models((actual,))
    row = _row(page)
    row.selected.setChecked(False)
    emitted: list[object] = []
    page.manual_save_requested.connect(emitted.append)
    page.probe_requested.connect(emitted.append)
    page._manual_id.setText("m")
    page._manual_add.click()
    assert not row.selected.isChecked()
    assert not emitted
    assert row.actual == actual
    assert row.checkboxes["supports_tools"].isChecked()


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_add_button_has_no_horizontal_inset(qtbot: QtBot, palette: str) -> None:
    previous_palette = theme.current_palette().name
    try:
        theme.set_palette(palette)
        page = _page(qtbot, "m")
        page.setStyleSheet(theme.app_stylesheet())
        page.resize(600, 640)
        page.show()
        qtbot.waitExposed(page)
        row = _row(page)
        for saved in (False, True):
            if saved:
                page.apply_manual_save("relay", "m")
            option = QStyleOptionButton()
            row.add.initStyleOption(option)
            content = row.add.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, row.add,
            )
            assert content.left() == 1  # Only the button border remains.
            assert content.right() == row.add.width() - 2
            assert content.width() >= row.add.fontMetrics().horizontalAdvance(row.add.text())
            assert row.add.height() == row.detect.height()
    finally:
        theme.set_palette(previous_palette)


def test_add_column_is_independent_of_detection_selection(qtbot: QtBot) -> None:
    page = _page(qtbot, "m", "other")
    row = _row(page)
    saves: list[ModelProbeTask] = []
    probes: list[object] = []
    page.manual_save_requested.connect(saves.append)
    page.probe_requested.connect(probes.append)
    assert row.add.text() == "添加"
    row.selected.click()
    assert not saves
    assert not row.saved
    row.selected.click()
    row.add.click()
    assert len(saves) == 1
    assert not row.selected.isChecked()
    assert not probes
    page.apply_manual_save("relay", "m")
    assert row.add.text() == "已添加"
    assert not row.add.isEnabled()
    assert not row.selected.isChecked()
    row.selected.click()
    assert row.add.text() == "已添加"
    with qtbot.waitSignal(page.probe_requested):
        page._run.click()


def test_add_column_tracks_probe_and_capability_save_states(qtbot: QtBot) -> None:
    page = _page(qtbot, "m")
    row = _row(page)
    row.detect.click()
    assert not row.add.isEnabled()
    page.show_probe_error("relay", "m", "timeout")
    assert row.add.isEnabled()
    assert row.add.text() == "添加"
    row.checkboxes["supports_tools"].click()
    assert not row.add.isEnabled()
    change = ModelCapabilityChange(_endpoint(), "m", row.display_name, "supports_tools", True)
    page.show_capability_save_error(change, "failed")
    assert row.add.isEnabled()
    page.apply_probe_result("relay", _result())
    assert row.add.text() == "已添加"
    assert not row.add.isEnabled()
    assert not row.selected.isChecked()
