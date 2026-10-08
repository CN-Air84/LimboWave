"""首次引导：没配模型时要出现，配好后密钥不进配置。"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.onboarding import (
    complete_onboarding,
    endpoint_identity,
    needs_onboarding,
    protocol_for,
    save_onboarding_endpoint,
)
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey


def test_empty_configuration_needs_onboarding(tmp_path: Path) -> None:
    config = ConfigurationService(JsonConfigRepository(tmp_path / "config.json")).load()
    assert needs_onboarding(config)


def test_claude_uses_anthropic_and_custom_ids_come_from_the_host() -> None:
    assert protocol_for("claude") is ProviderProtocol.ANTHROPIC_MESSAGES
    assert protocol_for("deepseek") is ProviderProtocol.OPENAI_COMPLETIONS
    assert endpoint_identity("kimi", "https://api.moonshot.cn/v1", custom=False) == "kimi"
    assert (
        endpoint_identity("custom", "https://Relay.Example.com/v1", custom=True)
        == "custom-relay-example-com"
    )


def test_save_onboarding_endpoint_stores_the_secret_outside_config(
    tmp_path: Path, vault_key: VaultKey
) -> None:
    config_path = tmp_path / "config.json"
    secrets_path = tmp_path / "secrets.json"
    settings = SettingsService(ConfigurationService(JsonConfigRepository(config_path)))
    save_onboarding_endpoint(
        settings=settings,
        credentials=CredentialService(SecretStore(vault_key, secrets_path)),
        provider_id="deepseek",
        provider_name="DeepSeek",
        custom=False,
        url="https://api.deepseek.com",
        secret="  sk-live  ",
    )
    stored = config_path.read_text(encoding="utf-8")
    assert "sk-live" not in stored
    config = ConfigurationService(JsonConfigRepository(config_path)).load()
    assert needs_onboarding(config)
    assert config.default_model_id is None
    assert not config.models and not config.actual_models
    endpoint = config.endpoints[0]
    assert endpoint.credential_ref == "deepseek"
    assert endpoint.base_url == "https://api.deepseek.com"
    assert CredentialService(SecretStore(vault_key, secrets_path)).resolve("deepseek") == "sk-live"


def test_empty_secret_leaves_the_endpoint_without_a_credential(
    tmp_path: Path, vault_key: VaultKey
) -> None:
    config_path = tmp_path / "config.json"
    settings = SettingsService(ConfigurationService(JsonConfigRepository(config_path)))
    save_onboarding_endpoint(
        settings=settings,
        credentials=CredentialService(SecretStore(vault_key, tmp_path / "secrets.json")),
        provider_id="deepseek",
        provider_name="DeepSeek",
        custom=False,
        url="https://api.deepseek.com",
        secret="   ",
    )
    config = ConfigurationService(JsonConfigRepository(config_path)).load()
    assert config.endpoints[0].credential_ref is None
    assert needs_onboarding(config)


@pytest.mark.parametrize(
    ("provider_id", "provider_name", "custom", "url", "display_name", "expected_name"),
    [
        ("deepseek", "DeepSeek", False, "https://api.deepseek.com", "  工作账号  ", "工作账号"),
        ("claude", "Claude", False, "https://api.anthropic.com", "我的 Claude", "我的 Claude"),
        ("custom", "自定义", True, "https://relay.example/v1", "  我的中转  ", "我的中转"),
        ("deepseek", "DeepSeek", False, "https://api.deepseek.com", "   ", "DeepSeek"),
        ("custom", "自定义", True, "https://relay.example/v1", "", "relay.example"),
        ("custom", "自定义", True, "https://relay.example/v1", "   ", "relay.example"),
    ],
)
def test_save_onboarding_endpoint_persists_display_name_without_changing_identity(
    tmp_path: Path,
    vault_key: VaultKey,
    provider_id: str,
    provider_name: str,
    custom: bool,
    url: str,
    display_name: str,
    expected_name: str,
) -> None:
    config_path = tmp_path / "config.json"
    settings = SettingsService(ConfigurationService(JsonConfigRepository(config_path)))
    save_onboarding_endpoint(
        settings=settings,
        credentials=CredentialService(SecretStore(vault_key, tmp_path / "secrets.json")),
        provider_id=provider_id,
        provider_name=provider_name,
        custom=custom,
        url=url,
        secret="sk-test",
        display_name=display_name,
    )
    config = ConfigurationService(JsonConfigRepository(config_path)).load()
    endpoint = config.endpoints[0]
    assert endpoint.name == expected_name
    assert endpoint.id == endpoint_identity(provider_id, url, custom=custom)
    assert endpoint.credential_ref == endpoint.id
    assert endpoint.api == protocol_for(provider_id)
    assert not config.models and not config.actual_models
    assert needs_onboarding(config)


def _settings_with_endpoint(tmp_path: Path) -> SettingsService:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    settings.upsert_endpoint(
        EndpointConfig(
            id="deepseek",
            name="DeepSeek",
            base_url="https://api.deepseek.com",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
            rpm=123,
            headers={"X-Test": "retained"},
        )
    )
    return settings


def test_endpoint_resave_preserves_advanced_settings(tmp_path: Path, vault_key: VaultKey) -> None:
    settings = _settings_with_endpoint(tmp_path)
    saved = save_onboarding_endpoint(
        settings=settings,
        credentials=CredentialService(SecretStore(vault_key, tmp_path / "secrets.json")),
        provider_id="deepseek",
        provider_name="DeepSeek",
        custom=False,
        url="https://api.deepseek.com",
        secret="sk-new",
        display_name="New name",
    )
    assert saved.name == "New name"
    assert saved.rpm == 123
    assert saved.headers == {"X-Test": "retained"}
    assert len(settings.load().endpoints) == 1


def test_finish_requires_a_saved_actual_model(tmp_path: Path) -> None:
    settings = _settings_with_endpoint(tmp_path)
    before = settings.load()
    with pytest.raises(ValueError, match="请先添加模型"):
        complete_onboarding(settings, "deepseek", "not-added")
    with pytest.raises(ValueError, match="站点不存在"):
        complete_onboarding(settings, "missing", "not-added")
    assert settings.load() == before


def test_finish_uses_selected_model_capabilities_and_is_idempotent(tmp_path: Path) -> None:
    settings = _settings_with_endpoint(tmp_path)
    settings.record_unverified_model(
        endpoint_id="deepseek", model_id="chosen", display_name="Chosen"
    )
    settings.set_model_capability(
        endpoint_id="deepseek",
        model_id="chosen",
        display_name="Chosen",
        capability="supports_tools",
        supported=True,
    )
    assert needs_onboarding(settings.load())
    first = complete_onboarding(settings, "deepseek", "chosen")
    assert first == complete_onboarding(settings, "deepseek", "chosen")
    config = settings.load()
    assert not needs_onboarding(config)
    assert len(config.models) == 1
    assert config.default_model_id == first
    assert config.models[0].name == "Chosen"
    assert config.models[0].bindings[0].supports_tools


def test_finish_promotes_selected_endpoint_in_existing_binding(tmp_path: Path) -> None:
    settings = _settings_with_endpoint(tmp_path)
    settings.upsert_endpoint(
        EndpointConfig(
            id="other",
            name="Other",
            base_url="https://other.example",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        )
    )
    settings.create_model(
        LogicalModel(
            id="chosen",
            name="Existing name",
            bindings=[
                ModelBinding(endpoint_id="other", model_id="chosen"),
                ModelBinding(endpoint_id="deepseek", model_id="chosen"),
            ],
        )
    )
    assert complete_onboarding(settings, "deepseek", "chosen") == "chosen"
    config = settings.load()
    assert len(config.models) == 1
    assert config.models[0].name == "Existing name"
    assert [b.endpoint_id for b in config.models[0].bindings] == ["deepseek", "other"]


def test_finish_avoids_all_existing_logical_model_ids(tmp_path: Path) -> None:
    settings = _settings_with_endpoint(tmp_path)
    settings.record_unverified_model(
        endpoint_id="deepseek", model_id="chosen", display_name="Chosen"
    )
    for logical_id in ("chosen", "deepseek--chosen"):
        # These existing unbound logical models must not be overwritten.
        settings.upsert_model(LogicalModel(id=logical_id, name="Existing"))
    chosen = complete_onboarding(settings, "deepseek", "chosen")
    assert chosen == "deepseek--chosen-2"
    config = settings.load()
    assert len(config.models) == 3
    assert config.models[0].name == "Existing"
    assert config.models[-1].bindings[0].key == ("deepseek", "chosen")
