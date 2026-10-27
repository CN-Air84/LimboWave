"""Voice configuration and credentials, independent of chat/model configuration."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from limbowave.application.services.credential_service import CredentialService
from limbowave.domain.voice import VoiceEndpoint, VoiceError, VoiceSettings


class VoiceSettingsService:
    def __init__(self, path: Path, credentials: CredentialService | None) -> None:
        self.path = path
        self.credentials = credentials

    def load(self) -> VoiceSettings:
        try:
            return VoiceSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return VoiceSettings()

    def has_secret(self, ref: str | None) -> bool:
        return bool(ref and self.credentials and ref in self.credentials.list_refs())

    def save(
        self,
        settings: VoiceSettings,
        *,
        transcription_secret: str = "",
        speech_secret: str = "",
        clear_transcription: bool = False,
        clear_speech: bool = False,
    ) -> VoiceSettings:
        previous = self.load()
        created: list[str] = []
        retired: list[str] = []
        data = settings.model_dump()
        try:
            for name, secret, clear in (
                ("transcription", transcription_secret.strip(), clear_transcription),
                ("speech", speech_secret.strip(), clear_speech),
            ):
                old = getattr(previous, name)
                endpoint = getattr(settings, name)
                same_address = (old.base_url, old.provider) == (
                    endpoint.base_url,
                    endpoint.provider,
                )
                # Caller-supplied refs are never trusted, and a changed destination never
                # inherits a credential intended for another service.
                ref = old.credential_ref if same_address and not clear else None
                if secret and not clear:
                    if self.credentials is None:
                        raise VoiceError("请先解锁资料库再保存语音密钥")
                    ref = "voice." + name + "." + uuid4().hex
                    self.credentials.store_secret(ref, secret)
                    created.append(ref)
                if endpoint.enabled and endpoint.provider == "api" and not self.has_secret(ref):
                    raise VoiceError("启用外部 API 前请填写密钥；更换地址后需重新填写")
                data[name]["credential_ref"] = ref
                if old.credential_ref and old.credential_ref != ref:
                    retired.append(old.credential_ref)
            result = VoiceSettings.model_validate(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(result.model_dump(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except Exception as exc:
            if self.credentials:
                for ref in created:
                    with suppress(Exception):
                        self.credentials.delete_secret(ref)
            if isinstance(exc, VoiceError):
                raise
            if isinstance(exc, ValidationError):
                raise VoiceError("语音设置无效，请检查地址、模型和语速") from None
            raise VoiceError("语音设置未保存，请检查资料库是否解锁及目录是否可写") from None
        # A cleanup failure must not roll back credentials referenced by committed settings.
        if self.credentials:
            for ref in retired:
                if ref.startswith("voice."):
                    with suppress(Exception):
                        self.credentials.delete_secret(ref)
        return result

    def secret_for(self, ref: str | None) -> str | None:
        try:
            return self.credentials.resolve(ref) if self.credentials else None
        except Exception:
            raise VoiceError("无法读取语音密钥，请重新解锁资料库") from None

    def secret_for_endpoint(self, endpoint: VoiceEndpoint) -> str | None:
        secret = self.secret_for(endpoint.credential_ref)
        if endpoint.provider == "api" and not secret:
            raise VoiceError("语音密钥不可用，请在设置中重新保存；未向服务发送内容")
        return secret
