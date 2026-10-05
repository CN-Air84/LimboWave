"""站点删除滑动确认：不可被点击、按键、半程拖动或已关闭面板绕过。"""

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QTabWidget
from pytestqt.qtbot import QtBot

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.ui.endpoint_delete_panel import EndpointDeletePanel
from limbowave.ui.settings_dialog import SettingsDialog
from limbowave.ui.slide_confirm import SlideConfirm


@pytest.fixture
def dialog(qtbot: QtBot, tmp_path: Path, vault_key: VaultKey) -> SettingsDialog:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    for site in ("relay-a", "relay-b"):
        settings.upsert_endpoint(EndpointConfig(
            id=site, name=site, base_url=f"https://{site}.example/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        ))
    settings.upsert_model(LogicalModel(
        id="only-a", name="Only A",
        bindings=[ModelBinding(endpoint_id="relay-a", model_id="only-a")],
    ))
    settings.upsert_model(LogicalModel(
        id="both", name="Both",
        bindings=[
            ModelBinding(endpoint_id=site, model_id="both") for site in ("relay-a", "relay-b")
        ],
    ))
    widget = SettingsDialog(settings, credentials)
    qtbot.addWidget(widget)
    widget.show()
    widget.findChild(QTabWidget).setCurrentWidget(widget._endpoints_tab)
    return widget


def _open(qtbot: QtBot, dialog: SettingsDialog, site: str = "relay-a") -> EndpointDeletePanel:
    tab = dialog._endpoints_tab
    tab._select_endpoint(site)
    tab._on_delete()
    panel = tab._delete_panel
    assert panel is not None
    qtbot.waitUntil(lambda: panel._motion is None)
    return panel


def _drag(qtbot: QtBot, slider: SlideConfirm, fraction: float) -> None:
    start = slider._handle_rect().center().toPoint()
    end = QPoint(start.x() + int(slider._span() * fraction) + 2, start.y())
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(slider, end)
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=end)


def test_bound_endpoint_offers_bulk_delete_without_writing(
    qtbot: QtBot, dialog: SettingsDialog,
) -> None:
    before = dialog._settings.load()
    panel = _open(qtbot, dialog)
    assert panel._title.text() == "解绑并删除站点"
    assert panel.findChild(QListWidget).count() == 2
    assert any("1 个模型将暂无可用站点" in label.text() for label in panel.findChildren(QLabel))
    assert dialog._settings.load() == before
    dialog._endpoints_tab._on_delete()  # 已打开的确认框不会重复创建
    assert dialog._endpoints_tab._delete_panel is panel


def test_clicking_track_and_enter_cannot_delete(qtbot: QtBot, dialog: SettingsDialog) -> None:
    before = dialog._settings.load()
    panel = _open(qtbot, dialog)
    slider = panel.slider
    for x in (slider.width() // 2, slider.width() - 1):
        qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(x, slider.height() // 2))
    qtbot.keyClick(slider, Qt.Key.Key_Return)
    qtbot.keyClick(slider, Qt.Key.Key_Space)
    assert dialog._settings.load() == before
    assert slider.progress == 0


def test_partial_drag_snaps_back_without_changes(qtbot: QtBot, dialog: SettingsDialog) -> None:
    before = dialog._settings.load()
    panel = _open(qtbot, dialog)
    _drag(qtbot, panel.slider, 0.5)
    qtbot.waitUntil(lambda: panel.slider.progress == 0)
    assert dialog._settings.load() == before
    assert not panel._closing


@pytest.mark.parametrize("method", ["cancel", "escape", "outside", "close"])
def test_cancel_does_not_unbind(
    qtbot: QtBot, dialog: SettingsDialog, method: str,
) -> None:
    before = dialog._settings.load()
    panel = _open(qtbot, dialog)
    if method == "cancel":
        next(b for b in panel.findChildren(QPushButton) if b.text() == "取消").click()
    elif method == "escape":
        qtbot.keyClick(panel, Qt.Key.Key_Escape)
    elif method == "outside":
        qtbot.mouseClick(dialog, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    else:
        panel.close_panel()
    panel._confirm()  # 退场期间也不许提交
    assert panel._closing
    assert dialog._endpoints_tab._delete_panel is None
    assert dialog._settings.load() == before


def test_full_drag_deletes_and_unbinds_once(qtbot: QtBot, dialog: SettingsDialog) -> None:
    tab = dialog._endpoints_tab
    changed: list[bool] = []
    tab.configuration_changed.connect(lambda: changed.append(True))
    panel = _open(qtbot, dialog)
    _drag(qtbot, panel.slider, 1.0)
    panel._confirm()
    config = dialog._settings.load()
    assert changed == [True]
    assert [e.id for e in config.endpoints] == ["relay-b"]
    assert [m.id for m in config.models] == ["only-a", "both"]
    assert config.models[0].bindings == []
    assert [b.endpoint_id for b in config.models[1].bindings] == ["relay-b"]
    assert all(a.endpoint_id == "relay-b" for a in config.actual_models)
    assert tab._list.count() == 1
    assert tab._editing is None
    assert tab._delete_panel is None


def test_unbound_endpoint_also_needs_slide(qtbot: QtBot, dialog: SettingsDialog) -> None:
    dialog._settings.unbind_endpoint("both", "relay-b")
    panel = _open(qtbot, dialog, "relay-b")
    assert panel._title.text() == "删除站点"
    assert panel.findChild(QListWidget) is None
    assert len(dialog._settings.load().endpoints) == 2
    _drag(qtbot, panel.slider, 1.0)
    assert [e.id for e in dialog._settings.load().endpoints] == ["relay-a"]


def test_delete_failure_reports_error_without_changes(
    qtbot: QtBot, dialog: SettingsDialog, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = dialog._settings.load()
    errors: list[str] = []
    changed: list[bool] = []
    dialog._endpoints_tab.configuration_changed.connect(lambda: changed.append(True))
    monkeypatch.setattr(
        "limbowave.ui.settings_dialog._show_error",
        lambda parent, exc: errors.append(str(exc)),
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(dialog._settings, "_save", fail)
    panel = _open(qtbot, dialog)
    _drag(qtbot, panel.slider, 1.0)
    assert errors == ["disk full"]
    assert changed == []
    assert dialog._settings.load() == before
    assert dialog._endpoints_tab._editing.id == "relay-a"


def test_many_models_scroll_without_pushing_confirmation_offscreen(
    qtbot: QtBot, dialog: SettingsDialog,
) -> None:
    for index in range(30):
        dialog._settings.upsert_model(LogicalModel(
            id=f"model-{index}", name=f"Model {index}",
            bindings=[ModelBinding(endpoint_id="relay-a", model_id=f"model-{index}")],
        ))
    panel = _open(qtbot, dialog)
    models = panel.findChild(QListWidget)
    assert models.count() == 32
    assert models.height() <= 140
    assert models.verticalScrollBar().maximum() > 0
    assert panel.geometry().bottom() < dialog.height()
    assert panel.slider.isVisible()


def test_new_gesture_cannot_accumulate_snapback_progress(qtbot: QtBot) -> None:
    slider = SlideConfirm()
    slider.resize(400, 40)
    qtbot.addWidget(slider)
    slider.show()
    with qtbot.assertNotEmitted(slider.confirmed):
        _drag(qtbot, slider, 0.7)
        center = slider._handle_rect().center().toPoint()
        qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=center)
        qtbot.mouseMove(slider, QPoint(slider.width() - 1, center.y()))
        qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton)
    assert slider.progress == 0


@pytest.mark.parametrize("button", [Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton])
def test_drag_must_start_on_left_handle(qtbot: QtBot, button: Qt.MouseButton) -> None:
    slider = SlideConfirm()
    slider.resize(400, 40)
    qtbot.addWidget(slider)
    slider.show()
    start = QPoint(slider.width() // 2, slider.height() // 2)
    if button == Qt.MouseButton.RightButton:
        start = slider._handle_rect().center().toPoint()
    with qtbot.assertNotEmitted(slider.confirmed):
        qtbot.mousePress(slider, button, pos=start)
        qtbot.mouseMove(slider, QPoint(slider.width() - 1, start.y()))
        qtbot.mouseRelease(slider, button)
    assert slider.progress == 0


def test_hidden_slider_discards_incomplete_gesture(qtbot: QtBot) -> None:
    slider = SlideConfirm()
    slider.resize(400, 40)
    qtbot.addWidget(slider)
    slider.show()
    with qtbot.assertNotEmitted(slider.confirmed):
        qtbot.mousePress(slider, Qt.MouseButton.LeftButton,
                         pos=slider._handle_rect().center().toPoint())
        qtbot.mouseMove(slider, QPoint(slider.width() // 2, slider.height() // 2))
        slider.hide()
        slider.show()
        qtbot.mouseMove(slider, QPoint(slider.width() - 1, slider.height() // 2))
        qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton)
    assert slider.progress == 0
