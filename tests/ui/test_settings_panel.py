from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import Vault, VaultKey
from limbowave.ui.settings_dialog import _SecurityTab
from limbowave.ui.settings_panel import SettingsPage


@pytest.mark.parametrize("with_vault", [False, True])
@pytest.mark.parametrize(
    ("button_text", "signal_name"),
    [
        ("生成加密备份…", "backup_requested"),
        ("从备份恢复…", "restore_requested"),
        ("重置数据…", "reset_requested"),
    ],
)
def test_data_security_page_relays_data_requests(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
    with_vault: bool, button_text: str, signal_name: str,
) -> None:
    from PySide6.QtWidgets import QPushButton

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    vault = Vault(tmp_path / "vault.json") if with_vault else None
    page = SettingsPage(None, settings, credentials, vault=vault)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    page.select_tab("数据与安全")

    titles = [button.text() for button in page._rail._buttons]
    assert titles == ["逻辑模型", "站点端点", "数据与安全", "请求日志", "关于"]
    merged = page._pages._stack.currentWidget()
    assert merged is not None
    security = merged.findChild(_SecurityTab)
    assert (security is not None) is with_vault
    if security is not None:
        assert security.isVisible()
        assert security._enable_btn.isVisible()
        assert not security._disable_btn.isVisible()
    button = next(b for b in merged.findChildren(QPushButton) if b.text() == button_text)
    assert button.isVisible()
    with qtbot.waitSignal(getattr(page, signal_name)):
        button.click()


@pytest.mark.parametrize("title", ["数据", "安全", "数据与安全"])
def test_data_security_navigation_selects_merged_page(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, title: str,
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials, vault=Vault(tmp_path / "vault.json"))
    qtbot.addWidget(page)
    page.prepare_open(None, initial_tab=title)
    index = page._rail.index_of("数据与安全")
    assert index >= 0
    assert page._pages.current_index == index
    assert page._rail.current_index == index


def test_data_security_page_scrolls_to_reset_in_small_window(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton, QScrollArea

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials, vault=Vault(tmp_path / "vault.json"))
    qtbot.addWidget(page)
    page.resize(1000, 360)
    page.show()
    page.select_tab("数据与安全")
    qtbot.waitExposed(page)
    merged = page._pages._stack.currentWidget()
    assert isinstance(merged, QScrollArea)
    button = merged.findChild(QPushButton, "resetDataButton")
    assert button is not None
    assert merged.verticalScrollBar().maximum() > 0
    merged.ensureWidgetVisible(button)
    position = button.mapTo(merged.viewport(), QPoint(0, 0))
    assert merged.viewport().rect().contains(button.rect().translated(position))


class _Store:
    def get(self, ref: str) -> str | None:
        return None

    def set(self, ref: str, secret: str) -> None:
        pass

    def delete(self, ref: str) -> None:
        pass

    def refs(self) -> list[str]:
        return []


def test_settings_page_fills_parent_and_uses_vertical_tabs(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)

    assert page._rail.width() == 190
    assert page._rail._layout.direction().value == 2  # TopToBottom
    assert page._pages.current_index == 0
    assert page._rail._indicator.height() > 0
    assert page._rail._indicator.x() == 4

    page.resize(1100, 760)
    assert page._rail._indicator.x() == 4


def test_settings_page_switches_tab_with_animation(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)

    old_indicator_y = page._rail._indicator.y()
    page.select_tab("站点端点", animated=True)
    assert page._pages._effect.isEnabled()
    target = page._rail.index_of("站点端点")
    qtbot.waitUntil(
        lambda: (
            page._pages.current_index == target
            and page._pages._effect.opacity() >= 0.999
            and not page._pages._effect.isEnabled()
            and page._pages._stack.pos().x() == 0
        ),
        timeout=1500,
    )
    assert page._rail._indicator.y() > old_indicator_y
    assert page._rail._indicator.x() == 4


