"""User-owned, reversible reuse boundaries; never edits historical records."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.privacy_service import PrivacyService
from limbowave.domain.privacy import PrivacyPolicy
from limbowave.ui.background_tasks import BackgroundTasks
from limbowave.ui.floating import ask_confirm


class PrivacyPanel(QWidget):
    fresh_requested = Signal()

    def __init__(self, service: PrivacyService, conversation_id: str | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.conversation_id = conversation_id
        self._jobs = BackgroundTasks(self)
        self._generation = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode = QLabel("正在读取翻篇状态…")
        self.mode.setWordWrap(True)
        layout.addWidget(self.mode)
        self.fresh = QPushButton("翻篇聊 · 不带入过去", self)
        self.fresh.setObjectName("privacyFreshStart")
        self.fresh.setVisible(conversation_id is not None)
        self.fresh.clicked.connect(self._fresh)
        layout.addWidget(self.fresh)
        hint = QLabel(
            "话题禁区对整个资料库生效。匹配话题、别名或明确的资料 ID 后，不再自动复用。"
            "为防止间接带回，规则建立前的记忆和助手回复、无来源便签及压缩摘要也暂不带入。"
            "不删除聊天；不承诺识别所有同义表达。你本轮主动输入的内容仍会发送。"
            "修改前请先停止回复；已发往模型的内容无法撤回。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.rules = QListWidget()
        self.rules.setAccessibleName("持久化话题禁区")
        self.rules.setMinimumHeight(110)
        layout.addWidget(self.rules)
        form = QFormLayout()
        self.topic = QLineEdit()
        self.topic.setMaxLength(200)
        self.topic.setPlaceholderText("例如：那次离职")
        self.topic.setAccessibleName("不再主动关联的话题")
        self.aliases = QPlainTextEdit()
        self.aliases.setPlaceholderText("每行一个关键词或别名，例如旧项目名、相关人名")
        self.aliases.setMaximumHeight(70)
        self.aliases.setAccessibleName("话题别名，每行一个")
        self.sources = QLineEdit()
        self.sources.setPlaceholderText("可选：消息／记忆 ID，以逗号分隔")
        form.addRow("话题", self.topic)
        form.addRow("别名", self.aliases)
        form.addRow("资料 ID", self.sources)
        layout.addLayout(form)
        self.add = QPushButton("设为禁区")
        self.add.setObjectName("privacyAddBoundary")
        self.add.clicked.connect(self._add)
        self.remove = QPushButton("解除所选禁区…")
        self.remove.setEnabled(False)
        self.rules.currentItemChanged.connect(
            lambda item, _previous: self.remove.setEnabled(item is not None)
        )
        self.remove.clicked.connect(self._remove)
        layout.addWidget(self.add)
        layout.addWidget(self.remove)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.reload()

    def reload(self) -> None:
        self._generation += 1
        generation = self._generation
        self._jobs.submit(
            lambda: self.service.policy(self.conversation_id),
            lambda policy: self._loaded(policy) if generation == self._generation else None,
            lambda exc: self.status.setText(f"无法读取隐私规则：{exc}")
            if generation == self._generation else None,
        )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.reload()

    def hideEvent(self, event: QHideEvent) -> None:
        self._generation += 1
        self.rules.clear()
        self.topic.clear()
        self.aliases.clear()
        self.sources.clear()
        self.status.clear()
        super().hideEvent(event)

    def _loaded(self, policy: PrivacyPolicy) -> None:
        self.mode.setText(
            "本会话已翻篇：不带入全局个人记忆和其他聊天；系统行为规则仍有效。"
            if policy.fresh_start else "普通会话 · 原有历史保留；可另开一个不带入过去的聊天。"
        )
        self.rules.clear()
        for rule in policy.rules:
            item = QListWidgetItem(rule.topic)
            item.setData(Qt.ItemDataRole.UserRole, rule.id)
            item.setToolTip("别名：" + "、".join(rule.aliases))
            self.rules.addItem(item)
        self.rules.setToolTip("暂无话题禁区" if not policy.rules else "选择条目可解除")

    def _add(self) -> None:
        # Small encrypted writes run synchronously with the UI command loop: no send
        # can interleave between the idle check and commit.
        try:
            self.service.add(self.topic.text(), self.aliases.toPlainText().splitlines(),
                             [s.strip() for s in self.sources.text().split(",") if s.strip()])
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.topic.clear()
        self.aliases.clear()
        self.sources.clear()
        self.status.setText("禁区已保存，下次发送即按安全原文重建上下文。历史未删除。")
        self.reload()

    def _remove(self) -> None:
        item = self.rules.currentItem()
        if item is None:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)

        def remove(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.service.remove(rule_id)
                self.status.setText("禁区已解除；相应历史资料可能再次被带入。")
                self.reload()
            except Exception as exc:
                self.status.setText(str(exc))

        ask_confirm(self.window(), "解除话题禁区", "解除后相关历史可能再次被自动复用，确定继续？",
                    on_answer=remove, confirm_text="解除禁区")

    def _fresh(self) -> None:
        ask_confirm(
            self.window(), "翻篇聊", "新聊天不带入全局个人记忆、旧聊天、旧摘要或便签。"
            "原聊天完整保留，新聊天仍正常保存，也可积累本会话记忆；产品行为规则仍有效。",
            on_answer=lambda confirmed: self.fresh_requested.emit() if confirmed else None,
            confirm_text="开始翻篇聊天",
        )
