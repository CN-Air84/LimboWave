from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLineEdit

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.network_settings_service import NetworkSettingsService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.network import NetworkSettings, SearchProvider
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.ui.network_settings_page import NetworkSettingsPage
from limbowave.ui.settings_panel import SettingsPage


@pytest.fixture
def services(tmp_path, vault_key):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    return NetworkSettingsService(settings, credentials), settings, credentials


def test_save_key_masked_and_never_prefilled(qtbot, services):
    service, _, credentials = services
    page = NetworkSettingsPage(service)
    qtbot.addWidget(page)
    assert page.key.echoMode() == QLineEdit.EchoMode.Password
    page.provider.setCurrentIndex(page.provider.findData("serpapi_google"))
    page.key.setText("NEW-API-KEY")
    page.max_results.setValue(3)
    page.save()
    saved = service.load()
    assert saved.search_provider is SearchProvider.SERPAPI_GOOGLE
    assert saved.max_results == 3 and credentials.resolve(saved.credential_ref) == "NEW-API-KEY"
    assert page.key.text() == "" and "未进行在线验证" in page.status.text()
    assert "NEW-API-KEY" not in page.feedback.text()


def test_switching_provider_clears_unsaved_key(qtbot, services):
    service, _, _ = services
    page = NetworkSettingsPage(service)
    qtbot.addWidget(page)
    page.key.setText("DONT-SEND-TO-OTHER-PROVIDER")
    page.provider.setCurrentIndex(page.provider.findData("tavily"))
    assert page.key.text() == ""
    page.save()
    assert service.load().search_provider is SearchProvider.DISABLED
    assert "密钥" in page.feedback.text()


def test_clear_and_locked_vault_states(qtbot, services):
    service, settings, credentials = services
    saved = service.save(NetworkSettings(search_provider=SearchProvider.TAVILY), secret="KEY")
    page = NetworkSettingsPage(service)
    qtbot.addWidget(page)
    assert page.clear_button.isEnabled()
    page.clear_key()
    assert service.load().search_provider is SearchProvider.DISABLED
    assert credentials.resolve(saved.credential_ref) is None
    locked = NetworkSettingsPage(NetworkSettingsService(settings, None))
    qtbot.addWidget(locked)
    assert not locked.key.isEnabled() and not locked.clear_button.isEnabled()
    assert "未解锁" in locked.status.text()


def test_hiding_page_clears_typed_secret(qtbot, services):
    page = NetworkSettingsPage(services[0])
    qtbot.addWidget(page)
    page.show()
    page.key.setText("UNSAVED-KEY")
    page.hide()
    assert page.key.text() == ""


def test_network_page_is_lazy_and_openable(qtbot, services):
    _, settings, credentials = services
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    index = page._rail.index_of("联网搜索")
    assert index >= 0 and index in page._pending_pages
    page.select_tab("联网搜索")
    assert index not in page._pending_pages
    assert len(page.findChildren(NetworkSettingsPage)) == 1


def test_custom_provider_shows_endpoint_and_saves_contract(qtbot, services):
    service, _, credentials = services
    page = NetworkSettingsPage(service)
    qtbot.addWidget(page)
    page.provider.setCurrentIndex(page.provider.findData("custom_json"))
    assert page.custom_endpoint.isEnabled()
    page.custom_endpoint.setText("https://search.example.com/v1/search")
    page.key.setText("CUSTOM-KEY")
    page.save()
    saved = service.load()
    assert saved.search_provider is SearchProvider.CUSTOM_JSON
    assert saved.custom_search_endpoint == "https://search.example.com/v1/search"
    assert credentials.resolve(saved.credential_ref) == "CUSTOM-KEY"


def test_non_custom_provider_hides_stale_custom_endpoint(qtbot, services):
    page = NetworkSettingsPage(services[0])
    qtbot.addWidget(page)
    page.provider.setCurrentIndex(page.provider.findData("custom_json"))
    page.custom_endpoint.setText("https://search.example.com/v1/search")
    page.provider.setCurrentIndex(page.provider.findData("azure_bing"))
    assert not page.custom_endpoint.isEnabled() and page.custom_endpoint.text() == ""
