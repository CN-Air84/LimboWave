"""首次使用：先保存端点，再由用户选择已添加的实际模型作为默认聊天模型。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.routing_service import RoutingService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.routing import RoutingError


def needs_onboarding(config: AppConfiguration) -> bool:
    """还路由不到一个模型时，启动要先走首次引导。"""
    try:
        RoutingService(config).route()
    except RoutingError:
        return True
    return False


def protocol_for(provider_id: str) -> ProviderProtocol:
    if provider_id == "claude":
        return ProviderProtocol.ANTHROPIC_MESSAGES
    return ProviderProtocol.OPENAI_COMPLETIONS


def endpoint_identity(provider_id: str, url: str, *, custom: bool) -> str:
    if not custom:
        return provider_id
    host = (urlparse(url).hostname or "custom").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "custom"
    return f"custom-{slug}"[:64]


def endpoint_label(provider_name: str, url: str, *, custom: bool) -> str:
    if not custom:
        return provider_name
    return urlparse(url).hostname or provider_name


def save_onboarding_endpoint(
    *,
    settings: SettingsService,
    credentials: CredentialService,
    provider_id: str,
    provider_name: str,
    custom: bool,
    url: str,
    secret: str,
    display_name: str = "",
) -> EndpointConfig:
    """仅保存端点和加密密钥，不隐式导入或选择远端模型。"""
    endpoint_id = endpoint_identity(provider_id, url, custom=custom)
    existing = next((e for e in settings.load().endpoints if e.id == endpoint_id), None)
    values = existing.model_dump() if existing is not None else {}
    secret_text = secret.strip()
    endpoint = EndpointConfig.model_validate(
        {
            **values,
            "id": endpoint_id,
            "name": display_name.strip() or endpoint_label(provider_name, url, custom=custom),
            "base_url": url,
            "api": protocol_for(provider_id),
            "credential_ref": endpoint_id if secret_text else None,
        }
    )
    if secret_text:
        credentials.store_secret(endpoint_id, secret_text)
    settings.upsert_endpoint(endpoint)
    return endpoint


def complete_onboarding(settings: SettingsService, endpoint_id: str, model_id: str) -> str:
    """把明确选择的已保存实际模型绑定为默认；复用现有绑定，避免重复创建。"""
    config = settings.load()
    endpoint = next((e for e in config.endpoints if e.id == endpoint_id), None)
    if endpoint is None:
        raise ValueError("站点不存在，请返回修改")
    actual = config.actual_model(endpoint_id, model_id)
    if actual is None:
        raise ValueError("请先添加模型，再选择默认聊天模型")
    owner = config.binding_owner(endpoint_id, model_id)
    if owner is not None:
        logical_id = owner
        model = next(m for m in config.models if m.id == owner)
        # 用户明确选中的站点必须排首位；同一实际模型不能另建重复绑定。
        if model.bindings[0].key != actual.key:
            settings.reorder_bindings(
                owner,
                [
                    endpoint_id,
                    *(b.endpoint_id for b in model.bindings if b.endpoint_id != endpoint_id),
                ],
            )
    else:
        used = {m.id for m in config.models}
        base = model_id if model_id not in used else f"{endpoint_id}--{model_id}"
        logical_id = base
        suffix = 2
        while logical_id in used:
            logical_id = f"{base}-{suffix}"
            suffix += 1
        settings.create_model(
            LogicalModel(
                id=logical_id,
                name=actual.display_name,
                bindings=[ModelBinding(endpoint_id=endpoint_id, model_id=model_id)],
            )
        )
    settings.set_default_model(logical_id)
    return logical_id