@pytest.mark.parametrize("kind", ["models", "endpoints"])
def test_settings_lists_reverse_refresh_animation_when_selecting_upward(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, kind: str
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    for suffix in ("a", "b"):
        settings.create_model(LogicalModel(id=f"model-{suffix}", name=f"Model {suffix}"))
        settings.upsert_endpoint(
            EndpointConfig(
                id=f"endpoint-{suffix}",
                name=f"Endpoint {suffix}",
                base_url="https://example.com/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            )
        )
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)

    if kind == "models":
        view = page.models_tab._list
        stack = page.models_tab._stack
    else:
        page.select_tab("站点端点", animated=False)
        view = page.endpoints_tab._list
        stack = page.endpoint_workspace._stack

    view.setCurrentRow(1)
    while stack._animation is not None:
        stack._animation.setCurrentTime(stack._animation.duration())
    view.setCurrentRow(0)
    assert stack._outgoing is not None
    assert stack._animation is not None
    stack._animation.setCurrentTime(80)
    assert stack._outgoing.y() > 0
    stack._animation.setCurrentTime(160)
    assert stack._stack.y() < 0


def test_settings_page_can_return_to_chat(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    with qtbot.waitSignal(page.close_requested, timeout=1000):
        page._close_button.click()


def test_endpoint_ready_auto_discovers_then_save_opens_actual_models(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey
) -> None:
    from limbowave.application.services.model_probe import DiscoveredModel, DiscoveryResult

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    credentials.store_secret("relay", "private")
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)
    tab = page.endpoints_tab
    tab._on_new()
    tab._name.setText("Relay")
    with qtbot.waitSignal(page.model_discovery_requested, timeout=1500) as discovery:
        tab._base_url.setText("https://example.com/v1")
    endpoint = discovery.args[0]
    assert endpoint.id == "relay"
    assert endpoint.credential_ref == "relay"
    page.apply_model_discovery(
        endpoint,
        DiscoveryResult((DiscoveredModel("qwen-3.8-max", "Qwen Max"),), "已发现 1 个模型"),
    )

    tab._on_save()
    target = page._rail.index_of("站点端点")
    qtbot.waitUntil(lambda: page._pages.current_index == target, timeout=1500)
    assert page._rail.index_of("实际模型") == -1
    assert page.endpoint_workspace._subtabs.currentIndex() == 1
    assert page.endpoint_workspace._stack.current_index == 1
    assert page.actual_models.endpoint_id == "relay"
    assert len(page.actual_models._rows) == 1
    row = page.actual_models._item("qwen-3.8-max")
    assert row is not None
    assert row.display_name == "Qwen Max"


def test_discovery_failure_routes_manual_add_then_explicit_batch_probe(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from limbowave.application.services.model_probe import DiscoveryResult
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    endpoint = EndpointConfig(
        id="relay", name="Relay", base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    page.actual_models.activate_endpoint(endpoint, discover=False)
    page.apply_model_discovery(endpoint, DiscoveryResult((), "模型清单请求失败（HTTP 404）"))
    page.actual_models._manual_id.setText("my-model")
    with qtbot.waitSignal(page.manual_model_save_requested, timeout=1000):
        page.actual_models._manual_add.click()
    page.apply_manual_model_save(endpoint.id, "my-model")
    row = page.actual_models._item("my-model")
    assert row is not None and not row.selected.isChecked()
    row.selected.click()
    with qtbot.waitSignal(page.model_probe_requested, timeout=1000) as blocker:
        page.actual_models._run.click()
    assert blocker.args[0].model_id == "my-model"
    assert blocker.args[0].endpoint == endpoint


def test_settings_page_relays_unverified_manual_save(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey
) -> None:
    from limbowave.application.services.model_probe import DiscoveryResult
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    endpoint = EndpointConfig(
        id="relay", name="Relay", base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    page.actual_models.activate_endpoint(endpoint, discover=False)
    page.apply_model_discovery(endpoint, DiscoveryResult((), "HTTP 404"))
    page.actual_models._manual_id.setText("manual-model")
    with qtbot.waitSignal(page.manual_model_save_requested, timeout=1000) as blocker:
        page.actual_models._manual_add.click()
    assert blocker.args[0].model_id == "manual-model"


def test_capability_edit_roundtrips_through_service_and_reopens(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from PySide6.QtCore import Qt

    from limbowave.application.services.model_probe import DiscoveryResult
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    endpoint = EndpointConfig(
        id="relay", name="Relay", base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    settings.upsert_endpoint(endpoint)
    settings.record_unverified_model(endpoint_id="relay", model_id="m", display_name="M")
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page._on_endpoint_saved(endpoint)
    page.apply_model_discovery(endpoint, DiscoveryResult((), "HTTP 404"))
    row = page.actual_models._item("m")
    assert row is not None
    assert row.checkboxes["supports_streaming"].checkState() == Qt.CheckState.PartiallyChecked
    with qtbot.waitSignal(page.model_capability_save_requested) as signal:
        row.checkboxes["supports_streaming"].click()
    change = signal.args[0]
    config, _ = settings.set_model_capability(
        endpoint_id=change.endpoint.id, model_id=change.model_id, display_name=change.display_name,
        capability=change.capability, supported=change.supported,
    )
    actual = config.actual_model("relay", "m")
    assert actual is not None
    page.apply_model_capability_save(change, actual)
    assert row.checkboxes["supports_streaming"].isEnabled()
    assert "手动设置" in row.status_text

    reopened = SettingsPage(None, settings, credentials)
    qtbot.addWidget(reopened)
    reopened._on_endpoint_saved(endpoint)
    restored = reopened.actual_models._item("m")
    assert restored is not None
    assert not restored.selected.isChecked()
    assert restored.checkboxes["supports_streaming"].checkState() == Qt.CheckState.Checked
    settings.set_model_capability(
        endpoint_id="relay", model_id="m", display_name="M",
        capability="supports_streaming", supported=False,
    )
    reopened.prepare_open(None)
    assert restored.checkboxes["supports_streaming"].checkState() == Qt.CheckState.Unchecked


def test_probe_edit_lock_survives_endpoint_switch_and_covers_logical_page(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol
    from limbowave.ui.model_probe_page import ModelProbeTask

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    endpoint = EndpointConfig(
        id="relay", name="Relay", base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    other = endpoint.model_copy(update={"id": "other"})
    settings.upsert_endpoint(endpoint)
    settings.upsert_endpoint(other)
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page._on_endpoint_saved(endpoint)
    task = ModelProbeTask(endpoint, "m", "M")
    with qtbot.waitSignal(page.model_probe_requested):
        page.models_tab.probe_requested.emit(task)
    page._on_endpoint_saved(other)
    page._on_endpoint_saved(endpoint)
    row = page.actual_models._item("m")
    assert row is not None
    assert all(not c.isEnabled() for c in row.checkboxes.values())
    assert "检测中" in row.status_text
    page.show_model_probe_error("relay", "m", "连接超时")
    assert all(c.isEnabled() for c in row.checkboxes.values())
    assert not page._probe_tasks


@pytest.mark.parametrize("initial_tab", [None, "站点端点"])
def test_entry_snapshot_keeps_tab_content_at_live_position(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, initial_tab: str | None
) -> None:
    from PySide6.QtCore import QPoint

    from limbowave.ui.main_window import MainWindow

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    page = SettingsPage(window, settings, credentials)
    if initial_tab:
        page.select_tab(initial_tab)
    window.show_full_page(page)
    transition = window._transition
    assert transition is not None
    assert not page._pages._effect.isEnabled()
    control = page.endpoints_tab._list if initial_tab else page.models_tab._list
    expected = control.mapTo(page, QPoint(0, 0))
    assert transition._incoming_widget is page
    assert page.pos() == QPoint(0, 0)
    qtbot.waitUntil(lambda: window._transition is None, timeout=1500)
    assert page.pos() == QPoint(0, 0)
    assert control.mapTo(page, QPoint(0, 0)) == expected


def test_endpoint_subtabs_match_appearance_layout_and_animation(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)
    page.select_tab("站点端点", animated=False)
    workspace = page.endpoint_workspace
    tabs = workspace._subtabs
    stack = workspace._stack
    assert page._rail.index_of("实际模型") == -1
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["端点配置", "实际模型"]
    assert tabs.objectName() == "endpointSubtabs"
    assert tabs._indicator.objectName() == "endpointSubtabIndicator"
    first = stack._stack.widget(0)
    assert first is not None
    assert first.layout().contentsMargins().left() == 18
    assert first.layout().contentsMargins().top() == 14
    second = stack._stack.widget(1)
    assert second is not None
    assert second.layout().contentsMargins().left() == 18
    assert second.layout().contentsMargins().bottom() == 18
    host = tabs.parentWidget()
    assert host is not None
    assert host.layout().contentsMargins().left() == 4

    previous_indicator_x = tabs._indicator.x()
    workspace.select_subtab("实际模型", animated=True)
    assert stack._effect.isEnabled()
    qtbot.waitUntil(
        lambda: stack.current_index == 1 and not stack._effect.isEnabled(), timeout=1500,
    )
    assert tabs._indicator.x() > previous_indicator_x
    workspace.select_subtab("端点配置", animated=False)
    assert stack.current_index == 0
    assert not stack._effect.isEnabled()


def test_direct_actual_models_navigation_selects_parent_and_child(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.select_tab("实际模型", animated=False)
    assert page._pages.current_index == page._rail.index_of("站点端点")
    assert page.endpoint_workspace._stack.current_index == 1
    assert page._rail.current_index == page._rail.index_of("站点端点")


def test_security_password_prompt_uses_login_animation(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QLineEdit, QWidget

    from limbowave.ui.login_page import _AnimatedPasswordEdit
    from limbowave.ui.settings_dialog import _SecurityTab

    class VaultStub:
        def recovery_enabled(self) -> bool:
            return False

    host = QWidget()
    qtbot.addWidget(host)
    host.resize(800, 600)
    tab = _SecurityTab(VaultStub(), host)
    tab.resize(600, 400)
    host.show()
    tab.show()
    tab._enable_btn.click()

    edit = tab.findChild(_AnimatedPasswordEdit)
    assert edit is not None
    assert edit.echoMode() == QLineEdit.EchoMode.Password
    qtbot.keyClicks(edit, "secret")
    assert len(edit._dots) == 6


@pytest.mark.parametrize("theme_index", [0, 1])  # 灵波夜（深色）、清昼（浅色）
def test_rail_hover_never_masks_or_outranks_checked_tab(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, theme_index: int
) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QPushButton

    from limbowave.domain.appearance import BUILTIN_THEMES, MaterialSettings
    from limbowave.ui import theme
    from limbowave.ui.theme_effects import install_hover_suspension

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    theme.apply_appearance_theme(BUILTIN_THEMES[theme_index])
    try:
        page = SettingsPage(None, settings, credentials)
        qtbot.addWidget(page)
        page.resize(1000, 700)
        page.show()
        qtbot.waitExposed(page)
        # 悬停停用默认开启：它把底色写进按钮自身样式表，曾在悬停期间抹掉选中底色。
        install_hover_suspension(page, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
        rail = page._rail
        checked, other = rail._buttons[0], rail._buttons[1]

        def surface(button: QPushButton) -> QColor:
            point = button.mapTo(rail, QPoint(button.width() - 8, button.height() // 2))
            return rail.grab().toImage().pixelColor(point)

        def hover(button: QPushButton | None) -> None:
            # QSS 按钮开着鼠标追踪，只有真实的鼠标移动才会进入 :hover；None 表示移到栏底空白处。
            if button is None:
                qtbot.mouseMove(rail, QPoint(rail.width() // 2, rail.height() - 4))
            else:
                qtbot.mouseMove(button, QPoint(button.width() - 8, button.height() // 2))
            QApplication.processEvents()

        hover(None)
        base = rail.grab().toImage().pixelColor(QPoint(rail.width() // 2, rail.height() - 4))
        selected = surface(checked)
        hover(checked)
        assert surface(checked) == selected
        hover(other)
        hovered = surface(other)
        hover(None)

        def contrast(color: QColor) -> int:
            channels = zip(color.getRgb()[:3], base.getRgb()[:3], strict=True)
            return sum(abs(a - b) for a, b in channels)

        # 悬浮必须可见，但永远比选中弱一档。
        assert 0 < contrast(hovered) < contrast(selected)
    finally:
        theme.apply_appearance_theme(BUILTIN_THEMES[0])


def test_subtab_selected_surface_survives_hover() -> None:
    from limbowave.ui import theme

    css = theme.app_stylesheet()
    hover = css.split("QTabBar#endpointSubtabs::tab:hover {", 1)[1].split("}", 1)[0]
    selected = css.split("QTabBar#endpointSubtabs::tab:selected:hover {", 1)[1].split("}", 1)[0]
    assert f"background: {theme.tab_hover_surface()};" in hover
    assert f"background: {theme.tab_selected_surface()};" in selected


def test_restyle_rebuilds_rgba_surfaces_after_theme_switch(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey
) -> None:
    from dataclasses import replace

    from limbowave.domain.appearance import BUILTIN_THEMES
    from limbowave.ui import theme

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    source = BUILTIN_THEMES[0]
    theme.apply_appearance_theme(source)
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    light = BUILTIN_THEMES[1]
    theme.apply_appearance_theme(
        replace(light, colors=replace(light.colors, tab_indicator="#12AB34"))
    )
    try:
        page.restyle()
        css = page.styleSheet()
        # 指示器和选项卡底色都是 rgba，内联样式的十六进制刷新改不到它们。
        assert "rgba(18, 171, 52," in css
        assert theme.tab_selected_surface() in css
        assert f"#settingsRail {{ background: {theme.BG_SURFACE};" in css
    finally:
        theme.apply_appearance_theme(source)


def test_endpoint_sidebar_is_shared_and_switches_model_context(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    for endpoint_id in ("first", "second"):
        settings.upsert_endpoint(EndpointConfig(
            id=endpoint_id, name=endpoint_id, base_url="https://example.com/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        ))
        settings.record_unverified_model(
            endpoint_id=endpoint_id, model_id=f"{endpoint_id}-model", display_name=endpoint_id,
        )
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1100, 800)
    page.show()
    page.select_tab("站点端点", animated=False)
    workspace = page.endpoint_workspace
    sidebar = page.endpoints_tab.sidebar
    assert sidebar.parentWidget() is workspace._splitter
    assert workspace._stack.parentWidget() is workspace._splitter.widget(1)
    assert not workspace._stack.isAncestorOf(sidebar)

    page.endpoints_tab._list.setCurrentRow(0)
    workspace.select_subtab("实际模型", animated=False)
    assert sidebar.isVisible()
    assert page.actual_models.endpoint_id == "first"
    assert set(page.actual_models._rows) == {"first-model"}
    page.endpoints_tab._list.setCurrentRow(1)
    assert sidebar.isVisible()
    assert workspace._subtabs.currentIndex() == 1
    assert page.actual_models.endpoint_id == "second"
    assert set(page.actual_models._rows) == {"second-model"}
    assert page.endpoints_tab._name.text() == "second"

    page.endpoints_tab.reload()
    assert page.endpoints_tab._selected_id() == "second"
    assert page.actual_models.endpoint_id == "second"
    page.endpoints_tab._on_new()
    assert workspace._subtabs.currentIndex() == 0
    assert page.endpoints_tab._selected_id() is None
    assert page.actual_models.endpoint_id is None
    assert not page.actual_models._rows


@pytest.mark.parametrize("endpoints", [False, True])
@pytest.mark.parametrize("subtab", [0, 1])
def test_list_selection_animates_right_content_and_keeps_subtab(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, endpoints: bool, subtab: int,
) -> None:
    from PySide6.QtCore import QPoint

    from limbowave.domain.models import LogicalModel
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    for suffix in ("a", "b", "c"):
        settings.upsert_endpoint(EndpointConfig(
            id=suffix, name=suffix, base_url="https://example.com/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        ))
        settings.create_model(LogicalModel(id=suffix, name=suffix))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1100, 800)
    page.show()
    page.select_tab("站点端点" if endpoints else "逻辑模型", animated=False)
    tab = page.endpoints_tab if endpoints else page.models_tab
    workspace = page.endpoint_workspace if endpoints else tab
    tabs, stack = workspace._subtabs, workspace._stack
    tab._list.setCurrentRow(0)
    qtbot.waitUntil(lambda: stack._animation is None)
    tabs.setCurrentIndex(subtab)
    qtbot.waitUntil(lambda: stack._animation is None)

    old_image = stack._stack.grab().toImage()
    tab._list.setCurrentRow(1)
    assert tab._name.text() == "b"
    assert tabs.currentIndex() == stack.current_index == subtab
    assert stack._effect.isEnabled()
    assert stack._effect.opacity() == 0.0
    outgoing = stack._outgoing
    assert outgoing is not None
    assert outgoing.pixmap().toImage() == old_image
    assert outgoing.pos() == QPoint(0, 0)
    assert outgoing.graphicsEffect().opacity() == 1.0
    stack._animation.setCurrentTime(80)
    assert outgoing.x() == 0
    assert -14 < outgoing.y() < 0
    assert 0.0 < outgoing.graphicsEffect().opacity() < 1.0
    assert stack._effect.opacity() == 0.0
    stack._animation.setCurrentTime(160)
    assert stack._outgoing is None
    assert stack._stack.pos() == QPoint(0, 18)
    stack._animation.setCurrentTime(100)
    assert 0.0 < stack._effect.opacity() < 1.0
    assert stack._stack.x() == 0
    assert 0 < stack._stack.y() < 18

    # 快速连续切换以及子页动画中切换条目，以最后一次选择为准。
    tabs.setCurrentIndex(1 - subtab)
    tab._list.setCurrentRow(2)
    assert tab._name.text() == "c"
    assert tabs.currentIndex() == stack.current_index == 1 - subtab
    qtbot.waitUntil(lambda: stack._animation is None)
    assert stack._effect.opacity() == 1.0
    assert not stack._effect.isEnabled()
    assert stack._stack.pos() == QPoint(0, 0)
    assert tab._selected_id() == "c"
    if endpoints:
        assert page.actual_models.endpoint_id == "c"
    else:
        assert tab._id.text() == "c"

    # 退场尚未完成就再次换项，旧快照必须被替换并最终释放。
    tab._list.setCurrentRow(0)
    first_outgoing = stack._outgoing
    assert first_outgoing is not None
    stack._animation.setCurrentTime(80)
    tab._list.setCurrentRow(1)
    assert stack._outgoing is not None
    assert stack._outgoing is not first_outgoing
    assert not first_outgoing.isVisible()
    assert tab._name.text() == "b"
    qtbot.waitUntil(lambda: stack._animation is None)
    assert stack._outgoing is None
    assert not stack._effect.isEnabled()
    assert tab._selected_id() == "b"


def test_add_all_models_persists_catalog_without_probing(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from limbowave.application.services.model_probe import DiscoveredModel, DiscoveryResult
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol
    from limbowave.ui.model_probe_page import ModelProbeTask

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    endpoint = EndpointConfig(
        id="relay", name="Relay", base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    )
    settings.upsert_endpoint(endpoint)
    settings.set_model_capability(
        endpoint_id="relay", model_id="saved", display_name="Saved",
        capability="supports_tools", supported=True,
    )
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.actual_models.activate_endpoint(
        endpoint, discover=False, actual_models=settings.load().actual_models,
    )
    page.apply_model_discovery(endpoint, DiscoveryResult(
        (DiscoveredModel("saved", "Saved"), DiscoveredModel("new", "New")), "ok",
    ))
    probes: list[object] = []
    saved: list[str] = []
    page.model_probe_requested.connect(probes.append)

    def save(task: ModelProbeTask) -> None:
        settings.record_unverified_model(
            endpoint_id=task.endpoint.id, model_id=task.model_id, display_name=task.display_name,
        )
        saved.append(task.model_id)
        page.apply_manual_model_save(task.endpoint.id, task.model_id)

    page.manual_model_save_requested.connect(save)
    page.actual_models._select_all.click()
    config = settings.load()
    assert saved == ["new"]
    assert not probes
    assert {model.model_id for model in config.actual_models} == {"saved", "new"}
    existing = config.actual_model("relay", "saved")
    added = config.actual_model("relay", "new")
    assert existing is not None and existing.supports_tools is True
    assert added is not None and added.supports_streaming is None
    assert added.supports_thinking is None
    assert added.supports_tools is False  # 未验证模型沿用服务的保守工具能力默认值。
    page.actual_models.activate_endpoint(
        endpoint, discover=False, actual_models=config.actual_models,
    )
    assert all(row.saved and not row.selected.isChecked() for row in page.actual_models._items())
    assert not page.actual_models._select_all.isEnabled()


def test_about_tab_navigation_and_reopening(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey,
) -> None:
    from PySide6.QtCore import Qt

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)
    index = page._rail.index_of("关于")
    assert index == len(page._rail._buttons) - 1
    qtbot.mouseClick(page._rail._buttons[index], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page._pages._stack.currentWidget() is page.about_tab)
    assert page._rail.current_index == index
    assert page.about_tab.verticalScrollBar().maximum() > 0
    page.select_tab("逻辑模型")
    page.prepare_open(None, "关于")
    assert page._pages._stack.currentWidget() is page.about_tab
    assert page._rail.current_index == index
    # 只读信息页不会创建配置、收集诊断信息或要求可选服务。
    assert not (tmp_path / "config.json").exists()
    with qtbot.waitSignal(page.close_requested, timeout=1000):
        page._close_button.click()


@pytest.mark.parametrize(
    ("title", "workspace_attr"),
    [
        ("逻辑模型", "models_tab"),
        ("站点端点", "endpoint_workspace"),
        ("外观", "appearance_tab"),
    ],
)
def test_settings_subtabs_slide_horizontally_in_both_directions(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, title: str, workspace_attr: str,
) -> None:
    from PySide6.QtCore import QAbstractAnimation, QPoint

    from limbowave.application.services.preferences_service import PreferencesService

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    page = SettingsPage(
        None, settings, credentials,
        preferences=PreferencesService(tmp_path / "preferences.json"),
    )
    qtbot.addWidget(page)
    page.resize(1100, 800)
    page.select_tab(title, animated=False)
    page.show()
    qtbot.waitExposed(page)
    workspace = getattr(page, workspace_attr)
    tabs, stack = workspace._subtabs, workspace._stack
    qtbot.waitUntil(lambda: stack._animation is None)

    # Visit every subtab in both directions, including all seven appearance pages.
    targets = [*range(1, tabs.count()), *reversed(range(tabs.count() - 1))]
    for target in targets:
        direction = 1 if target > stack.current_index else -1
        tabs.setCurrentIndex(target)
        assert stack._animation is not None
        stack._animation.setCurrentTime(80)
        assert stack._stack.y() == 0
        assert -14 < stack._stack.x() * direction < 0
        assert 0.0 < stack._effect.opacity() < 1.0
        stack._animation.setCurrentTime(160)
        assert stack.current_index == target
        assert stack._stack.pos() == QPoint(direction * 18, 0)
        assert stack._animation is not None
        stack._animation.setCurrentTime(100)
        assert stack._stack.y() == 0
        assert 0 < stack._stack.x() * direction < 18
        stack._animation.setCurrentTime(230)
        assert stack._animation is None
        assert stack._stack.pos() == QPoint(0, 0)
        assert not stack._effect.isEnabled()
    qtbot.waitUntil(
        lambda: tabs._indicator_animation.state() == QAbstractAnimation.State.Stopped,
        timeout=1500,
    )
    assert tabs._indicator.geometry() == tabs._target_rect(tabs.currentIndex())


def test_request_log_loading_failure_and_reopen_do_not_poison_settings(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, monkeypatch,
) -> None:
    from concurrent.futures import Future

    from PySide6.QtCore import Qt

    from limbowave.application.services.request_log_service import RequestLogService, RunLogPage
    from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
    from limbowave.ui.request_log_dialog import RequestLogDialog

    service = RequestLogService(in_memory_uow_factory())
    pending = []

    def load(conversation_id, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        pending.append((conversation_id, future))
        return future

    monkeypatch.setattr(service, "load_page_async", load)
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    page = SettingsPage(None, settings, credentials, request_logs=service, conversation_id="c1")
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    request_index = page._rail.index_of("请求日志")
    qtbot.mouseClick(page._rail._buttons[request_index], Qt.MouseButton.LeftButton)
    viewer = page.findChild(RequestLogDialog)
    assert viewer is not None
    assert len(pending) == 1
    qtbot.waitUntil(lambda: page._pages._animation is None)
    assert page._pages.current_index == request_index
    assert "加载" in viewer._summary.text()

    about = page._rail.index_of("关于")
    qtbot.mouseClick(page._rail._buttons[about], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page._pages._animation is None)
    assert page._pages.current_index == about
    pending[0][1].set_exception(ValueError("unreadable logs"))
    qtbot.waitUntil(lambda: viewer._page_future is None)
    assert "加载失败" in viewer._summary.text()
    assert page.about_tab.isVisible()

    qtbot.mouseClick(page._rail._buttons[request_index], Qt.MouseButton.LeftButton)
    page._pages._animation.setCurrentTime(80)
    page.hide()
    page.prepare_open("c2")
    page.show()
    assert page.findChild(RequestLogDialog) is viewer
    assert page._pages.current_index == request_index
    assert page._pages._animation is None
    assert page._pages._effect.opacity() == 1.0
    assert not page._pages._effect.isEnabled()
    assert pending[-1][0] == "c2"
    pending[-1][1].set_result(RunLogPage([], False))
    qtbot.waitUntil(lambda: viewer._page_future is None)
    assert "暂无请求日志" in viewer._summary.text()
    pending[-2][1].set_exception(ValueError("stale failure"))
    viewer._poll_results()
    assert "暂无请求日志" in viewer._summary.text()
    page.prepare_open(None)
    assert page.findChild(RequestLogDialog) is not viewer or not viewer.isVisible()
    page.select_tab("逻辑模型")
    assert page.models_tab.isVisible()
    service.close()
