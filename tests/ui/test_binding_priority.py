"""Logical-model priority page and completed drag/move persistence."""

from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QAbstractItemView, QLabel, QListWidget, QPushButton

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.ui.logical_models_tab import LogicalModelsTab
from limbowave.ui.settings_dialog import _EndpointsTab


@pytest.fixture
def page(qtbot, tmp_path: Path):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    for site in ("a", "b", "c"):
        settings.upsert_endpoint(EndpointConfig(
            id=site, name=f"站点 {site}", base_url=f"https://{site}.example.com/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        ))
    settings.upsert_model(LogicalModel(
        id="logical", name="Logical",
        bindings=[ModelBinding(endpoint_id=site, model_id=f"remote-{site}")
                  for site in ("a", "b", "c")],
    ))
    tab = LogicalModelsTab(settings)
    qtbot.addWidget(tab)
    tab.resize(1050, 680)
    tab.show()
    tab._select_model("logical")
    return tab, settings


def _order(widget):
    return [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())]


def test_priority_subpage_and_expandable_binding_list(page, qtbot):
    tab, _settings = page
    assert [tab._subtabs.tabText(i) for i in range(tab._subtabs.count())] == [
        "逻辑模型设置", "模型绑定", "优先级排序",
    ]
    tab._subtabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: tab._binding_page.isVisible())
    assert tab._bindings.minimumHeight() >= 240
    assert tab._bindings.height() > 160
    assert tab._bindings.maximumHeight() > 1000
    tab._subtabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: tab._priority_page.isVisible())
    assert tab._priority_list.dragDropMode() == QAbstractItemView.DragDropMode.InternalMove
    assert tab._priority_list.defaultDropAction() == Qt.DropAction.MoveAction
    assert _order(tab._priority_list) == ["a", "b", "c"]
    assert "默认" in tab._priority_list.item(0).text()
    assert not hasattr(tab, "_default_binding_btn")
    assert "设为默认站点" not in [b.text() for b in tab.findChildren(QPushButton)]


def test_move_saves_default_from_priority_without_changing_unsaved_fields(page, qtbot):
    tab, settings = page
    tab._subtabs.setCurrentIndex(2)
    tab._name.setText("尚未保存的名称")
    tab._priority_list.setCurrentRow(2)
    qtbot.mouseClick(tab._priority_up_btn, Qt.MouseButton.LeftButton)
    assert _order(tab._priority_list) == ["a", "c", "b"]
    assert _order(tab._bindings) == ["a", "c", "b"]
    model = settings.load().models[0]
    assert [b.endpoint_id for b in model.bindings] == ["a", "c", "b"]
    qtbot.mouseClick(tab._priority_up_btn, Qt.MouseButton.LeftButton)
    assert _order(tab._priority_list) == ["c", "a", "b"]
    assert "默认" in tab._priority_list.item(0).text()
    assert "默认" not in tab._priority_list.item(1).text()
    assert tab._bindings.item(0).text().startswith("★ ")
    assert tab._priority_list.currentItem().data(Qt.ItemDataRole.UserRole) == "c"
    assert tab._name.text() == "尚未保存的名称"
    assert "站点 c" in tab._preview.text()
    assert "已保存" in tab._priority_status.text()
    other = LogicalModelsTab(settings)
    qtbot.addWidget(other)
    other._select_model("logical")
    assert _order(other._priority_list) == ["c", "a", "b"]
    assert "站点 c" in other._preview.text()


def test_completed_drop_uses_same_save_path(page, monkeypatch):
    tab, settings = page
    widget = tab._priority_list
    widget.setCurrentRow(2)

    def simulate_qt_internal_move(self, event):
        assert self.model().moveRows(QModelIndex(), 2, 1, QModelIndex(), 0)
        event.accept()

    monkeypatch.setattr(QListWidget, "dropEvent", simulate_qt_internal_move)
    event = QDropEvent(QPointF(4, 4), Qt.DropAction.MoveAction,
                       widget.mimeData([widget.item(2)]),
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    assert _order(widget) == ["c", "a", "b"]
    assert [b.endpoint_id for b in settings.load().models[0].bindings] == ["c", "a", "b"]


@pytest.mark.parametrize("error", [ValueError("绑定已变化"), OSError("磁盘不可写")])
def test_failed_save_restores_authoritative_order(page, monkeypatch, error):
    tab, settings = page
    alerts = []
    monkeypatch.setattr("limbowave.ui.floating.ask_alert",
                        lambda _parent, _title, detail: alerts.append(str(detail)))

    def fail(*_args):
        raise error

    monkeypatch.setattr(settings, "reorder_bindings", fail)
    tab._priority_list.setCurrentRow(2)
    tab._priority_up_btn.click()
    assert _order(tab._priority_list) == ["a", "b", "c"]
    assert alerts == [str(error)]
    assert "未保存" in tab._priority_status.text()


def test_empty_single_and_boundary_controls(page):
    tab, settings = page
    tab._priority_list.setCurrentRow(0)
    assert not tab._priority_up_btn.isEnabled()
    assert tab._priority_down_btn.isEnabled()
    tab._priority_list.setCurrentRow(2)
    assert tab._priority_up_btn.isEnabled()
    assert not tab._priority_down_btn.isEnabled()
    for site in ("a", "c"):
        settings.unbind_endpoint("logical", site)
    tab.reload()
    assert not tab._priority_up_btn.isEnabled()
    assert not tab._priority_down_btn.isEnabled()
    settings.unbind_endpoint("logical", "b")
    tab.reload()
    assert tab._priority_list.count() == 0
    assert "未绑定" in tab._priority_status.text()
    tab._on_new()
    assert not tab._priority_list.isEnabled()
    assert "选择" in tab._priority_status.text()


def test_cancelled_drop_does_not_save(page, monkeypatch):
    tab, settings = page
    saves = []
    monkeypatch.setattr(settings, "reorder_bindings", lambda *args: saves.append(args))
    monkeypatch.setattr(QListWidget, "dropEvent", lambda _self, event: event.ignore())
    widget = tab._priority_list
    event = QDropEvent(QPointF(4, 4), Qt.DropAction.MoveAction,
                       widget.mimeData([widget.item(2)]),
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    assert _order(widget) == ["a", "b", "c"]
    assert saves == []


def test_endpoint_priority_editor_removed_but_old_config_is_compatible(
    page, qtbot, tmp_path, vault_key,
):
    _tab, settings = page
    old = settings.load().endpoints[0].model_copy(update={"priority": 87})
    settings.upsert_endpoint(old)
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    endpoints = _EndpointsTab(settings, credentials)
    qtbot.addWidget(endpoints)
    for row in range(endpoints._list.count()):
        if endpoints._list.item(row).data(Qt.ItemDataRole.UserRole) == old.id:
            endpoints._list.setCurrentRow(row)
            break
    assert not hasattr(endpoints, "_priority")
    assert "优先级" not in [label.text() for label in endpoints.findChildren(QLabel)]
    endpoints._rpm.setValue(12)
    updated = endpoints._form_endpoint()
    assert updated.priority == 87
    assert updated.rpm == 12
