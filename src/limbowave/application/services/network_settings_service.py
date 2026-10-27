"""Write-only search key editing backed by the existing encrypted credential vault."""

from __future__ import annotations

from contextlib import suppress
from uuid import uuid4

from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.network import NetworkSettings, SearchProvider, WebAccessError


class NetworkSettingsService:
    def __init__(self, settings: SettingsService, credentials: CredentialService | None) -> None:
        self._settings = settings
        self._credentials = credentials

    @property
    def unlocked(self) -> bool:
        return self._credentials is not None

    def load(self) -> NetworkSettings:
        return self._settings.load().network

    def has_secret(self, ref: str | None) -> bool:
        try:
            return bool(ref and self._credentials and ref in self._credentials.list_refs())
        except Exception:
            return False

    def save(
        self,
        settings: NetworkSettings,
        *,
        secret: str = "",
        clear_key: bool = False,
    ) -> NetworkSettings:
        created: str | None = None
        try:
            old = self.load()
            # Never accept caller-supplied references or inherit another provider's key.
            ref = old.credential_ref if old.search_provider == settings.search_provider else None
            secret = secret.strip()
            if clear_key:
                if self._credentials is None:
                    raise WebAccessError("vault_locked", "请先解锁资料库再清除搜索凭据")
                ref = None
            elif secret:
                if self._credentials is None:
                    raise WebAccessError("vault_locked", "请先解锁资料库再保存搜索凭据")
                if len(secret) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
                    raise WebAccessError("invalid_credentials", "搜索 API Key 格式无效")
                ref = created = "network.search." + uuid4().hex
                self._credentials.store_secret(ref, secret)
            provider = SearchProvider.DISABLED if clear_key else settings.search_provider
            if provider is not SearchProvider.DISABLED and not self.has_secret(ref):
                raise WebAccessError(
                    "credentials_unavailable", "启用搜索前请填写密钥；更换搜索服务需重新填写"
                )
            value = NetworkSettings(
                search_provider=provider,
                credential_ref=ref,
                max_results=settings.max_results,
                custom_search_endpoint=(
                    settings.custom_search_endpoint
                    if provider is SearchProvider.CUSTOM_JSON
                    else None
                ),
            )
            self._settings.update_network(value)
        except Exception as exc:
            if created and self._credentials:
                with suppress(Exception):
                    self._credentials.delete_secret(created)
            if isinstance(exc, WebAccessError):
                raise
            raise WebAccessError(
                "settings_failed", "联网设置未保存，请检查资料库和配置文件是否可写"
            ) from None
        if (
            self._credentials
            and old.credential_ref != ref
            and old.credential_ref
            and old.credential_ref.startswith("network.search.")
        ):
            with suppress(Exception):
                self._credentials.delete_secret(old.credential_ref)
        return value
