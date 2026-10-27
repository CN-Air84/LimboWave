"""Cancellable desktop voice state machine. It never submits chat or approves tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from limbowave.application.services.voice_service import VoiceSettingsService
from limbowave.domain.voice import MAX_RECORDING_SECONDS, VoiceError, VoiceProvider, VoiceSettings
from limbowave.infrastructure.voice_api import CompatibleVoiceProvider

if TYPE_CHECKING:
    from limbowave.ui.chat_view import ChatView
    from limbowave.ui.voice_audio import MemoryVoiceAudio


class VoiceController(QObject):
    def __init__(
        self,
        chat: ChatView,
        service: VoiceSettingsService,
        *,
        allowed: Callable[[], bool],
        provider: VoiceProvider | None = None,
        audio_factory: Callable[[QObject], MemoryVoiceAudio] | None = None,
    ) -> None:
        super().__init__(chat)
        self.chat = chat
        self.controls = chat.voice_controls
        self.service = service
        self.settings = service.load()
        self.provider = provider or CompatibleVoiceProvider()
        self._allowed = allowed
        self._audio_factory = audio_factory
        self._audio: MemoryVoiceAudio | None = None
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._closed = False
        self.state = "idle"
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self.controls.record.hold_started.connect(self.start_recording)
        self.controls.record.hold_finished.connect(self.finish_recording)
        self.controls.record.hold_cancelled.connect(self.cancel)
        self.controls.read.clicked.connect(self.read_reply)
        self.controls.stop.clicked.connect(lambda: self.cancel("已停止语音"))
        chat.voice_context_invalidated.connect(self.cancel)
        chat.destroyed.connect(self.shutdown)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            app.aboutToQuit.connect(self.shutdown)
        self.configure(self.settings)

    def configure(self, settings: VoiceSettings) -> None:
        self.cancel()
        self.settings = settings
        self.controls.configure(settings)

    def _eligible(self) -> bool:
        return (
            not self._closed
            and isValid(self.chat)
            and self._allowed()
            and self.chat.isVisible()
            and not self.chat.history_loading
            and self.chat._input.isEnabled()
            and self.chat._composer.isEnabled()
        )

    def _get_audio(self) -> MemoryVoiceAudio:
        if self._audio is None:
            factory = self._audio_factory
            if factory is None:
                from limbowave.ui.voice_audio import MemoryVoiceAudio

                factory = MemoryVoiceAudio
            self._audio = factory(self)
            self._audio.failed.connect(self._failed)
            self._audio.finished.connect(lambda: self.cancel("朗读结束"))
        return self._audio

    def _set_state(self, state: str, text: str = "") -> None:
        self.state = state
        if isValid(self.controls):
            self.controls.show_state(state, text)
        if state == "idle":
            self._timer.stop()
        else:
            self._timer.start()

    def cancel(self, text: str = "", *, release_hold: bool = True) -> None:
        if release_hold and isValid(self.controls):
            self.controls.record.reset_hold()
        self._generation += 1
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        if self._audio is not None:
            self._audio.cancel()
        self._set_state("idle", text)

    def _failed(self, message: str) -> None:
        self.cancel(message)

    def start_recording(self) -> None:
        if not self.settings.transcription.enabled or not self._eligible():
            return
        self.cancel(release_hold=False)
        try:
            self._get_audio().start_recording()
        except Exception as exc:
            self._failed(
                str(exc) if isinstance(exc, VoiceError) else "无法启动录音，请检查音频设备"
            )
            return
        self._started = time.monotonic()
        self._set_state("recording", "正在录音 · 0 秒 / 60 秒 · 松开转写，Esc 取消")

    def finish_recording(self) -> None:
        if self.state != "recording":
            return
        if not self._eligible():
            self.cancel()
            return
        self.controls.record.reset_hold()
        try:
            audio = self._get_audio().finish_recording()
            loop = asyncio.get_running_loop()
        except Exception as exc:
            self._failed(
                str(exc) if isinstance(exc, VoiceError) else "语音运行环境暂不可用，请重试"
            )
            return
        self._set_state("transcribing", "正在转写 · 可随时停止；结果只填入草稿，不会自动发送")
        self._task = loop.create_task(self._transcribe(audio, self._generation))

    async def _transcribe(self, audio: bytes, generation: int) -> None:
        try:
            if generation != self._generation or not self._eligible():
                return
            endpoint = self.settings.transcription
            text = await self.provider.transcribe(
                audio, endpoint, self.service.secret_for_endpoint(endpoint)
            )
            if generation != self._generation or not self._eligible():
                return
            self._task = None
            # Append at the end of the *current* draft, preserving concurrent typing.
            cursor = self.chat._input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(("\n" if self.chat._input.toPlainText() else "") + text)
            self.chat._input.setTextCursor(cursor)
            self.chat._input.setFocus(Qt.FocusReason.OtherFocusReason)
            self._set_state("idle", "转写已填入输入框 · 请校对后手动发送")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if generation == self._generation:
                self._failed(
                    str(exc) if isinstance(exc, VoiceError) else "转写失败；草稿未改动，请重试"
                )
        finally:
            if generation == self._generation:
                self._task = None
                if self.state == "transcribing":
                    self.cancel()

    def read_reply(self) -> None:
        if not self.settings.speech.enabled or not self._eligible():
            return
        self.cancel()
        text = next(
            (
                row.content_text().strip()
                for row in reversed(self.chat._rows)
                if row._role == "assistant" and row._final and row.content_text().strip()
            ),
            "",
        )
        if not text:
            self._set_state("idle", "还没有可朗读的完整回复")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._failed("语音运行环境暂不可用，请重试")
            return
        self._set_state("synthesizing", "正在准备朗读 · AI 生成声音 · 点击停止或按 Esc 取消")
        self._task = loop.create_task(self._synthesize(text, self._generation))

    async def _synthesize(self, text: str, generation: int) -> None:
        try:
            if generation != self._generation or not self._eligible():
                return
            endpoint = self.settings.speech
            audio = await self.provider.synthesize(
                text,
                endpoint,
                self.service.secret_for_endpoint(endpoint),
                voice=self.settings.voice,
                speed=self.settings.speed,
            )
            if generation != self._generation or not self._eligible():
                return
            self._task = None
            self._set_state("playing", "正在朗读 · AI 生成声音 · 点击停止或按 Esc 立即打断")
            self._get_audio().play(audio)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if generation == self._generation:
                self._failed(
                    str(exc) if isinstance(exc, VoiceError) else "朗读失败；文字聊天不受影响"
                )
        finally:
            if generation == self._generation:
                self._task = None
                if self.state == "synthesizing":
                    self.cancel()

    def _tick(self) -> None:
        if not self._eligible():
            self.cancel()
            return
        if self.state == "recording":
            seconds = int(time.monotonic() - self._started)
            if seconds >= MAX_RECORDING_SECONDS:
                self.finish_recording()
            else:
                self.controls.show_state(
                    "recording", f"正在录音 · {seconds} 秒 / 60 秒 · 松开转写，Esc 取消"
                )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self.state != "idle":
            if (
                event.type() == QEvent.Type.KeyPress
                and isinstance(event, QKeyEvent)
                and event.key() == Qt.Key.Key_Escape
            ):
                self.cancel("已停止语音")
                return True
            if self.state == "recording" and event.type() == QEvent.Type.ApplicationDeactivate:
                self.cancel("录音已取消")
        return super().eventFilter(watched, event)

    def shutdown(self, *_args: Any) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
