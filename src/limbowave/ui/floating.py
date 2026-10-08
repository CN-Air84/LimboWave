"""内建悬浮窗（in-app floating panel）：应用内弹出面板，替代系统级 QDialog/QMessageBox。

为什么要有它：系统对话框（``QDialog.exec()`` / ``QMessageBox``）是**另一扇 OS 窗口**——
有独立的标题栏、独立的外观、打断式的模态循环。这个组件做成**主窗口内的子部件**：

- 视觉与应用主题一致（同一个 stylesheet、同样的圆角与配色）；
- 点击面板外部或按 Esc 即关闭，不打断主循环（没有嵌套事件循环）；
- 交互改成**回调式**（``on_submit`` / ``finished``），调用方不再 ``exec()`` 阻塞。

API 约定（本模块的三个助手函数是全部调用入口）：

- :func:`open_panel` —— 把任意内容部件装进悬浮窗弹出；
- :func:`ask_confirm` —— 确认框（是/否），结果走回调；
- :func:`ask_choice` —— 多选一确认框（如权限的 拒绝/本会话允许/允许本次），结果走回调；
- :func:`ask_prompt` —— 单行/密码输入框，提交走回调（取消不回调）。

阻塞语义的变化必须如实说：调用方从「``exec()`` 返回后继续」改成「回调里继续」。
权限确认那条链路（``_ask_user``）保留了 await 语义——用 Future 桥接回调，
异步代码写法不变。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import soft_shadow, theme
from limbowave.ui.popup_material import install_popup_material


class _PanelShadow(QWidget):
    """Sibling canvas: the shadow must extend outside the panel, without replacing its fade."""

    def __init__(self, panel: FloatingPanel) -> None:
        super().__init__(panel.parentWidget())
        self._panel = panel
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")
        self.hide()
        panel.installEventFilter(self)
        panel._opacity.opacityChanged.connect(self._fade)
        panel.destroyed.connect(self.deleteLater)

    def _fade(self, opacity: float) -> None:
        self.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.ZOrderChange,
        ):
            self.setGeometry(soft_shadow.shadow_bounds(self._panel.geometry()))
            if self._panel.isVisible():
                self.show()
                self.stackUnder(self._panel)
            self.update()
        elif kind == QEvent.Type.Hide:
            self.hide()
        elif kind == QEvent.Type.ParentChange:
            self.setParent(self._panel.parentWidget())
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setOpacity(self._panel._opacity.opacity())
        body = QRectF(self._panel.geometry().translated(-self.pos()))
        soft_shadow.paint_soft_shadow(painter, body, 10, QRectF(self.rect()))
        painter.end()


class FloatingPanel(QFrame):
    """应用内悬浮面板。圆角卡片 + 标题栏 + 关闭按钮。

    关闭途径：点标题栏的 ✕、按 Esc、点击面板外部（经应用级事件过滤器）。
    """

    closed = Signal()

    def __init__(self, parent: QWidget, title: str, *, width: int = 420) -> None:
        super().__init__(parent)
        self._outside_filter_installed = False
        self._closing = False
        self._motion: QParallelAnimationGroup | None = None
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setEnabled(False)
        self.setObjectName("floatingPanel")
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setStyleSheet(
            f"#floatingPanel {{ background: {theme.BG_SURFACE};"
            " border: 1px solid transparent; border-radius: 10px; }"
            "QLabel { background: transparent; border: none; }"
            "QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateEdit"
            " { background: " + theme.BG_ELEVATED + "; }"
        )
        self.setMinimumWidth(min(width, 720))

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        self._title = QLabel(title)
        self._title.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_TITLE}px; font-weight: bold;"
        )
        header.addWidget(self._title)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭（Esc）")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" color: {theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        close_btn.clicked.connect(self.close_panel)
        header.addWidget(close_btn)
        root.addLayout(header)

        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        root.addLayout(self.content_layout, 1)
        install_popup_material(self, kind="card", radius=10, selector="QFrame#floatingPanel")
        self._shadow = _PanelShadow(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        # Material sampling can cover the QSS frame; stroke last so the outline
        # stays visible over wallpaper and follows live theme changes.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = QColor(theme.TEXT_SECONDARY)
        border.setAlphaF(0.65)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 9.5, 9.5)
        painter.end()

    # ---------- 生命周期 ----------

    def popup(self, anchor: QWidget | None = None) -> None:
        """显示面板。给 ``anchor``（通常是按钮）就贴着它下方弹出，否则居中。"""
        if self._closing:
            return
        self._stop_motion()
        parent = self.parentWidget()
        if parent is not None:
            parent_rect = parent.rect()
            self.adjustSize()
            if anchor is not None:
                top_left = anchor.mapTo(parent, anchor.rect().bottomLeft())
                x = min(max(8, top_left.x()), parent_rect.width() - self.width() - 8)
                y = top_left.y() + 4
                if y + self.height() > parent_rect.height() - 8:
                    y = max(8, parent_rect.height() - self.height() - 8)
            else:
                x = (parent_rect.width() - self.width()) // 2
                y = (parent_rect.height() - self.height()) // 3
            self.move(x, y)
        target = self.pos()
        self._opacity.setEnabled(True)
        self._opacity.setOpacity(0.0)
        self.move(target + QPoint(0, 10))
        self.raise_()
        self.show()
        self._animate(target, 1.0, 200, QEasingCurve.Type.OutCubic, self._finish_open)
        if not self._outside_filter_installed:
            QApplication.instance().installEventFilter(self)  # type: ignore[union-attr]
            self._outside_filter_installed = True

    def _animate(
        self,
        target: QPoint,
        opacity: float,
        duration: int,
        easing: QEasingCurve.Type,
        on_finished: Callable[[], None],
    ) -> None:
        group = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self._opacity, b"opacity", group)
        fade.setStartValue(self._opacity.opacity())
        fade.setEndValue(opacity)
        fade.setDuration(duration)
        fade.setEasingCurve(easing)
        slide = QPropertyAnimation(self, b"pos", group)
        slide.setStartValue(self.pos())
        slide.setEndValue(target)
        slide.setDuration(duration)
        slide.setEasingCurve(easing)
        group.addAnimation(fade)
        group.addAnimation(slide)
        group.finished.connect(on_finished)
        self._motion = group
        group.start()

    def _stop_motion(self) -> None:
        if self._motion is not None:
            self._motion.stop()
            self._motion.deleteLater()
            self._motion = None

    def _finish_open(self) -> None:
        self._motion = None
        self._opacity.setOpacity(1.0)
        self._opacity.setEnabled(False)

    def _finish_close(self) -> None:
        self._motion = None
        self.hide()
        self.deleteLater()

    def close_panel(self) -> None:
        if self._closing:
            return
        self._closing = True
        focus = QApplication.focusWidget()
        if focus is not None and (focus is self or self.isAncestorOf(focus)):
            focus.clearFocus()
        if self._outside_filter_installed:
            QApplication.instance().removeEventFilter(self)  # type: ignore[union-attr]
            self._outside_filter_installed = False
        self._stop_motion()
        if self.isVisible():
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._opacity.setEnabled(True)
            self._animate(
                self.pos() + QPoint(0, 8),
                0.0,
                150,
                QEasingCurve.Type.InCubic,
                self._finish_close,
            )
        else:
            self._finish_close()
        # 结果回调仍即时交付；视觉退场在后台完成。
        self.closed.emit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """点击面板外部即关闭；Esc 关闭。"""
        if event.type() == QEvent.Type.MouseButtonPress:
            widget = watched
            if isinstance(widget, QWidget) and not self.isAncestorOf(widget) and widget is not self:
                self.close_panel()
                return False
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_panel()
            return
        super().keyPressEvent(event)


def open_panel(
    parent: QWidget, title: str, content: QWidget, *, width: int = 420,
    anchor: QWidget | None = None,
) -> FloatingPanel:
    """把 ``content`` 装进悬浮窗弹出。返回面板（调用方可再挂 ``closed``）。"""
    panel = FloatingPanel(parent, title, width=width)
    if content.layout() is not None:
        content.layout().setContentsMargins(0, 0, 0, 0)  # type: ignore[union-attr]
    panel.content_layout.addWidget(content)
    panel.popup(anchor)
    return panel


def ask_confirm(
    parent: QWidget,
    title: str,
    detail: str,
    on_answer: Callable[[bool], None],
    *,
    confirm_text: str = "确定",
    cancel_text: str = "取消",
    danger: bool = False,
) -> FloatingPanel:
    """确认框（回调式）。``danger=True`` 时确认键标红语义（用普通强调色替代）。"""
    panel = FloatingPanel(parent, title, width=380)
    text = QLabel(detail)
    text.setWordWrap(True)
    text.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
    panel.content_layout.addWidget(text)

    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton(cancel_text)
    cancel.clicked.connect(panel.close_panel)
    row.addWidget(cancel)
    ok = QPushButton(confirm_text)
    ok.setProperty("accent", True)
    row.addWidget(ok)
    panel.content_layout.addLayout(row)

    # 「确定」与「面板被关闭（✕/Esc/点外部）」都会走这里，但只许回调一次：
    # 先 close_panel 再答 False 的顺序由 closed 信号触发，用 answered 守卫去重。
    answered = [False]

    def _answer(value: bool) -> None:
        if answered[0]:
            return
        answered[0] = True
        on_answer(value)

    def _ok() -> None:
        _answer(True)
        panel.close_panel()

    ok.clicked.connect(_ok)
    panel.closed.connect(lambda: _answer(False))
    panel.popup()
    return panel


def ask_choice(
    parent: QWidget,
    title: str,
    detail: str,
    choices: Sequence[tuple[str, str]],
    on_choice: Callable[[str | None], None],
    *,
    accent: str | None = None,
    width: int = 380,
) -> FloatingPanel:
    """多选一确认框（回调式）。``choices`` 是 (键, 按钮文字)，从左到右排列。

    点按钮回调它的键；✕/Esc/点外部回调 ``None``。与 :func:`ask_confirm` 一样
    **只回调一次**。正文按纯文本显示：调用方可能放进外部内容（如工具参数），
    不能让它被当成富文本解析。
    """
    panel = FloatingPanel(parent, title, width=width)
    text = QLabel(detail)
    text.setTextFormat(Qt.TextFormat.PlainText)
    text.setWordWrap(True)
    text.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
    panel.content_layout.addWidget(text)

    answered = [False]

    def _answer(value: str | None) -> None:
        if answered[0]:
            return
        answered[0] = True
        on_choice(value)

    def _picker(key: str) -> Callable[[], None]:
        def _pick() -> None:
            _answer(key)
            panel.close_panel()

        return _pick

    row = QHBoxLayout()
    row.addStretch(1)
    for key, label in choices:
        button = QPushButton(label)
        if key == accent:
            button.setProperty("accent", True)
        button.clicked.connect(_picker(key))
        row.addWidget(button)
    panel.content_layout.addLayout(row)

    panel.closed.connect(lambda: _answer(None))
    panel.popup()
    return panel


def ask_prompt(
    parent: QWidget,
    title: str,
    label: str,
    on_submit: Callable[[str], None],
    *,
    password: bool = False,
    animated_password: bool = False,
    default_text: str = "",
    placeholder: str = "",
    confirm_label: str = "确定",
    multiline: bool = False,
    on_suggest: Callable[[], Awaitable[str | None]] | None = None,
    suggest_label: str = "AI 重命名",
) -> FloatingPanel:
    """输入框（回调式）。提交空串或点 ✕/外部/Esc 都不回调。

    ``multiline=True`` 用多行编辑器（编辑消息用），``password=True`` 用密码框。
    ``animated_password=True`` 在密码框中复用登录页的输入动效。
    ``on_suggest`` 可选异步候选生成：只填入草稿，确认后才提交；关闭时取消。
    """
    panel = FloatingPanel(parent, title, width=380)
    caption = QLabel(label)
    caption.setTextFormat(Qt.TextFormat.PlainText)
    caption.setWordWrap(True)
    caption.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
    panel.content_layout.addWidget(caption)

    edit: QLineEdit | QPlainTextEdit
    if multiline:
        edit = QPlainTextEdit()
        edit.setPlaceholderText(placeholder)
        edit.setPlainText(default_text)
        edit.setMinimumHeight(100)
    else:
        line: QLineEdit
        if password and animated_password:
            from limbowave.ui.login_page import _AnimatedPasswordEdit

            line = _AnimatedPasswordEdit()
        else:
            line = QLineEdit()
            line.setEchoMode(QLineEdit.EchoMode.Password if password else QLineEdit.EchoMode.Normal)
        line.setPlaceholderText(placeholder)
        line.setText(default_text)
        edit = line
    panel.content_layout.addWidget(edit)

    row = QHBoxLayout()
    suggest = QPushButton(suggest_label) if on_suggest is not None else None
    if suggest is not None:
        row.addWidget(suggest)
    row.addStretch(1)
    cancel = QPushButton("取消")
    cancel.clicked.connect(panel.close_panel)
    row.addWidget(cancel)
    submit = QPushButton(confirm_label)
    submit.setProperty("accent", True)
    row.addWidget(submit)
    panel.content_layout.addLayout(row)

    suggestion_task: asyncio.Task[None] | None = None
    closed = False

    def _set_pending(pending: bool) -> None:
        edit.setReadOnly(pending)
        submit.setEnabled(not pending)
        if suggest is not None:
            suggest.setEnabled(not pending)
            suggest.setText("生成中…" if pending else suggest_label)

    async def _generate_suggestion() -> None:
        nonlocal suggestion_task
        assert on_suggest is not None
        try:
            value = await on_suggest()
            if closed:
                return
            if value and value.strip():
                if isinstance(edit, QPlainTextEdit):
                    edit.setPlainText(value.strip())
                else:
                    edit.setText(value.strip())
                    edit.selectAll()
                caption.setText(label)
                edit.setFocus()
            else:
                caption.setText("AI 命名失败，请重试或手动输入。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not closed:
                # ValueError is reserved for actionable local validation failures.
                caption.setText(
                    str(exc) if isinstance(exc, ValueError)
                    else "AI 命名失败，请重试或手动输入。"
                )
        finally:
            suggestion_task = None
            if not closed:
                _set_pending(False)
                panel.adjustSize()

    def _request_suggestion() -> None:
        nonlocal suggestion_task
        if closed or suggestion_task is not None:
            return
        caption.setText("正在根据会话内容生成名称…")
        _set_pending(True)
        suggestion_task = asyncio.ensure_future(_generate_suggestion())

    def _cancel_suggestion() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if suggestion_task is not None and not suggestion_task.cancelling():
            suggestion_task.cancel()

    if suggest is not None:
        suggest.clicked.connect(_request_suggestion)
        panel.closed.connect(_cancel_suggestion)
        panel.destroyed.connect(_cancel_suggestion)

    def _submit() -> None:
        if closed or suggestion_task is not None:
            return
        value = edit.toPlainText() if isinstance(edit, QPlainTextEdit) else edit.text()
        value = value.strip()
        if not value:
            return
        # 先回调再关面板：取消路径挂在 closed 上（得 None），
        # 顺序反了会让 closed 把已提交的值覆盖成 None（实测踩过）。
        on_submit(value)
        panel.close_panel()

    submit.clicked.connect(_submit)
    if isinstance(edit, QLineEdit):
        edit.returnPressed.connect(_submit)
    panel.popup()
    edit.setFocus()
    return panel


def ask_alert(parent: QWidget, title: str, detail: str) -> FloatingPanel:
    """单按钮提示框（替代 QMessageBox.warning/information 的展示型弹窗）。"""
    panel = FloatingPanel(parent, title, width=380)
    text = QLabel(detail)
    text.setWordWrap(True)
    text.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
    panel.content_layout.addWidget(text)
    row = QHBoxLayout()
    row.addStretch(1)
    ok = QPushButton("好")
    ok.setProperty("accent", True)
    ok.clicked.connect(panel.close_panel)
    row.addWidget(ok)
    panel.content_layout.addLayout(row)
    panel.popup()
    return panel
