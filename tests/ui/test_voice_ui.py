from __future__ import annotations

import asyncio
import time

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal

from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.voice_service import VoiceSettingsService
from limbowave.domain.voice import VoiceEndpoint, VoiceSettings
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.ui.chat_view import ChatView, ComposerDraft
from limbowave.ui.voice_controller import VoiceController
from limbowave.ui.voice_controls import HoldToTalkButton
from limbowave.ui.voice_settings import VoiceSettingsPage


class FakeAudio(QObject):
    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.recording = False
        self.played = None
        self.cancel_count = 0

    def start_recording(self):
        self.recording = True

    def finish_recording(self):
        self.recording = False
        return b"captured audio"

    def cancel(self):
        self.cancel_count += 1
        self.recording = False
        self.played = None

    def play(self, audio):
        self.played = audio


class FakeProvider:
    def __init__(self):
        self.inputs = []
        self.reads = []
        self.gate = None
        self.ignore_cancel = False
        self.fail = False

    async def transcribe(self, audio, endpoint, secret):
        self.inputs.append(audio)
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.gate.wait()
        if self.fail:
            raise RuntimeError("upstream secret details")
        return "转写结果"

    async def synthesize(self, text, endpoint, secret, **kwargs):
        self.reads.append(text)
        if self.gate is not None:
            await self.gate.wait()
        return b"speech audio"


@pytest.fixture
def service(tmp_path, vault_key):
    service = VoiceSettingsService(
        tmp_path / "voice.json",
        CredentialService(SecretStore(vault_key, tmp_path / "secrets.json")),
    )
    local = VoiceEndpoint(
        enabled=True, provider="local", base_url="http://127.0.0.1:8000/v1", model="local"
    )
    service.save(VoiceSettings(transcription=local, speech=local))
    return service


@pytest.fixture
def voice(qtbot, service):
    chat = ChatView()
    qtbot.addWidget(chat)
    chat.resize(900, 650)
    chat.show()
    qtbot.waitUntil(chat.isVisible)
    provider = FakeProvider()
    allowed = [True]
    controller = VoiceController(
        chat, service, allowed=lambda: allowed[0], provider=provider, audio_factory=FakeAudio
    )
    yield chat, controller, provider, allowed
    controller.shutdown()


async def settle():
    for _ in range(4):
        await asyncio.sleep(0)


async def test_hold_transcribe_appends_draft_never_sends(voice):
    chat, controller, provider, _ = voice
    sends = []
    chat.message_submitted.connect(sends.append)
    chat._input.setPlainText("原有草稿")
    controller.start_recording()
    assert controller.state == "recording"
    assert not provider.inputs
    controller.finish_recording()
    assert controller.state == "transcribing"
    chat._input.insertPlainText("继续输入")
    current = chat._input.toPlainText()
    await settle()
    assert chat._input.toPlainText() == current + "\n转写结果"
    assert not sends
    assert controller.state == "idle"
    assert "校对" in chat.voice_controls.status.text()


@pytest.mark.parametrize(
    "boundary", ["cancel", "hide", "clear", "draft", "edit", "loading", "lock"]
)
async def test_late_results_never_cross_context_boundary(voice, boundary):
    chat, controller, provider, allowed = voice
    provider.gate = asyncio.Event()
    provider.ignore_cancel = True
    controller.start_recording()
    controller.finish_recording()
    await settle()
    if boundary == "cancel":
        controller.cancel()
    elif boundary == "hide":
        chat.hide()
    elif boundary == "clear":
        chat.clear_transcript()
    elif boundary == "draft":
        chat.set_draft(ComposerDraft(text="新会话"))
    elif boundary == "edit":
        chat.begin_edit("old-id", "编辑消息")
    elif boundary == "loading":
        chat.set_history_loading(True)
    else:
        allowed[0] = False
        controller._tick()
    before = chat._input.toPlainText()
    await settle()
    provider.gate.set()
    await settle()
    assert chat._input.toPlainText() == before
    assert controller.state == "idle"


