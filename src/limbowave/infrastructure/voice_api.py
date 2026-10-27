"""Bounded, cancellable OpenAI-compatible audio transport; never logs payloads or keys."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from limbowave.domain.voice import (
    MAX_AUDIO_BYTES,
    MAX_SPEECH_CHARACTERS,
    VoiceEndpoint,
    VoiceError,
)


class CompatibleVoiceProvider:
    """The same contract serves explicitly configured remote and loopback servers.

    No discovery, fallback, redirects, proxy-environment inheritance or disk audio cache.
    A future native engine implements VoiceProvider rather than extending this adapter.
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _request(
        self,
        endpoint: VoiceEndpoint,
        secret: str | None,
        path: str,
        limit: int,
        **kwargs: Any,
    ) -> bytes:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        try:
            async with (
                asyncio.timeout(90),
                httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(60, connect=10, write=30, pool=10),
                ) as client,
                client.stream(
                    "POST",
                    endpoint.base_url + path,
                    headers=headers,
                    **kwargs,
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise VoiceError("语音服务拒绝认证，请检查 API 密钥及权限")
                if response.status_code == 429:
                    raise VoiceError("语音服务繁忙或额度不足，请稍后重试")
                if not 200 <= response.status_code < 300:
                    raise VoiceError("语音服务请求失败，请检查地址、模型及服务状态")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > limit:
                        raise VoiceError("语音服务返回内容过大，已停止接收")
                    body.extend(chunk)
                return bytes(body)
        except (httpx.TimeoutException, TimeoutError):
            raise VoiceError("语音服务超时；文字聊天不受影响，请稍后重试") from None
        except (httpx.HTTPError, ValueError):
            raise VoiceError("无法连接语音服务，请检查地址、网络与证书") from None

    async def transcribe(self, audio: bytes, endpoint: VoiceEndpoint, secret: str | None) -> str:
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise VoiceError("录音为空或过大，请重新录制")
        body = await self._request(
            endpoint,
            secret,
            "/audio/transcriptions",
            64 * 1024,
            files={"file": ("recording.wav", audio, "audio/wav")},
            data={"model": endpoint.model, "response_format": "json"},
        )
        try:
            data = json.loads(body)
        except ValueError:
            raise VoiceError("转写服务返回了无效结果，请检查接口兼容性") from None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise VoiceError("没有识别到文字，请重新按住说话")
        return text.strip()

    async def synthesize(
        self,
        text: str,
        endpoint: VoiceEndpoint,
        secret: str | None,
        *,
        voice: str,
        speed: float,
    ) -> bytes:
        if not text.strip() or len(text) > MAX_SPEECH_CHARACTERS:
            raise VoiceError(f"朗读内容需为 1–{MAX_SPEECH_CHARACTERS} 字，请选择较短的回复")
        audio = await self._request(
            endpoint,
            secret,
            "/audio/speech",
            MAX_AUDIO_BYTES,
            json={
                "model": endpoint.model,
                "input": text,
                "voice": voice,
                "speed": speed,
                "response_format": "wav",
            },
        )
        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise VoiceError("朗读服务未返回 WAV 音频，请检查接口兼容性")
        return audio
