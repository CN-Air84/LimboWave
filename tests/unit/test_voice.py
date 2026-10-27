from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.voice_service import VoiceSettingsService
from limbowave.domain.voice import VoiceEndpoint, VoiceError, VoiceSettings
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.voice_api import CompatibleVoiceProvider


def endpoint(**changes):
    return VoiceEndpoint(
        enabled=True, base_url="https://voice.example/v1", model="whisper-1", **changes
    )


@pytest.fixture
def service(tmp_path, vault_key):
    return VoiceSettingsService(
        tmp_path / "voice.json",
        CredentialService(SecretStore(vault_key, tmp_path / "secrets.json")),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "https://user:secret@host/v1",
        "https://host/v1?key=secret",
        "https://host/#secret",
        "file:///tmp/test",
        "https://host\\evil",
        "https://host:invalid",
        "https://host/\npath",
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        VoiceEndpoint(base_url=url)


def test_default_disabled_and_corrupt_config_fails_closed(service):
    assert not service.load().transcription.enabled
    service.path.write_text('{"transcription":{"enabled":true}}')
    assert not service.load().transcription.enabled


def test_local_is_loopback_only_and_needs_no_key(service):
    local = VoiceEndpoint(
        enabled=True, provider="local", base_url="http://127.0.0.1:8000/v1", model="local-whisper"
    )
    assert service.save(VoiceSettings(transcription=local)).transcription == local
    with pytest.raises(ValueError):
        VoiceEndpoint(provider="local", base_url="https://external.example")


def test_keys_encrypted_preserved_and_explicitly_deleted(service):
    settings = service.save(
        VoiceSettings(transcription=endpoint()), transcription_secret="test-secret"
    )
    assert service.secret_for(settings.transcription.credential_ref) == "test-secret"
    for path in service.path.parent.glob("*.json"):
        assert "test-secret" not in path.read_text()
    assert (
        service.save(settings).transcription.credential_ref == settings.transcription.credential_ref
    )
    disabled = settings.model_copy(
        update={"transcription": endpoint().model_copy(update={"enabled": False})}
    )
    result = service.save(disabled, clear_transcription=True)
    assert result.transcription.credential_ref is None
    assert not service.has_secret(settings.transcription.credential_ref)


def test_changed_destination_never_inherits_key(service):
    saved = service.save(VoiceSettings(transcription=endpoint()), transcription_secret="secret-A")
    changed = saved.model_copy(
        update={
            "transcription": endpoint().model_copy(update={"base_url": "https://other.example/v1"})
        }
    )
    with pytest.raises(VoiceError, match="重新填写"):
        service.save(changed)
    assert service.load() == saved
    updated = service.save(changed, transcription_secret="secret-B")
    assert service.secret_for(updated.transcription.credential_ref) == "secret-B"
    assert not service.has_secret(saved.transcription.credential_ref)


def test_failure_preserves_old_settings_and_removes_staged_keys(service, monkeypatch):
    saved = service.save(VoiceSettings(transcription=endpoint()), transcription_secret="old-secret")

    def fail(*args):
        raise OSError("private details")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(VoiceError) as error:
        service.save(saved, transcription_secret="new-secret")
    assert "private details" not in str(error.value)
    assert service.load() == saved
    assert service.credentials.list_refs() == [saved.transcription.credential_ref]


async def test_transcription_request_and_result():
    async def handle(request):
        assert str(request.url) == "https://voice.example/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer private"
        body = await request.aread()
        assert b'filename="recording.wav"' in body
        assert b"whisper-1" in body
        return httpx.Response(200, json={"text": "  校对后发送  "})

    provider = CompatibleVoiceProvider(transport=httpx.MockTransport(handle))
    assert await provider.transcribe(b"RIFF-data", endpoint(), "private") == "校对后发送"


async def test_speech_request_and_local_no_authorization():
    async def handle(request):
        assert "authorization" not in request.headers
        assert request.url.path == "/v1/audio/speech"
        assert json.loads(request.content) == {
            "model": "whisper-1",
            "input": "你好",
            "voice": "alloy",
            "speed": 1.0,
            "response_format": "wav",
        }
        return httpx.Response(200, content=b"RIFF\x00\x00\x00\x00WAVEdata")

    provider = CompatibleVoiceProvider(transport=httpx.MockTransport(handle))
    audio = await provider.synthesize("你好", endpoint(), None, voice="alloy", speed=1.0)
    assert audio.startswith(b"RIFF")


@pytest.mark.parametrize("status", [301, 401, 429, 500])
async def test_provider_errors_never_expose_body_or_key(status):
    provider = CompatibleVoiceProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                text="secret echoed from upstream",
                headers={"Location": "https://evil.test"},
            )
        )
    )
    with pytest.raises(VoiceError) as error:
        await provider.transcribe(b"audio", endpoint(), "private-key")
    assert "secret" not in str(error.value)
    assert "private-key" not in str(error.value)


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": 123}, {}, ["text"]])
async def test_invalid_transcription(payload):
    provider = CompatibleVoiceProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    with pytest.raises(VoiceError):
        await provider.transcribe(b"audio", endpoint(), None)


async def test_reject_oversize_and_non_audio_response():
    provider = CompatibleVoiceProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not audio"))
    )
    with pytest.raises(VoiceError):
        await provider.transcribe(b"", endpoint(), None)
    with pytest.raises(VoiceError):
        await provider.synthesize("x" * 4001, endpoint(), None, voice="alloy", speed=1)
    with pytest.raises(VoiceError):
        await provider.synthesize("hello", endpoint(), None, voice="alloy", speed=1)


async def test_cancellation_propagates_without_wrapping():
    import asyncio

    entered = asyncio.Event()

    async def handle(request):
        entered.set()
        await asyncio.Event().wait()

    provider = CompatibleVoiceProvider(transport=httpx.MockTransport(handle))
    task = asyncio.create_task(provider.transcribe(b"audio", endpoint(), "private"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_response_size_limit_is_enforced():
    provider = CompatibleVoiceProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (64 * 1024 + 1))
        )
    )
    with pytest.raises(VoiceError, match="过大"):
        await provider.transcribe(b"audio", endpoint(), None)


async def test_timeout_is_sanitized():
    def handle(request):
        raise httpx.ReadTimeout("secret in upstream timeout", request=request)

    provider = CompatibleVoiceProvider(transport=httpx.MockTransport(handle))
    with pytest.raises(VoiceError, match="超时") as error:
        await provider.transcribe(b"audio", endpoint(), None)
    assert "secret" not in str(error.value)


def test_chat_credentials_cannot_be_referenced():
    with pytest.raises(ValueError):
        VoiceEndpoint(credential_ref="chat-endpoint-secret")


def test_missing_external_key_blocks_upload(service):
    with pytest.raises(VoiceError, match="未向服务发送"):
        service.secret_for_endpoint(endpoint())
