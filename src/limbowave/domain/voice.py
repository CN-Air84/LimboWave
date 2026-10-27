"""Opt-in voice settings and the replaceable speech-engine boundary."""

from __future__ import annotations

from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_RECORDING_SECONDS = 60
MAX_AUDIO_BYTES = 24 * 1024 * 1024
MAX_SPEECH_CHARACTERS = 4000


class VoiceError(Exception):
    """A deliberately sanitized, user-facing voice failure."""


class VoiceEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    provider: Literal["api", "local"] = "api"
    base_url: str = ""
    model: str = Field(default="", max_length=200)
    credential_ref: str | None = Field(
        default=None,
        pattern=r"^voice\.(transcription|speech)\.[0-9a-f]{32}$",
    )

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        try:
            url = urlsplit(value)
            _ = url.port
            if (
                url.scheme not in ("https", "http")
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or "?" in value
                or "#" in value
                or "\\" in value
                or any(c.isspace() or ord(c) < 32 for c in value)
            ):
                raise ValueError
            if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError
        except ValueError:
            raise ValueError(
                "地址需为 HTTPS（本机可用 HTTP），且不能含密钥、查询参数或片段"
            ) from None
        return value

    @field_validator("model")
    @classmethod
    def trim_model(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_endpoint(self) -> VoiceEndpoint:
        if (
            self.provider == "local"
            and self.base_url
            and urlsplit(self.base_url).hostname not in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("本地服务模式仅允许 localhost、127.0.0.1 或 ::1")
        if self.enabled and (not self.base_url or not self.model):
            raise ValueError("启用前请填写 API 基础地址和模型 ID")
        return self


class VoiceSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    transcription: VoiceEndpoint = Field(default_factory=VoiceEndpoint)
    speech: VoiceEndpoint = Field(default_factory=VoiceEndpoint)
    voice: str = Field(default="alloy", min_length=1, max_length=100)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class VoiceProvider(Protocol):
    """Native Whisper/system engines can implement this without UI or HTTP dependencies."""

    async def transcribe(
        self, audio: bytes, endpoint: VoiceEndpoint, secret: str | None
    ) -> str: ...

    async def synthesize(
        self,
        text: str,
        endpoint: VoiceEndpoint,
        secret: str | None,
        *,
        voice: str,
        speed: float,
    ) -> bytes: ...