async def test_transcription_failure_keeps_text_and_allows_retry(voice):
    chat, controller, provider, _ = voice
    chat._input.setPlainText("不能丢")
    provider.fail = True
    controller.start_recording()
    controller.finish_recording()
    await settle()
    assert chat._input.toPlainText() == "不能丢"
    assert "secret" not in chat.voice_controls.status.text()
    assert chat.voice_controls.record.isEnabled()
    provider.fail = False
    controller.start_recording()
    controller.finish_recording()
    await settle()
    assert chat._input.toPlainText().endswith("转写结果")


async def test_read_completed_body_only_and_interrupt(voice, qtbot):
    chat, controller, provider, _ = voice
    chat.begin_assistant()
    chat.append_assistant_delta("完整回复")
    chat._stream_row.set_thinking("不要发送的推理")
    chat.end_assistant("完整回复")
    chat.begin_assistant()
    chat.append_assistant_delta("未完成回复")
    controller.read_reply()
    await settle()
    assert provider.reads == ["完整回复"]
    assert controller.state == "playing"
    assert controller._audio.played == b"speech audio"
    qtbot.keyClick(chat._input, Qt.Key.Key_Escape)
    assert controller.state == "idle"
    assert controller._audio.played is None


async def test_stop_while_synthesizing_prevents_playback(voice):
    chat, controller, provider, _ = voice
    chat.begin_assistant()
    chat.end_assistant("朗读这段")
    provider.gate = asyncio.Event()
    controller.read_reply()
    await settle()
    assert controller.state == "synthesizing"
    controller.cancel()
    provider.gate.set()
    await settle()
    assert controller._audio is None
    assert controller.state == "idle"


async def test_sixty_second_limit_stops_capture(voice):
    chat, controller, _, _ = voice
    chat.voice_controls.record._start()
    controller._started = time.monotonic() - 61
    controller._tick()
    await settle()
    assert not controller._audio.recording
    assert controller.state == "idle"
    assert chat._input.toPlainText() == "转写结果"


def test_disabled_does_not_construct_audio(voice):
    _, controller, _, _ = voice
    controller.configure(VoiceSettings())
    controller.start_recording()
    controller.read_reply()
    assert controller._audio is None
    assert controller.controls.isHidden()


def test_keyboard_hold_and_release_outside(qtbot):
    button = HoldToTalkButton()
    qtbot.addWidget(button)
    button.show()
    events = []
    button.hold_started.connect(lambda: events.append("start"))
    button.hold_finished.connect(lambda: events.append("finish"))
    qtbot.keyPress(button, Qt.Key.Key_Space)
    qtbot.keyRelease(button, Qt.Key.Key_Space)
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-10, -10))
    assert events == ["start", "finish", "start", "finish"]


def test_settings_secret_write_only_and_save_feedback(qtbot, service):
    page = VoiceSettingsPage(service)
    qtbot.addWidget(page)
    page.transcription.provider.setCurrentIndex(0)
    page.transcription.url.setText("https://voice.example/v1")
    page.transcription.key.setText("private-key")
    page.save()
    assert "已保存" in page.feedback.text()
    assert page.transcription.key.text() == ""
    assert "已加密保存" in page.transcription.key.placeholderText()
    saved = service.load()
    assert service.secret_for(saved.transcription.credential_ref) == "private-key"
    page.save()
    assert service.load() == saved


def test_settings_invalid_url_never_echoes_key(qtbot, service):
    page = VoiceSettingsPage(service)
    qtbot.addWidget(page)
    previous = service.load()
    page.transcription.url.setText("https://host/v1?key=secret-value")
    page.save()
    assert "secret-value" not in page.feedback.text()
    assert service.load() == previous


