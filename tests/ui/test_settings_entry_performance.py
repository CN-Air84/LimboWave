from unittest.mock import Mock

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.preferences_service import PreferencesService
from limbowave.application.services.settings_service import SettingsService
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.ui.settings_panel import SettingsPage


def make_page(qtbot, tmp_path, vault_key, **kwargs):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    page = SettingsPage(
        None, settings, credentials,
        preferences=PreferencesService(tmp_path / "preferences.json"), **kwargs,
    )
    qtbot.addWidget(page)
    return page


def test_entry_builds_only_the_selected_category(qtbot, tmp_path, vault_key):
    page = make_page(qtbot, tmp_path, vault_key)
    assert page._models_tab is not None
    assert page._endpoint_workspace is None
    assert page._appearance_tab is None
    assert page._reminder_notifications_tab is None
    assert page._presence_tab is None
    assert page._about_tab is None
    page.prepare_open(None)
    page.resolve_pending_changes()
    page.restyle()
    assert page._endpoint_workspace is None
    assert page._appearance_tab is None
    assert page._about_tab is None


def test_lazy_category_is_created_once_without_changing_selection(qtbot, tmp_path, vault_key):
    page = make_page(qtbot, tmp_path, vault_key)
    created = []
    page.tab_created.connect(lambda title, tab: created.append((title, tab)))
    index = page._pages.current_index
    appearance = page.appearance_tab
    assert page._pages.current_index == index
    page.select_tab("外观")
    assert page.appearance_tab is appearance
    assert created == [("外观", appearance)]
    page.prepare_open(None, "关于")
    page.select_tab("外观")
    assert page.appearance_tab is appearance
    assert len(created) == 2


def test_reopening_does_not_reload_hidden_categories(qtbot, tmp_path, vault_key, monkeypatch):
    page = make_page(qtbot, tmp_path, vault_key)
    endpoints = page.endpoints_tab
    reload = Mock(wraps=endpoints.reload)
    monkeypatch.setattr(endpoints, "reload", reload)
    page.prepare_open(None, "关于")
    reload.assert_not_called()
    page.select_tab("站点端点")
    reload.assert_called_once()


def test_lan_factory_runs_only_when_selected(qtbot, tmp_path, vault_key):
    from PySide6.QtWidgets import QWidget

    factory = Mock(side_effect=QWidget)
    page = make_page(qtbot, tmp_path, vault_key, lan_panel=factory)
    factory.assert_not_called()
    page.select_tab("局域网访问")
    factory.assert_called_once()
    page.select_tab("逻辑模型")
    page.select_tab("局域网访问")
    factory.assert_called_once()


def test_settings_import_does_not_load_hidden_ui_or_network_stack():
    import subprocess
    import sys

    code = """
import sys
from limbowave.ui.settings_panel import SettingsPage
assert SettingsPage is not None
for name in (
    'httpx', 'limbowave.ui.settings_dialog', 'limbowave.ui.model_probe_page',
    'limbowave.ui.about_page', 'limbowave.ui.session_memory_panel',
):
    assert name not in sys.modules, name
from limbowave.ui.logical_models_tab import LogicalModelsTab
assert LogicalModelsTab is not None
assert 'httpx' not in sys.modules
assert 'limbowave.ui.model_probe_page' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_hidden_endpoint_data_catches_up_on_selection(qtbot, tmp_path, vault_key):
    from limbowave.domain.providers import EndpointConfig, ProviderProtocol

    page = make_page(qtbot, tmp_path, vault_key)
    endpoints = page.endpoints_tab
    assert endpoints._list.count() == 0
    page._settings.upsert_endpoint(EndpointConfig(
        id="new", name="New endpoint", base_url="https://example.com/v1",
        api=ProviderProtocol.OPENAI_COMPLETIONS,
    ))
    page.prepare_open(None, "关于")
    assert endpoints._list.count() == 0
    page.select_tab("站点端点")
    assert endpoints._list.count() == 1
    assert endpoints._list.item(0).text() == "New endpoint"


def test_lazy_page_uses_the_current_theme(qtbot, tmp_path, vault_key, monkeypatch):
    from PySide6.QtWidgets import QLabel

    from limbowave.ui import theme

    page = make_page(qtbot, tmp_path, vault_key)
    monkeypatch.setattr(theme, "TEXT_SECONDARY", "#123456")
    monkeypatch.setattr(theme, "FS_SMALL", 19)
    page.restyle()
    page.select_tab("数据与安全")
    note = next(
        label for label in page._pages._stack.currentWidget().findChildren(QLabel)
        if "备份使用独立密码" in label.text()
    )
    assert "#123456" in note.styleSheet()
    assert "19px" in note.styleSheet()

def test_application_wiring_keeps_optional_tabs_lazy_and_connects_them_once(
    qtbot, tmp_path, vault_key, monkeypatch,
):
    import asyncio
    from dataclasses import replace

    from PySide6.QtWidgets import QApplication, QWidget
    from qasync import QEventLoop

    import limbowave.app as app
    from limbowave.application.services.reminder_scheduler import ReminderScheduler
    from limbowave.bootstrap import AppPaths
    from limbowave.infrastructure import shell
    from limbowave.ui.gentle_presence import GentlePresenceController
    from limbowave.ui.main_window import MainWindow
    from limbowave.ui.reminder_notifications import ReminderNotifications

    async def idle_scheduler(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(ReminderScheduler, "run", idle_scheduler)
    monkeypatch.setattr(app, "_build_kernel", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "probe_shell", lambda *args: None)
    window = MainWindow()
    qtbot.addWidget(window)
    loop = QEventLoop(QApplication.instance())
    asyncio.set_event_loop(loop)
    shutdown = None
    try:
        _, _, warm, _, shutdown = app._wire(
            window, AppPaths(tmp_path, tmp_path / "logs"), vault_key,
            appearance_prepared=True,
        )
        warm()
        page = window.findChild(SettingsPage)
        assert page is not None
        assert page._endpoint_workspace is None
        assert page._reminder_notifications_tab is None
        assert page._presence_tab is None
        assert window.findChild(QWidget, "lanAccessPanel") is None
        reminders = window.findChild(ReminderNotifications)
        presence = window.findChild(GentlePresenceController)
        assert reminders is not None and presence is not None
        # Controllers can change while their UI does not exist. Materialization
        # must display the live state, rather than the initial preference file.
        reminders.configure(replace(reminders.settings, sound_enabled=False))
        presence.mute()
        reminder_tab = page.reminder_notifications_tab
        presence_tab = page.presence_tab
        assert reminder_tab is not None and presence_tab is not None
        assert not reminder_tab.sound.isChecked()
        assert presence_tab._muted.isChecked()
        # Repeated entry must not attach duplicate subscribers.
        warm()
        assert page.reminder_notifications_tab is reminder_tab
        assert page.presence_tab is presence_tab
        observed = []
        reminders.settings_changed.connect(observed.append)
        reminder_tab.sound.setChecked(True)
        assert reminders.settings.sound_enabled
        assert len(observed) == 1
    finally:
        if shutdown is not None:
            loop.run_until_complete(shutdown())
        loop.close()
        asyncio.set_event_loop(None)
