"""Lazy Qt multimedia adapter. Raw capture and playback stay in memory only."""

from __future__ import annotations

import io
import wave

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl, Signal
from PySide6.QtMultimedia import (
    QAudio,
    QAudioFormat,
    QAudioOutput,
    QAudioSource,
    QMediaDevices,
    QMediaPlayer,
)

from limbowave.domain.voice import MAX_AUDIO_BYTES, VoiceError


class MemoryVoiceAudio(QObject):
    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source: QAudioSource | None = None
        self._capture: QIODevice | None = None
        self._pcm = bytearray()
        self._format: QAudioFormat | None = None
        self._player: QMediaPlayer | None = None
        self._output: QAudioOutput | None = None
        self._playback: QBuffer | None = None

    def start_recording(self) -> None:
        self.cancel()
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise VoiceError("没有可用麦克风，请连接设备并允许麦克风访问")
        audio_format = QAudioFormat()
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        supported = False
        for channels in (1, 2):
            audio_format.setChannelCount(channels)
            for rate in (16000, 48000, 44100):
                audio_format.setSampleRate(rate)
                if device.isFormatSupported(audio_format):
                    supported = True
                    break
            if supported:
                break
        if not supported:
            raise VoiceError("麦克风不支持 PCM 录音，请在系统中切换输入设备")
        self._format = audio_format
        source = QAudioSource(device, audio_format, self)
        self._source = source
        source.stateChanged.connect(lambda state: self._capture_state(source, state))
        self._capture = source.start()
        if self._capture is not None:
            self._capture.readyRead.connect(self._read_capture)
        if self._capture is None or source.error().value != QAudio.Error.NoError.value:
            self.cancel()
            raise VoiceError("无法打开麦克风，请检查系统权限及设备占用")

    def _capture_state(self, source: QAudioSource, state: QAudio.State) -> None:
        if (
            self._source is source
            and state.value == QAudio.State.StoppedState.value
            and source.error().value != QAudio.Error.NoError.value
        ):
            self.failed.emit("录音设备中断，请检查麦克风权限和连接")

    def _read_capture(self) -> None:
        if self._capture is None:
            return
        chunk = self._capture.readAll().data()
        if len(self._pcm) + len(chunk) > MAX_AUDIO_BYTES:
            self.cancel()
            self.failed.emit("录音过大，已停止并清除，请缩短录音")
            return
        self._pcm.extend(chunk)

    def finish_recording(self) -> bytes:
        self._read_capture()
        source, self._source = self._source, None
        if source is None or self._capture is None or self._format is None:
            raise VoiceError("没有正在进行的录音")
        source.stop()
        source.deleteLater()
        raw = bytes(self._pcm)
        fmt = self._format
        self._clear_capture()
        if len(raw) < fmt.sampleRate() * fmt.channelCount() * 2 // 4:
            raise VoiceError("录音太短，请按住说完后再松开")
        if len(raw) > MAX_AUDIO_BYTES:
            raise VoiceError("录音过大，已丢弃，请缩短录音")
        stream = io.BytesIO()
        with wave.open(stream, "wb") as output:
            output.setnchannels(fmt.channelCount())
            output.setsampwidth(2)
            output.setframerate(fmt.sampleRate())
            output.writeframes(raw)
        return stream.getvalue()

    def play(self, audio: bytes) -> None:
        self.cancel()
        if QMediaDevices.defaultAudioOutput().isNull():
            raise VoiceError("没有可用扬声器，请检查输出设备")
        self._playback = QBuffer(self)
        self._playback.setData(QByteArray(audio))
        self._playback.open(QIODevice.OpenModeFlag.ReadOnly)
        self._output = QAudioOutput(self)
        self._output.setVolume(0.65)
        player = QMediaPlayer(self)
        self._player = player
        player.setAudioOutput(self._output)
        player.mediaStatusChanged.connect(lambda status: self._playback_state(player, status))
        player.errorOccurred.connect(lambda *_: self._playback_error(player))
        player.setSourceDevice(self._playback, QUrl("memory.wav"))
        if self._player is player:
            player.play()

    def _playback_state(self, player: QMediaPlayer, status: QMediaPlayer.MediaStatus) -> None:
        if self._player is player and status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.cancel()
            self.finished.emit()

    def _playback_error(self, player: QMediaPlayer) -> None:
        if self._player is player:
            self.cancel()
            self.failed.emit("无法播放语音，请检查返回格式和输出设备")

    def _clear_capture(self) -> None:
        # The capture QIODevice belongs to QAudioSource; do not destroy it twice.
        self._capture = None
        self._pcm.clear()
        self._format = None

    def cancel(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            source.stop()
            source.deleteLater()
        self._clear_capture()
        player, self._player = self._player, None
        if player is not None:
            player.stop()
            player.setSource(QUrl())
            player.deleteLater()
        if self._playback is not None:
            self._playback.close()
            self._playback.setData(QByteArray())
            self._playback.deleteLater()
            self._playback = None
        if self._output is not None:
            self._output.deleteLater()
            self._output = None
