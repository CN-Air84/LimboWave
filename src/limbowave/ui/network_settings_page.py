"""Explicit provider selection, truthful status, and masked write-only API keys."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.network_settings_service import NetworkSettingsService
from limbowave.domain.network import NetworkSettings, SearchProvider, WebAccessError
from limbowave.ui import theme


class NetworkSettingsPage(QWidget):
    changed = Signal()

    def __init__(self, service: NetworkSettingsService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        title = QLabel("联网搜索与只读网页")
        title.setStyleSheet(f"font-size: {theme.FS_TITLE}px; font-weight: 600;")
        root.addWidget(title)
        form = QFormLayout()
        form.setSpacing(12)
        self.provider = QComboBox()
        for label, value in (
            ("关闭搜索（默认）", SearchProvider.DISABLED),
            ("Google · 经 SerpAPI（第三方）", SearchProvider.SERPAPI_GOOGLE),
            ("Bing · 经 SerpAPI（第三方）", SearchProvider.SERPAPI_BING),
            ("Microsoft Azure · Bing Web Search（旧版）", SearchProvider.AZURE_BING),
            ("Tavily · 搜索 API", SearchProvider.TAVILY),
            ("自定义 · JSON 搜索 API", SearchProvider.CUSTOM_JSON),
        ):
            self.provider.addItem(label, value.value)
        form.addRow("搜索服务", self.provider)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setMaxLength(1024)
        self.key.setPlaceholderText("填写新的 API Key；留空保持当前服务的已存密钥")
        form.addRow("新的 API Key", self.key)
        self.custom_endpoint = QLineEdit()
        self.custom_endpoint.setMaxLength(2048)
        self.custom_endpoint.setPlaceholderText("https://search.example.com/api/search")
        form.addRow("自定义 API 地址", self.custom_endpoint)
        self.max_results = QSpinBox()
        self.max_results.setRange(1, 10)
        form.addRow("最多返回条数", self.max_results)
        root.addLayout(form)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        help_text = QLabel(
            "SerpAPI 与 Tavily 使用各自 Key。Azure 旧版选项使用微软订阅密钥，通过 "
            "Ocp-Apim-Subscription-Key 调用 Bing Web Search v7；该服务已退役，仅供仍可用的"
            "既有资源兼容，不保证新账户可开通。\n"
            "自定义 API 必须是公网 HTTPS GET 地址；应用追加 q 与 count 参数，并用 "
            "Authorization: Bearer <Key> 认证。返回 JSON 须含 results 数组，每项支持 "
            "title/url/snippet/published_date；不允许自定义任意请求头或请求体。\n"
            "搜索词会发送给所选服务，每次由联网权限确认；不要提交秘密或私人聊天全文。"
            "切换服务需重新填写密钥，保存后立即生效。密钥只进加密资料库，不进普通配置或日志。\n"
            "网页读取不需要搜索 Key，但仍需联网授权；仅抓取公网文本，不执行 JavaScript、"
            "不发送登录 Cookie、不读取内网。跨来源重定向需另行授权。\n"
            "回答会区分搜索摘要与网页正文，并提供来源 URL / 查阅时间；"
            "未配置、权限拒绝或抓取失败时如实说明，不伪称已阅。"
        )
        help_text.setWordWrap(True)
        root.addWidget(help_text)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("保存联网设置")
        self.save_button.setProperty("accent", True)
        self.save_button.clicked.connect(self.save)
        self.clear_button = QPushButton("清除密钥并关闭搜索")
        self.clear_button.clicked.connect(self.clear_key)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        root.addStretch(1)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.reload()

    def reload(self) -> None:
        value = self._service.load()
        self.provider.setCurrentIndex(self.provider.findData(value.search_provider.value))
        self.max_results.setValue(value.max_results)
        self.custom_endpoint.setText(value.custom_search_endpoint or "")
        self.custom_endpoint.setEnabled(value.search_provider is SearchProvider.CUSTOM_JSON)
        self.key.clear()
        self.key.setEnabled(self._service.unlocked)
        self.clear_button.setEnabled(self._service.unlocked and bool(value.credential_ref))
        if not self._service.unlocked:
            message = "资料库未解锁，不能保存或使用搜索密钥。"
        elif value.search_provider is SearchProvider.DISABLED:
            message = "搜索已关闭；获授权的只读网页抓取仍可用。"
        elif self._service.has_secret(value.credential_ref):
            message = "已配置搜索凭据（未进行在线验证）；不会显示已有密钥。"
        else:
            message = "搜索凭据缺失；请填写对应服务的 Key。"
        self.status.setText(message)

    def _provider_changed(self) -> None:
        self.key.clear()
        is_custom = self.provider.currentData() == SearchProvider.CUSTOM_JSON.value
        self.custom_endpoint.setEnabled(is_custom)
        if not is_custom:
            self.custom_endpoint.clear()
        self.feedback.setText("服务选择尚未保存；更换服务需重新填写对应 Key。")

    def save(self) -> None:
        try:
            self._service.save(
                NetworkSettings(
                    search_provider=SearchProvider(self.provider.currentData()),
                    max_results=self.max_results.value(),
                    custom_search_endpoint=(
                        self.custom_endpoint.text().strip()
                        if self.provider.currentData() == SearchProvider.CUSTOM_JSON.value
                        else None
                    ),
                ),
                secret=self.key.text(),
            )
        except (WebAccessError, ValueError) as exc:
            self.feedback.setText(str(exc))
            self.key.clear()
            return
        self.reload()
        self.feedback.setText("联网设置已保存，立即生效；未执行在线搜索测试。")
        self.changed.emit()

    def clear_key(self) -> None:
        try:
            self._service.save(self._service.load(), clear_key=True)
        except WebAccessError as exc:
            self.feedback.setText(str(exc))
            return
        self.reload()
        self.feedback.setText("搜索密钥已清除，搜索已关闭。")
        self.changed.emit()

    def hideEvent(self, event: QHideEvent) -> None:
        self.key.clear()
        super().hideEvent(event)
