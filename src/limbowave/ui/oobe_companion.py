"""Approved offline onboarding education. No recording, network, secrets or persistence.

The examples deliberately remain simulated even in real onboarding. Services are
configured separately through the existing encrypted settings pages.
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme


def label(text: str, name: str = "demoMuted") -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def button(text: str, *, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    widget.setProperty("accent" if primary else "flat", True)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAutoDefault(False)
    return widget


class HoldButton(QPushButton):
    started = Signal()
    released_hold = Signal()
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__("按住试试")
        self.setProperty("accent", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("按住鼠标或空格，体验模拟转写")
        self.setToolTip("仅模拟，不会打开麦克风；支持鼠标和空格，Esc 取消。")
        self.held = False

    def start(self) -> None:
        if self.held or not self.isEnabled():
            return
        self.held = True
        self.setDown(True)
        self.started.emit()

    def finish(self, cancel: bool = False) -> None:
        if not self.held:
            return
        self.held = False
        self.setDown(False)
        (self.cancelled if cancel else self.released_hold).emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.start()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.finish()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.start()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.finish(True)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.finish()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.finish(True)
        super().focusOutEvent(event)


class Waveform(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(74)
        self.phase = 0.0
        self.active = False
        self.timer = QTimer(self)
        self.timer.setInterval(45)
        self.timer.timeout.connect(self.tick)
        self.setAccessibleName("模拟声波，不采集麦克风数据")

    def tick(self) -> None:
        self.phase += 0.22
        self.update()

    def set_active(self, active: bool) -> None:
        self.active = active
        if active:
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = 35
        step = min(8, (self.width() - 38) / count)
        offset = (self.width() - (count - 1) * step) / 2
        color = QColor(theme.ACCENT if self.active else theme.TEXT_SECONDARY)
        color.setAlpha(220 if self.active else 100)
        painter.setPen(QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(count):
            envelope = math.sin(math.pi * (i + 1) / (count + 1)) ** 1.2
            modulation = abs(math.sin(i * 0.63 + self.phase))
            height = 4 + envelope * (44 if self.active else 18) * (0.25 + modulation * 0.75)
            x = offset + i * step
            painter.drawLine(
                QPointF(x, self.height() / 2 - height / 2),
                QPointF(x, self.height() / 2 + height / 2),
            )


class VoiceTryout(QFrame):
    """Synthetic states only; deliberately has no production voice dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("tryout")
        self.setMinimumWidth(310)
        self.setMaximumWidth(380)
        self.state = "idle"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("试着说一句", "demoCardTitle"))
        top.addStretch()
        top.addWidget(label("交互示意", "demoBadge"))
        layout.addLayout(top)
        self.subtitle = label("不用打开麦克风，也能先感受一下。")
        layout.addWidget(self.subtitle)
        layout.addSpacing(7)
        self.wave = Waveform()
        layout.addWidget(self.wave)
        self.state_label = label("等你按住按钮", "demoState")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setAccessibleName("模拟语音状态")
        layout.addWidget(self.state_label)
        layout.addSpacing(7)
        self.draft = QPlainTextEdit()
        self.draft.setObjectName("demoDraft")
        self.draft.setAccessibleName("转写校对示例")
        self.draft.setPlaceholderText("松开后，示例文字会出现在这里。")
        self.draft.setFixedHeight(98)
        layout.addWidget(self.draft)
        self.message = label("", "demoMessage")
        self.message.hide()
        layout.addWidget(self.message)
        self.reply = label("好，先让自己歇一会儿。\n想说的时候，再慢慢说。", "demoReply")
        self.reply.hide()
        layout.addWidget(self.reply)
        self.helper = label("转写不是发送。文字出现后，还可以修改。", "demoSmall")
        layout.addWidget(self.helper)
        actions = QHBoxLayout()
        self.hold = HoldButton()
        self.send = button("确认发送")
        self.send.setEnabled(False)
        self.read = button("模拟朗读")
        self.read.hide()
        self.stop = button("停止")
        self.stop.hide()
        actions.addWidget(self.hold)
        actions.addStretch()
        for control in (self.send, self.read, self.stop):
            actions.addWidget(control)
        layout.addLayout(actions)
        layout.addSpacing(2)
        layout.addWidget(label("不录音 · 不联网 · 不播放声音 · 不保存", "demoSmall"))
        self.reset_button = button("重新体验")
        layout.addWidget(self.reset_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.hold.started.connect(self.start_recording)
        self.hold.released_hold.connect(self.finish_recording)
        self.hold.cancelled.connect(self.stop_all)
        self.send.clicked.connect(self.submit)
        self.read.clicked.connect(self.speak)
        self.stop.clicked.connect(self.stop_all)
        self.reset_button.clicked.connect(self.reset)
        self.draft.textChanged.connect(self.sync_send)
        self.transcription = QTimer(self)
        self.transcription.setSingleShot(True)
        self.transcription.setInterval(650)
        self.transcription.timeout.connect(self.transcribed)
        self.playback = QTimer(self)
        self.playback.setSingleShot(True)
        self.playback.setInterval(4200)
        self.playback.timeout.connect(self.stop_all)

    def sync_send(self) -> None:
        self.send.setEnabled(self.state == "review" and bool(self.draft.toPlainText().strip()))

    def start_recording(self) -> None:
        self.transcription.stop()
        self.playback.stop()
        self.state = "recording"
        self.wave.set_active(True)
        self.state_label.setText("正在模拟录音 · 松开试试")
        self.hold.setText("松开转写")
        self.stop.show()
        self.sync_send()

    def finish_recording(self) -> None:
        if self.state != "recording":
            return
        self.state = "transcribing"
        self.wave.set_active(False)
        self.state_label.setText("正在把这句话变成文字…")
        self.hold.setText("按住试试")
        self.hold.setEnabled(False)
        self.transcription.start()

    def transcribed(self) -> None:
        if self.state != "transcribing":
            return
        self.state = "review"
        current = self.draft.toPlainText()
        self.draft.setPlainText(
            current + ("\n" if current else "") + "今天有点累，想慢慢聊一会儿。"
        )
        self.state_label.setText("转写好了，先看一眼")
        self.hold.setEnabled(True)
        self.stop.hide()
        self.sync_send()
        self.draft.setFocus()

    def submit(self) -> None:
        if self.state != "review" or not self.draft.toPlainText().strip():
            return
        self.message.setText(self.draft.toPlainText().strip())
        self.message.show()
        self.draft.hide()
        self.reply.show()
        self.send.hide()
        self.hold.hide()
        self.read.show()
        self.state = "sent"
        self.state_label.setText("由你确认，才算发送")
        self.helper.setText("想听这条回复？朗读也由你主动开启。")

    def speak(self) -> None:
        self.state = "playing"
        self.state_label.setText("正在模拟朗读 · 随时可以打断")
        self.wave.set_active(True)
        self.read.setEnabled(False)
        self.stop.show()
        self.playback.start()

    def stop_all(self) -> None:
        self.transcription.stop()
        self.playback.stop()
        self.wave.set_active(False)
        self.hold.held = False
        self.hold.setDown(False)
        self.hold.setEnabled(True)
        self.hold.setText("按住试试")
        self.read.setEnabled(True)
        self.stop.hide()
        self.state = (
            "sent"
            if not self.message.isHidden()
            else ("review" if self.draft.toPlainText() else "idle")
        )
        self.state_label.setText("已停止，安静待在这里")
        self.sync_send()

    def reset(self) -> None:
        self.stop_all()
        self.state = "idle"
        self.draft.clear()
        self.draft.show()
        self.message.hide()
        self.reply.hide()
        self.read.hide()
        self.send.show()
        self.hold.show()
        self.helper.setText("转写不是发送。文字出现后，还可以修改。")
        self.state_label.setText("等你按住按钮")
        self.sync_send()

    def hideEvent(self, event: QHideEvent) -> None:
        self.stop_all()
        super().hideEvent(event)


class NetworkTryout(QFrame):
    """Offline consent/evidence simulation; no HTTP client, key, or persistence."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("tryout")
        self.state = "ready"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(12)
        layout.addWidget(label("一起查证一条消息", "demoCardTitle"))
        layout.addWidget(label("纯模拟 · 不联网 · 不保存 · 不收集密钥", "demoBadge"))
        self.provider = QComboBox()
        self.provider.addItems(
            ["示例搜索服务 A（模拟）", "示例搜索服务 B（模拟）"]
        )
        layout.addWidget(self.provider)
        self.configured = QCheckBox("模拟已配置搜索（正式功能默认关闭）")
        layout.addWidget(self.configured)
        self.query = label("“这个新框架，今天真的发布了吗？”", "demoState")
        layout.addWidget(self.query)
        self.search_button = button("模拟搜索", primary=True)
        self.search_button.clicked.connect(self.request_search)
        layout.addWidget(self.search_button)
        self.permission = QFrame()
        permission_layout = QVBoxLayout(self.permission)
        permission_layout.setContentsMargins(0, 0, 0, 0)
        self.permission_text = label("", "demoState")
        permission_layout.addWidget(self.permission_text)
        controls = QHBoxLayout()
        self.allow_button = button("仅允许这一次", primary=True)
        self.deny_button = button("这次不查")
        self.allow_button.clicked.connect(self.approve)
        self.deny_button.clicked.connect(self.reject)
        controls.addWidget(self.allow_button)
        controls.addWidget(self.deny_button)
        permission_layout.addLayout(controls)
        layout.addWidget(self.permission)
        self.evidence = label("", "demoState")
        self.evidence.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.evidence)
        self.read_button = button("继续模拟：读这篇文章")
        self.read_button.clicked.connect(self.request_page)
        layout.addWidget(self.read_button)
        self.status = label("", "demoSmall")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.failure_button = button("看看失败时怎么说")
        self.failure_button.clicked.connect(self.fail)
        self.reset_button = button("重新体验")
        self.reset_button.clicked.connect(self.reset)
        actions.addWidget(self.failure_button)
        actions.addWidget(self.reset_button)
        layout.addLayout(actions)
        self.provider.currentIndexChanged.connect(self.reset)
        self.configured.toggled.connect(self.reset)
        self.reset()

    def reset(self, _checked: object = None) -> None:
        self.state = "ready"
        self.permission.hide()
        self.evidence.clear()
        self.evidence.hide()
        self.read_button.hide()
        self.search_button.setEnabled(True)
        self.status.setText("先试试默认关闭的状态，或勾选上方开关模拟已配置。")

    def request_search(self) -> None:
        self.reset()
        if not self.configured.isChecked():
            self.state = "unconfigured"
            self.status.setText("搜索尚未配置，我现在不能查证。正式入口：设置 → 联网搜索。")
            return
        self.state = "search_permission"
        self.permission_text.setText(
            f"将把关键词‘示例新框架 发布’发送给 {self.provider.currentText()}。\n"
            "正式服务可能计费；不发送整段私人聊天。只允许本次搜索吗？"
        )
        self.permission.show()
        self.search_button.setEnabled(False)
        self.status.setText("这是模拟授权，不代表同意正式产品联网。")

    def approve(self) -> None:
        if self.state == "search_permission":
            self.state = "snippets"
            self.evidence.setText(
                "我刚刚搜索到……（模拟）\n"
                "示例摘要：某框架公布了一项更新，但仅凭摘要不能确认完整细节。\n\n"
                "示例来源：example.invalid/news/demo\n"
                "查阅时间：演示时间，不是实际查询\n"
                "发布日期：未提供，不推断为今天"
            )
            self.read_button.show()
            self.status.setText("摘要 ≠ 已读全文。想看正文，需要单独允许网页读取。")
        elif self.state == "page_permission":
            self.state = "page_content"
            self.evidence.setText(
                "我刚刚查阅了这篇文章，文章提到……（模拟）\n"
                "示例正文：本次更新增加了一个实验功能；具体可用范围仍需看原文说明。\n\n"
                "示例来源：example.invalid/news/demo\n"
                "查阅时间：演示时间，不是实际查询\n"
                "这是虚构演示，不是真实新闻或完整文章"
            )
            self.status.setText("只有成功取得正文，才说查阅过；正文截断时会如实标记。")
        else:
            return
        self.permission.hide()
        self.evidence.show()
        self.search_button.setEnabled(True)

    def request_page(self) -> None:
        if self.state != "snippets":
            return
        self.state = "page_permission"
        self.evidence.hide()
        self.read_button.hide()
        self.permission_text.setText(
            "将读取示例来源的网页正文（本演示不会请求它）。\n"
            "正式功能不发送登录 Cookie，不执行网页脚本。允许这次读取吗？"
        )
        self.permission.show()
        self.search_button.setEnabled(False)
        self.status.setText("网页内容只是证据，不会成为执行指令。")

    def reject(self) -> None:
        if self.state not in {"search_permission", "page_permission"}:
            return
        self.reset()
        self.state = "denied"
        self.status.setText("好，这次不查。没有新取得的内容，我不会假装已经看过。")

    def fail(self) -> None:
        self.reset()
        self.state = "failed"
        self.status.setText(
            "这次没有取得可核验的结果，可能是额度或网络问题。不能据此判断消息真假。"
        )


class CompanionIntroduction(QScrollArea):
    """Scrollable educational content; the owning OOBE supplies title and navigation."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.setObjectName("oobeScroll")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("oobeColumn")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(18)
        from PySide6.QtWidgets import QBoxLayout

        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._columns.setSpacing(28)
        descriptions = QVBoxLayout()
        descriptions.setSpacing(20)
        features = (
            (
                ("说完，先给你看", "转写回到输入框，不会直接发出去。"),
                ("想听，再开口", "点一下朗读完整回复；不想听了，随时停止。"),
                ("麦克风，由你掌握", "只在按住时录音。默认关闭，不在后台监听。"),
            )
            if kind == "voice"
            else (
                ("摘要，不冒充全文", "先搜索线索，再阅读文章；每一步都说清楚。"),
                ("来源和时间，给你看", "附来源和查阅时间，不把查阅时间当成发布时间。"),
                ("联网之前，先看权限", "搜索默认关闭；启用后仍受当前权限和话题禁区限制。"),
            )
        )
        for number, (title, detail) in enumerate(features, 1):
            row = QHBoxLayout()
            index = label(f"{number:02}", "demoIndex")
            index.setFixedWidth(28)
            row.addWidget(index, alignment=Qt.AlignmentFlag.AlignTop)
            text = QVBoxLayout()
            text.setSpacing(5)
            text.addWidget(label(title, "demoCardTitle"))
            text.addWidget(label(detail))
            row.addLayout(text, 1)
            descriptions.addLayout(row)
        descriptions.addStretch()
        self._columns.addLayout(descriptions, 1)
        self.tryout: VoiceTryout | NetworkTryout = (
            VoiceTryout() if kind == "voice" else NetworkTryout()
        )
        self.tryout.setMinimumWidth(0)
        self.tryout.setMaximumWidth(380)
        self.tryout.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._columns.addWidget(self.tryout, 1, Qt.AlignmentFlag.AlignTop)
        root.addLayout(self._columns)
        divider = QFrame()
        divider.setObjectName("demoDivider")
        divider.setFixedHeight(1)
        root.addWidget(divider)
        if kind == "voice":
            root.addWidget(
                label(
                    "现在可以接入语音 API，也能连接自己部署的本机兼容服务。",
                    "demoFooterLead",
                )
            )
            root.addWidget(
                label(
                    "启用前会说明音频和文字发往哪里；密钥加密保存。"
                    "当前不内置本地模型，声音由 AI 生成。以后可在「设置 → 语音」调整。",
                    "demoSmall",
                )
            )
        else:
            root.addWidget(label("想用时，再到「设置 → 联网搜索」配置。", "demoFooterLead"))
            root.addWidget(
                label(
                    "可用搜索服务及所需参数以「设置 → 联网搜索」为准；服务可能计费。"
                    "密钥仅进加密资料库。只读网页无需搜索 Key，不登录、不执行脚本、不读内网。"
                    "示例模拟逐次确认；实际以当前权限为准，完全访问可免确认，自定义档位当前不支持搜索。",
                    "demoSmall",
                )
            )
        self.setWidget(content)

    def resizeEvent(self, event: Any) -> None:
        from PySide6.QtWidgets import QBoxLayout

        stacked = self.viewport().width() < 650 or theme.FS_BASE > 18
        self._columns.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight,
        )
        super().resizeEvent(event)

    def stop_activity(self) -> bool:
        if isinstance(self.tryout, VoiceTryout):
            active = self.tryout.state in ("recording", "transcribing", "playing")
            self.tryout.stop_all()
            return active
        active = self.tryout.state in ("search_permission", "page_permission")
        self.tryout.reset()
        return active

    def reset(self) -> None:
        self.tryout.reset()
        self.verticalScrollBar().setValue(0)

    def hideEvent(self, event: QHideEvent) -> None:
        self.reset()
        super().hideEvent(event)


def introduction_stylesheet() -> str:
    """Production theme tokens; scoped to educational controls only."""
    return f"""
    QLabel#demoCardTitle {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_BASE + 1}px;
        font-weight: 600; }}
    QLabel#demoMuted {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_BASE}px; }}
    QLabel#demoSmall {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px; }}
    QLabel#demoFooterLead, QLabel#demoState {{ color: {theme.TEXT_PRIMARY};
        font-size: {theme.FS_BASE}px; }}
    QLabel#demoIndex, QLabel#demoBadge {{ color: {theme.ACCENT};
        font-size: {theme.FS_SMALL}px; }}
    QFrame#tryout {{ background: {theme.BG_SURFACE}; border: 1px solid {theme.BORDER};
        border-radius: {theme.RADIUS_BUBBLE}px; }}
    QPlainTextEdit#demoDraft {{ background: {theme.BG_ELEVATED};
        border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; padding: 10px; }}
    QLabel#demoMessage {{ background: {theme.BG_ELEVATED}; color: {theme.TEXT_PRIMARY};
        border-radius: {theme.RADIUS_MD}px; padding: 12px; }}
    QLabel#demoReply {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_BASE}px; }}
    QFrame#demoDivider {{ background: {theme.BORDER}; }}
    """