async def test_actual_button_hold_release_runs_full_flow(voice, qtbot):
    chat, controller, provider, _ = voice
    qtbot.mousePress(chat.voice_controls.record, Qt.MouseButton.LeftButton)
    assert controller.state == "recording"
    assert chat.voice_controls.record._held
    qtbot.mouseRelease(chat.voice_controls.record, Qt.MouseButton.LeftButton)
    assert controller.state == "transcribing"
    await settle()
    assert provider.inputs == [b"captured audio"]
    assert chat._input.toPlainText() == "转写结果"


async def test_keyboard_hold_runs_full_flow(voice, qtbot):
    chat, controller, _, _ = voice
    qtbot.keyPress(chat.voice_controls.record, Qt.Key.Key_Space)
    assert controller.state == "recording"
    qtbot.keyRelease(chat.voice_controls.record, Qt.Key.Key_Space)
    await settle()
    assert controller.state == "idle"
    assert chat._input.toPlainText() == "转写结果"


@pytest.mark.parametrize("action", ["focus", "shutdown", "settings"])
def test_recording_stops_on_sensitive_boundary(voice, qtbot, action):
    chat, controller, _, _ = voice
    qtbot.mousePress(chat.voice_controls.record, Qt.MouseButton.LeftButton)
    assert controller._audio.recording
    if action == "focus":
        chat._input.setFocus()
        qtbot.wait(5)
    elif action == "shutdown":
        controller.shutdown()
    else:
        controller.configure(VoiceSettings())
    assert not controller._audio.recording
    assert controller.state == "idle"


def test_voice_tab_is_lazy_and_relays_save(qtbot, tmp_path, service):
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.settings_service import SettingsService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
    from limbowave.ui.settings_panel import SettingsPage

    page = SettingsPage(
        None,
        SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json"))),
        service.credentials,
        voice_service=service,
    )
    qtbot.addWidget(page)
    assert page.findChild(VoiceSettingsPage) is None
    page.select_tab("语音")
    voice_page = page.findChild(VoiceSettingsPage)
    assert voice_page is not None
    with qtbot.waitSignal(page.voice_changed):
        voice_page.save()


def test_audio_capture_wav_and_cleanup_without_microphone(qtbot, monkeypatch):
    import io
    import wave

    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtMultimedia import QAudioFormat

    from limbowave.domain.voice import VoiceError
    from limbowave.ui.voice_audio import MemoryVoiceAudio

    class Source(QObject):
        def stop(self):
            pass

    audio = MemoryVoiceAudio()
    audio._source = Source()
    audio._capture = QBuffer()
    audio._capture.setData(b"\0\0" * 16000)
    audio._capture.open(QIODevice.OpenModeFlag.ReadOnly)
    audio._format = QAudioFormat()
    audio._format.setChannelCount(1)
    audio._format.setSampleRate(16000)
    audio._format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    wav = audio.finish_recording()
    with wave.open(io.BytesIO(wav)) as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 16000
        assert stream.getnframes() == 16000
    assert audio._capture is None
    assert audio._pcm == bytearray()
    with pytest.raises(VoiceError):
        audio.finish_recording()
    audio.cancel()


def test_audio_capture_limit_clears_buffer(qtbot, monkeypatch):
    from PySide6.QtCore import QBuffer, QIODevice

    from limbowave.ui import voice_audio

    audio = voice_audio.MemoryVoiceAudio()
    capture = QBuffer()
    capture.setData(b"too many bytes")
    capture.open(QIODevice.OpenModeFlag.ReadOnly)
    audio._capture = capture
    monkeypatch.setattr(voice_audio, "MAX_AUDIO_BYTES", 4)
    with qtbot.waitSignal(audio.failed):
        audio._read_capture()
    assert audio._capture is None
    assert not audio._pcm


async def test_disallowed_before_request_does_not_upload(voice):
    _, controller, provider, allowed = voice
    controller.start_recording()
    controller.finish_recording()
    allowed[0] = False
    await settle()
    assert provider.inputs == []
    assert controller.state == "idle"
