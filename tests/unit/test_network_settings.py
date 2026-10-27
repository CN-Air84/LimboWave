from __future__ import annotations

import pytest
from pydantic import ValidationError

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.network_settings_service import NetworkSettingsService
from limbowave.application.services.settings_service import SettingsService, _rebuild
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.network import NetworkSettings, SearchProvider, WebAccessError
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore


@pytest.fixture
def services(tmp_path, vault_key):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    return NetworkSettingsService(settings, credentials), settings, credentials


def test_old_config_defaults_disabled_and_rebuild_preserves_network():
    assert AppConfiguration.model_validate({}).network.search_provider is SearchProvider.DISABLED
    network = NetworkSettings(
        search_provider=SearchProvider.TAVILY, credential_ref="ref", max_results=3
    )
    original = AppConfiguration(network=network)
    assert _rebuild(original).network == network
    assert AppConfiguration.model_validate_json(original.model_dump_json()).network == network


@pytest.mark.parametrize(
    "value",
    [
        {"max_results": 0},
        {"max_results": 11},
        {"max_results": True},
        {"api_key": "secret"},
        {"search_provider": "unknown"},
    ],
)
def test_config_is_bounded_and_does_not_accept_plaintext_keys(value):
    with pytest.raises(ValidationError):
        NetworkSettings.model_validate(value)


def test_secret_only_encrypted_and_reference_not_from_caller(services, tmp_path):
    service, settings, credentials = services
    saved = service.save(
        NetworkSettings(search_provider=SearchProvider.SERPAPI_GOOGLE, credential_ref="model-key"),
        secret="NET-SECRET",
    )
    assert saved.credential_ref.startswith("network.search.")
    assert credentials.resolve(saved.credential_ref) == "NET-SECRET"
    assert settings.load().network == saved
    assert "NET-SECRET" not in (tmp_path / "config.json").read_text()
    assert "NET-SECRET" not in (tmp_path / "secrets.json").read_text()
    assert service.save(saved).credential_ref == saved.credential_ref


def test_provider_switch_does_not_reuse_or_send_previous_key(services):
    service, _, credentials = services
    old = service.save(
        NetworkSettings(search_provider=SearchProvider.SERPAPI_GOOGLE), secret="GOOGLE-KEY"
    )
    with pytest.raises(WebAccessError):
        service.save(NetworkSettings(search_provider=SearchProvider.TAVILY))
    assert service.load() == old and credentials.resolve(old.credential_ref) == "GOOGLE-KEY"
    updated = service.save(
        NetworkSettings(search_provider=SearchProvider.TAVILY), secret="TAVILY-KEY"
    )
    assert credentials.resolve(old.credential_ref) is None
    assert credentials.resolve(updated.credential_ref) == "TAVILY-KEY"


def test_clear_key_disables_search_and_deletes_vault_reference(services):
    service, _, credentials = services
    saved = service.save(
        NetworkSettings(search_provider=SearchProvider.SERPAPI_BING), secret="BING-KEY"
    )
    cleared = service.save(saved, clear_key=True)
    assert cleared.search_provider is SearchProvider.DISABLED and cleared.credential_ref is None
    assert credentials.resolve(saved.credential_ref) is None


def test_locked_vault_does_not_save_a_key(services):
    _, settings, _ = services
    locked = NetworkSettingsService(settings, None)
    with pytest.raises(WebAccessError) as exc:
        locked.save(NetworkSettings(search_provider=SearchProvider.TAVILY), secret="KEY")
    assert exc.value.code == "vault_locked" and settings.load().network == NetworkSettings()


def test_failed_configuration_save_removes_new_secret_not_old(services, monkeypatch):
    service, settings, credentials = services
    old = service.save(NetworkSettings(search_provider=SearchProvider.TAVILY), secret="OLD")

    def fail(value):
        raise OSError("SECRET-DETAIL")

    monkeypatch.setattr(settings, "update_network", fail)
    with pytest.raises(WebAccessError) as exc:
        service.save(old, secret="NEW")
    assert "SECRET-DETAIL" not in str(exc.value)
    assert credentials.list_refs() == [old.credential_ref]
    assert credentials.resolve(old.credential_ref) == "OLD"


def test_custom_provider_requires_https_endpoint_and_persists_it(services):
    service, _, credentials = services
    with pytest.raises(ValueError):
        NetworkSettings(search_provider=SearchProvider.CUSTOM_JSON)
    saved = service.save(
        NetworkSettings(
            search_provider=SearchProvider.CUSTOM_JSON,
            custom_search_endpoint="https://search.example.com/api",
        ),
        secret="CUSTOM-KEY",
    )
    assert saved.custom_search_endpoint == "https://search.example.com/api"
    assert credentials.resolve(saved.credential_ref) == "CUSTOM-KEY"


def test_non_custom_provider_drops_custom_endpoint(services):
    service, _, _ = services
    saved = service.save(
        NetworkSettings(
            search_provider=SearchProvider.TAVILY,
            custom_search_endpoint="https://search.example.com/api",
        ),
        secret="KEY",
    )
    assert saved.custom_search_endpoint is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://search.example.com/api?api_key=LEAK",
        "https://search.example.com/api?access_token=LEAK",
        "https://search.example.com/api?Authorization=LEAK",
    ],
)
def test_custom_endpoint_rejects_secret_query_parameters(endpoint):
    with pytest.raises(ValueError, match="不能在普通配置中携带密钥参数"):
        NetworkSettings(
            search_provider=SearchProvider.CUSTOM_JSON,
            custom_search_endpoint=endpoint,
        )
