"""Joint VOICE-01 + NET-01 onboarding proposal, awaiting explicit user acceptance.

Run: .venv/Scripts/python.exe tools/demos/voice_oobe_demo.py
Preview only: no microphone, API, history database, vault or persistence.
Production oobe_copy/oobe_demo/oobe_live are deliberately untouched.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QFontDatabase,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.oobe_copy import present, provider_by_id
from limbowave.ui.oobe_demo import _Stage, _stylesheet


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

    def paintEvent(self, event: object) -> None:
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
            ["Google / SerpAPI（模拟）", "Bing / SerpAPI（模拟）", "Tavily（模拟）"]
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


class VoiceOobeDemo(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("oobeDemo")
        self.setWindowTitle("LimboWave · 语音 + 联网查证（联合验收 Demo）")
        self.resize(1190, 830)
        self.setMinimumSize(1000, 760)
        self.screen_id = "voice"
        self.temporary_config: dict[str, object] = {}
        self.rail_buttons: dict[str, QPushButton] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName("demoHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(22, 13, 22, 13)
        row.addWidget(label("LimboWave / 灵波", "demoBrand"))
        row.addSpacing(18)
        review_label = label("OOBE 新板块合集 · 待联合验收", "demoMuted")
        review_label.setWordWrap(False)
        row.addWidget(review_label)
        row.addStretch()
        self.theme_button = button("浅色预览")
        self.theme_button.clicked.connect(self.toggle_theme)
        row.addWidget(self.theme_button)
        root.addWidget(header)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        rail = QFrame()
        rail.setObjectName("oobeRail")
        rail.setFixedWidth(205)
        nav = QVBoxLayout(rail)
        nav.setContentsMargins(20, 34, 18, 22)
        nav.setSpacing(9)
        nav.addWidget(label("流程对照", "demoCardTitle"))
        nav.addWidget(label("这一栏只用于验收，\n不会出现在正式引导里。", "demoSmall"))
        nav.addSpacing(24)
        for key, text in [
            ("intro", "01  原有 · 欢迎介绍"),
            ("voice", "02  新增 · 语音陪伴"),
            ("setup", "      可选 · 配置预览"),
            ("network", "03  新增 · 联网查证"),
            ("welcome", "04  原有 · 接入服务商"),
        ]:
            control = button(text)
            control.setObjectName("oobeRailButton")
            control.setCheckable(True)
            control.clicked.connect(lambda checked=False, k=key: self.show_screen(k))
            nav.addWidget(control)
            self.rail_buttons[key] = control
        nav.addStretch()
        nav.addWidget(label("两个新板块一起验收。\n通过后才接入正式 OOBE。", "demoSmall"))
        body.addWidget(rail)
        self.stage = _Stage()
        stage_layout = QVBoxLayout(self.stage)
        stage_layout.setContentsMargins(48, 38, 40, 26)
        self.pages = QStackedWidget()
        self.pages.setObjectName("oobePagesStack")
        self.indices: dict[str, int] = {}
        self.indices["intro"] = self.pages.addWidget(self.build_context("intro"))
        self.indices["voice"] = self.pages.addWidget(self.build_voice())
        self.indices["network"] = self.pages.addWidget(self.build_network())
        self.indices["setup"] = self.pages.addWidget(self.build_setup())
        self.indices["welcome"] = self.pages.addWidget(self.build_context("welcome"))
        stage_layout.addWidget(self.pages)
        body.addWidget(self.stage, 1)
        root.addLayout(body, 1)
        self.apply_theme()
        self.show_screen("voice")
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

    def heading(self, layout: QVBoxLayout, eyebrow: str, title: str, body: str) -> None:
        mark = QFrame()
        mark.setObjectName("oobeMark")
        mark.setFixedSize(36, 3)
        layout.addWidget(mark)
        layout.addSpacing(18)
        if eyebrow:
            layout.addWidget(label(eyebrow, "oobeEyebrow"))
            layout.addSpacing(7)
        layout.addWidget(label(title, "oobeTitle"))
        layout.addSpacing(14)
        layout.addWidget(label(body, "oobeBody"))

    def build_context(self, screen: str) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        copy = present(screen, provider_by_id("deepseek"))
        self.heading(layout, copy.eyebrow, copy.title, copy.body)
        layout.addSpacing(26)
        if screen == "welcome":
            for i, text in enumerate(["先选择服务商", "然后填入密钥", "再选择模型"], 1):
                layout.addWidget(label(f"0{i}    {text}", "demoCardTitle"))
            layout.addSpacing(16)
            layout.addWidget(label("接下来继续原来的设置流程；本 demo 到这里为止。", "demoSmall"))
        controls = QHBoxLayout()
        back = button("返回联网介绍")
        back.clicked.connect(lambda: self.show_screen("network"))
        primary = button("继续" if screen == "intro" else "再看一遍新增介绍", primary=True)
        primary.clicked.connect(lambda: self.show_screen("voice"))
        controls.addStretch()
        if screen == "welcome":
            controls.addWidget(back)
        controls.addWidget(primary)
        layout.addSpacing(22)
        layout.addLayout(controls)
        layout.addStretch()
        return page

    def build_voice(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        columns = QHBoxLayout()
        columns.setSpacing(32)
        copy = QVBoxLayout()
        copy.setSpacing(0)
        self.heading(
            copy,
            "可选的陪伴方式",
            "不想打字的时候，\n也可以说给我听。",
            "按住说话，松开转成文字。\n看一眼、改一改，再决定要不要发送。",
        )
        copy.addSpacing(32)
        for number, title, detail in [
            ("01", "说完，先给你看", "转写回到输入框，不会直接发出去。"),
            ("02", "想听，再开口", "点一下朗读完整回复；不想听了，随时停止。"),
            ("03", "麦克风，由你掌握", "只在按住时录音。默认关闭，不在后台监听。"),
        ]:
            line = QHBoxLayout()
            index = label(number, "demoIndex")
            index.setFixedWidth(28)
            line.addWidget(index, alignment=Qt.AlignmentFlag.AlignTop)
            texts = QVBoxLayout()
            texts.setSpacing(5)
            texts.addWidget(label(title, "demoCardTitle"))
            texts.addWidget(label(detail))
            line.addLayout(texts, 1)
            copy.addLayout(line)
            copy.addSpacing(21)
        copy.addStretch()
        columns.addLayout(copy, 1)
        self.tryout = VoiceTryout()
        columns.addWidget(self.tryout, 1, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(columns, 1)
        root.addSpacing(25)
        line = QFrame()
        line.setObjectName("demoDivider")
        line.setFixedHeight(1)
        root.addWidget(line)
        root.addSpacing(18)
        root.addWidget(
            label("现在可以接入语音 API，以后也能连接你自己部署的本地服务。", "demoFooterLead")
        )
        root.addSpacing(7)
        root.addWidget(
            label(
                "启用前会说明音频和文字发往哪里；密钥加密保存。"
                "当前不内置本地模型，声音由 AI 生成。",
                "demoSmall",
            )
        )
        root.addSpacing(20)
        actions = QHBoxLayout()
        back = button("上一步")
        back.clicked.connect(lambda: self.show_screen("intro"))
        configure = button("看看语音设置")
        configure.clicked.connect(lambda: self.show_screen("setup"))
        self.continue_button = button("知道了，继续", primary=True)
        self.continue_button.clicked.connect(lambda: self.show_screen("network"))
        actions.addWidget(back)
        actions.addStretch()
        actions.addWidget(configure)
        actions.addWidget(self.continue_button)
        root.addLayout(actions)
        root.addSpacing(8)
        self.footer_note = label("不用现在配置。以后随时在「设置 → 语音」中开启。", "demoSmall")
        self.footer_note.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(self.footer_note)
        return page


    def build_network(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        columns = QHBoxLayout()
        columns.setSpacing(30)
        copy = QVBoxLayout()
        self.heading(
            copy,
            "让现实信息可核验",
            "新消息，\n我们一起查证。",
            "遇到刚发布的新闻或新框架，\n不知道的，不装作知道。",
        )
        copy.addSpacing(26)
        for number, title, detail in [
            ("01", "摘要，不冒充全文", "先搜索线索，再阅读文章；每一步都说清楚。"),
            ("02", "来源和时间，给你看", "附来源和查阅时间，不把查阅时间当成发布时间。"),
            ("03", "联网之前，由你决定", "搜索默认关闭；搜索逐次确认，只读网页也受权限控制。"),
        ]:
            line = QHBoxLayout()
            index = label(number, "demoIndex")
            index.setFixedWidth(28)
            line.addWidget(index, alignment=Qt.AlignmentFlag.AlignTop)
            texts = QVBoxLayout()
            texts.setSpacing(5)
            texts.addWidget(label(title, "demoCardTitle"))
            texts.addWidget(label(detail))
            line.addLayout(texts, 1)
            copy.addLayout(line)
            copy.addSpacing(17)
        copy.addStretch()
        columns.addLayout(copy, 1)
        self.network_tryout = NetworkTryout()
        columns.addWidget(self.network_tryout, 1, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(columns, 1)
        root.addSpacing(16)
        divider = QFrame()
        divider.setObjectName("demoDivider")
        divider.setFixedHeight(1)
        root.addWidget(divider)
        root.addSpacing(12)
        root.addWidget(label("想用时，再到 设置 → 联网搜索 配置。", "demoFooterLead"))
        root.addSpacing(6)
        root.addWidget(
            label(
                "Google / Bing 经第三方 SerpAPI；也可用 Tavily。需对应 Key，可能计费；"
                "密钥仅进加密资料库。只读网页无需搜索 Key，不登录、不执行脚本、不读内网。",
                "demoSmall",
            )
        )
        root.addSpacing(14)
        actions = QHBoxLayout()
        back = button("返回语音介绍")
        back.clicked.connect(lambda: self.show_screen("voice"))
        self.network_continue_button = button("知道了，继续", primary=True)
        self.network_continue_button.clicked.connect(lambda: self.show_screen("welcome"))
        actions.addWidget(back)
        actions.addStretch()
        actions.addWidget(self.network_continue_button)
        root.addLayout(actions)
        return page

    def build_setup(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.heading(
            outer,
            "可选 · 随时可以跳过",
            "给声音，留一个入口。",
            "转写和朗读可以分别配置。\n这一步不影响你先开始文字聊天。",
        )
        outer.addSpacing(20)
        warning = label("配置预览：不连接服务，不保存；请不要填入真实密钥。", "demoSmall")
        outer.addWidget(warning)
        outer.addSpacing(16)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet("background: transparent;")
        form_widget = QWidget()
        form_widget.setObjectName("oobeColumn")
        forms = QVBoxLayout(form_widget)
        forms.setContentsMargins(0, 0, 15, 0)
        self.fields: dict[str, dict[str, QWidget]] = {}
        for name, caption in [("stt", "语音转写"), ("tts", "回复朗读")]:
            forms.addWidget(label(caption, "demoCardTitle"))
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            form.setSpacing(9)
            enabled = QCheckBox("启用（默认关闭）")
            mode = QComboBox()
            mode.addItems(["受控 API", "本机兼容服务"])
            url = QLineEdit()
            url.setPlaceholderText("https://speech.example/v1")
            model = QLineEdit()
            model.setPlaceholderText("填写服务支持的模型 ID")
            key = QLineEdit()
            key.setEchoMode(QLineEdit.EchoMode.Password)
            key.setPlaceholderText("仅演示，请勿填写真实密钥")
            key.setAccessibleName(caption + "演示密钥")
            form.addRow("", enabled)
            for title, control in [
                ("服务方式", mode),
                ("API 地址", url),
                ("模型 ID", model),
                ("API 密钥", key),
            ]:
                form.addRow(title, control)
            fields = {"enabled": enabled, "mode": mode, "url": url, "model": model, "key": key}
            self.fields[name] = fields
            mode.currentIndexChanged.connect(lambda i, f=fields: self.mode_changed(f, i))
            forms.addLayout(form)
            forms.addSpacing(20)
        forms.addWidget(
            label("本地模式需要自行启动兼容服务，尚不内置模型下载或离线引擎。", "demoSmall")
        )
        forms.addStretch()
        area.setWidget(form_widget)
        outer.addWidget(area, 1)
        controls = QHBoxLayout()
        back = button("暂时不用")
        back.clicked.connect(lambda: self.show_screen("voice"))
        save = button("模拟保存，返回介绍", primary=True)
        save.clicked.connect(self.mock_save)
        controls.addStretch()
        controls.addWidget(back)
        controls.addWidget(save)
        outer.addLayout(controls)
        return page

    def mode_changed(self, fields: dict[str, QWidget], index: int) -> None:
        fields["key"].clear()
        fields["url"].setPlaceholderText(
            "http://127.0.0.1:8000/v1" if index else "https://speech.example/v1"
        )
        fields["key"].setPlaceholderText(
            "本机无鉴权服务可以留空" if index else "仅演示，请勿填写真实密钥"
        )

    def mock_save(self) -> None:
        self.temporary_config = {
            name: {
                "enabled": fields["enabled"].isChecked(),
                "local": fields["mode"].currentIndex() == 1,
            }
            for name, fields in self.fields.items()
        }
        for fields in self.fields.values():
            fields["key"].clear()
        self.show_screen("voice")
        self.footer_note.setText("已模拟保存，仅当前 demo 有效；正式设置未改动。")

    def show_screen(self, screen: str) -> None:
        self.tryout.stop_all()
        self.network_tryout.reset()
        if screen == "voice" and self.tryout.state == "idle":
            self.tryout.state_label.setText("等你按住按钮")
        for fields in self.fields.values():
            fields["key"].clear()
        self.screen_id = screen
        self.pages.setCurrentIndex(self.indices[screen])
        for key, control in self.rail_buttons.items():
            control.setChecked(key == screen)

    def apply_theme(self) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(theme.app_stylesheet())
        self.setStyleSheet(
            _stylesheet()
            + f"""
            QWidget#oobeDemo, QStackedWidget#oobePagesStack {{ background: transparent; }}
            QFrame#demoHeader {{
                background: {theme.BG_SURFACE};
                border-bottom: 1px solid {theme.BORDER};
            }}
            QLabel#demoBrand, QLabel#demoCardTitle {{
                color: {theme.TEXT_PRIMARY};
                font-size: {theme.FS_BASE + 1}px;
                font-weight: 600;
            }}
            QLabel#demoMuted {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_BASE}px; }}
            QLabel#demoSmall {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px; }}
            QLabel#demoFooterLead {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_BASE}px; }}
            QLabel#demoIndex {{
                color: {theme.ACCENT};
                font-size: {theme.FS_BASE + 1}px;
                font-weight: 600;
            }}
            QLabel#demoBadge {{ color: {theme.ACCENT}; font-size: {theme.FS_SMALL}px; }}
            QLabel#demoState {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_BASE}px; }}
            QFrame#tryout {{
                background: {theme.BG_SURFACE};
                border: 1px solid {theme.BORDER};
                border-radius: 18px;
            }}
            QPlainTextEdit#demoDraft {{
                background: {theme.BG_ELEVATED};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
                padding: 10px;
            }}
            QLabel#demoMessage {{
                background: {theme.BG_ELEVATED};
                color: {theme.TEXT_PRIMARY};
                border-radius: 10px;
                padding: 12px;
            }}
            QLabel#demoReply {{
                color: {theme.TEXT_PRIMARY};
                font-size: {theme.FS_BASE}px;
                padding: 4px 0;
            }}
            QFrame#demoDivider {{ background: {theme.BORDER}; }}
        """
        )
        self.stage.update()
        self.tryout.wave.update()

    def toggle_theme(self) -> None:
        dark = theme.current_palette().name == "dark"
        theme.set_palette("light" if dark else "dark")
        self.theme_button.setText("深色预览" if dark else "浅色预览")
        self.apply_theme()

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            if self.tryout.state in ("recording", "transcribing", "playing"):
                self.tryout.stop_all()
            elif self.screen_id == "network" and self.network_tryout.state in {
                "search_permission", "page_permission",
            }:
                self.network_tryout.reject()
            elif self.screen_id == "network" and self.network_tryout.state != "ready":
                self.network_tryout.reset()
            elif self.screen_id == "network":
                self.show_screen("voice")
            elif self.screen_id == "welcome":
                self.show_screen("network")
            elif self.screen_id != "voice":
                self.show_screen("voice")
            return True
        if event.type() == QEvent.Type.ApplicationDeactivate and self.tryout.state == "recording":
            self.tryout.stop_all()
        return super().eventFilter(watched, event)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    # Explicit font loading also makes offscreen Windows screenshots faithful.
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        theme.set_font_family("Microsoft YaHei")
    theme.set_palette("dark")
    window = VoiceOobeDemo()
    window.show()
    if args.screenshots:
        from PySide6.QtTest import QTest

        args.screenshots.mkdir(parents=True, exist_ok=True)

        def capture() -> None:
            QTest.qWait(100)
            window.grab().save(str(args.screenshots / "voice-intro-dark.png"))
            window.tryout.state = "transcribing"
            window.tryout.transcribed()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "voice-review-dark.png"))
            window.tryout.submit()
            window.tryout.speak()
            QTest.qWait(160)
            window.grab().save(str(args.screenshots / "voice-playing-dark.png"))
            window.tryout.reset()
            window.show_screen("setup")
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "voice-setup-dark.png"))
            window.show_screen("network")
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "network-intro-dark.png"))
            window.network_tryout.configured.setChecked(True)
            window.network_tryout.request_search()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "network-permission-dark.png"))
            window.network_tryout.approve()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "network-snippets-dark.png"))
            window.network_tryout.request_page()
            window.network_tryout.approve()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "network-article-dark.png"))
            window.show_screen("voice")
            window.toggle_theme()
            QTest.qWait(100)
            window.grab().save(str(args.screenshots / "voice-intro-light.png"))
            window.resize(1000, 760)
            window.show_screen("network")
            QTest.qWait(100)
            window.grab().save(str(args.screenshots / "network-minimum-light.png"))
            window.close()
            app.quit()

        QTimer.singleShot(120, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
