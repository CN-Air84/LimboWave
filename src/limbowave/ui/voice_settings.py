"""Explicit voice opt-in and write-only credential editing."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.voice_service import VoiceSettingsService
from limbowave.domain.voice import VoiceEndpoint, VoiceError, VoiceSettings
from limbowave.ui import theme


class _EndpointForm(QGroupBox):
    def __init__(self, title: str, model_hint: str, parent: QWidget) -> None:
        super().__init__(title, parent)
        form = QFormLayout(self)
        form.setSpacing(10)
        self.enabled = QCheckBox("启用（默认关闭）")
        form.addRow(self.enabled)
        self.provider = QComboBox()
        self.provider.addItem("受控 API · HTTPS", "api")
        self.provider.addItem("本地服务 · 兼容 API", "local")
        form.addRow("服务方式", self.provider)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://你的服务地址/v1")
        self.url.setAccessibleName(title + " API 基础地址")
        form.addRow("API 基础地址", self.url)
        self.model = QLineEdit()
        self.model.setPlaceholderText(model_hint)
        self.model.setAccessibleName(title + "模型 ID")
        form.addRow("模型 ID", self.model)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setAccessibleName(title + " API 密钥")
        form.addRow("API 密钥", self.key)
        self.clear_key = QCheckBox("删除已保存的密钥（外部 API 需先关闭）")
        form.addRow(self.clear_key)
        self.provider.currentIndexChanged.connect(self._provider_changed)

    def _provider_changed(self) -> None:
        local = self.provider.currentData() == "local"
        self.url.setPlaceholderText(
            "http://127.0.0.1:8000/v1" if local else "https://你的服务地址/v1"
        )
        # Never carry an unsaved key across destinations/modes.
        self.key.clear()

    def load(self, endpoint: VoiceEndpoint, has_key: bool) -> None:
        self.enabled.setChecked(endpoint.enabled)
        self.provider.setCurrentIndex(self.provider.findData(endpoint.provider))
        self.url.setText(endpoint.base_url)
        self.model.setText(endpoint.model)
        self.key.clear()
        self.key.setPlaceholderText(
            "已加密保存；留空保留，不回显" if has_key else "填入密钥；本机无鉴权服务可留空"
        )
        self.clear_key.setChecked(False)

    def value(self) -> VoiceEndpoint:
        return VoiceEndpoint(
            enabled=self.enabled.isChecked(),
            provider=self.provider.currentData(),
            base_url=self.url.text(),
            model=self.model.text(),
        )


class VoiceSettingsPage(QWidget):
    changed = Signal(object)

    def __init__(self, service: VoiceSettingsService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        title = QLabel("语音 · 按需在场")
        title.setStyleSheet(f"font-size: {theme.FS_TITLE}px; font-weight: 600;")
        root.addWidget(title)
        hint = QLabel(
            "按住说话，松开转写；文字回到输入框，校对后由你发送。\n"
            "朗读由你主动开启，随时停止。不会常开麦克风，也不会自动朗读。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        privacy = QLabel(
            "启用即允许：录音发送到下方转写服务；点击朗读时，回复正文发送到朗读服务。\n"
            "LimboWave 不将原始录音或朗读音频写入磁盘；提供方的留存规则请自行确认。\n"
            "密钥仅加密保存在资料库，不交给聊天模型或浏览器。声音由 AI 生成。"
        )
        privacy.setWordWrap(True)
        root.addWidget(privacy)
        self.transcription = _EndpointForm("语音转写 · STT", "如 whisper-1，或本地模型 ID", self)
        self.speech = _EndpointForm("回复朗读 · TTS", "如 tts-1，或本地模型 ID", self)
        root.addWidget(self.transcription)
        root.addWidget(self.speech)
        options = QFormLayout()
        self.voice = QLineEdit()
        self.voice.setAccessibleName("朗读音色 ID")
        options.addRow("音色 ID", self.voice)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2.0)
        self.speed.setSingleStep(0.1)
        self.speed.setSuffix(" ×")
        self.speed.setAccessibleName("朗读语速")
        options.addRow("语速", self.speed)
        root.addLayout(options)
        note = QLabel(
            "基础地址填到 API 前缀（通常为 /v1）；程序自动追加\n"
            "/audio/transcriptions 或 /audio/speech。\n"
            "更换地址后请重新填写密钥，旧密钥不会发往新地址。本地模式仅连接本机，绝不回退到云端。\n"
            "已预留独立语音引擎接口；当前本地模式需要自行启动兼容服务，尚不内置下载或运行模型。"
        )
        note.setWordWrap(True)
        root.addWidget(note)
        footer = QHBoxLayout()
        self.save_button = QPushButton("保存语音设置")
        self.save_button.setProperty("accent", True)
        self.save_button.clicked.connect(self.save)
        footer.addWidget(self.save_button)
        footer.addStretch(1)
        root.addLayout(footer)
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        root.addStretch(1)
        self.reload()

    def reload(self) -> None:
        value = self._service.load()
        for form, endpoint in (
            (self.transcription, value.transcription),
            (self.speech, value.speech),
        ):
            form.load(endpoint, self._service.has_secret(endpoint.credential_ref))
        self.voice.setText(value.voice)
        self.speed.setValue(value.speed)

    def save(self) -> None:
        try:
            value = VoiceSettings(
                transcription=self.transcription.value(),
                speech=self.speech.value(),
                voice=self.voice.text().strip(),
                speed=self.speed.value(),
            )
            saved = self._service.save(
                value,
                transcription_secret=self.transcription.key.text(),
                speech_secret=self.speech.key.text(),
                clear_transcription=self.transcription.clear_key.isChecked(),
                clear_speech=self.speech.clear_key.isChecked(),
            )
        except (ValueError, VoiceError) as exc:
            message = (
                str(exc)
                if isinstance(exc, VoiceError)
                else (
                    "请检查设置：启用项需填写地址和模型；外部地址须为 HTTPS 且不含密钥/参数；"
                    "本地模式仅支持回环地址；音色不能为空。"
                )
            )
            self.feedback.setText(message)
            return
        self.reload()
        self.feedback.setText("已保存。返回聊天后即可使用；转写不会自动发送。")
        self.changed.emit(saved)

    def hideEvent(self, event: QHideEvent) -> None:
        self.transcription.key.clear()
        self.speech.key.clear()
        super().hideEvent(event)
