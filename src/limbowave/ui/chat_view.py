"""聊天视图：消息列表 + 输入区。

架构约束：本视图持有**展示态**（当前消息流的呈现），这是视图职责所在；
权威业务状态（会话树、分支、权限、上下文）不在此处。用户意图经信号外发。

视觉结构：消息排在居中的一栏里（最宽与输入框一致），每条消息是一个行容器
（``_BubbleRow``）。用户消息套靠右的强调色气泡；助手回复放在铺满整栏的
低对比度背景板里，内含 Markdown 正文（``QTextBrowser``）与复制 / Fork 动作栏；
错误是靠左的暖红提示块。用户消息保持纯文本。

一轮 Agent run 只占**一张**卡片（长任务不再碎成多卡）：卡片内按模型消息分成
若干段（正文 → 工具 → 正文 → …），段间以细分隔线区分；思考块与工具 chip
归属各自触发它们的分段；卡底一条状态行提示整轮进行到哪一步，动作栏在整轮
收敛后统一露出。历史重载按 ``run_id`` 把同轮消息并回同一张卡。
手动重试复用原用户行与回复栏，清掉旧尝试的展示并渐隐渐显一次；后台尝试记录仍保留。

发起一轮即预立卡片（``set_busy(True)`` 时就开好卡与首段）：正文位置最上方
先出现「正在构思……N秒」计秒，思考流也算构思，首个正文 delta（或工具开始、
本段定稿）落字即收；没等到任何内容的空卡在整轮收敛时撤掉。

流式渲染策略：流式期间气泡显示**纯文本**（每 delta 重排 HTML 会抖动且浪费），
``end_assistant`` 时一次性渲染 Markdown。这是有意的取舍：流式的实时性优先，
排版在定稿时一步到位。

思考块（标准 reasoning，§13.2）：在助手正文上方，默认折叠，点击展开。
只读协议标准字段，不解析 ``<think>`` 标签（那是 Pi 侧的事）。

压缩期间模型的输出**不是对话的一轮**：不建气泡；思考流单独进「压缩 · 思考过程」
悬浮窗（``CompressionThinkingPanel``），摘要正文留给压缩预览。开始压缩时若正有
一条回复在流式，那条回复照旧进它自己的气泡。
"""

from __future__ import annotations

import asyncio
import math
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import cast

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QMimeData,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QSizeF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QEnterEvent,
    QGuiApplication,
    QHideEvent,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.history_payload import HistoryEntry as HistoryEntry
from limbowave.domain.conversation import AssistantMessageSegment
from limbowave.domain.permissions import PermissionPreset
from limbowave.domain.tool_step import ToolStep, finish
from limbowave.ui import markdown_render, soft_shadow, theme
from limbowave.ui.attachment_bar import AttachmentBar, AttachmentMenu
from limbowave.ui.backdrop import BackdropEngine
from limbowave.ui.compression_widgets import (
    CompressButton,
    CompressionDivider,
    CompressionThinkingPanel,
)
from limbowave.ui.history_loading import HistoryLoadingOverlay
from limbowave.ui.message_motion import MessageSendEffect
from limbowave.ui.model_selector import ModelSelector, ModelSite
from limbowave.ui.popup_material import install_popup_material
from limbowave.ui.popup_motion import PopupMotion
from limbowave.ui.run_state_label import RunStateLabel
from limbowave.ui.sent_attachments import SentAttachment, SentAttachmentStrip
from limbowave.ui.thinking_block import ThinkingBlock as _ThinkingBlock
from limbowave.ui.tool_steps import ToolStepsView, make_tool_steps

# 一次物化多少条历史消息。每条消息是一个富文本组件，全量物化会卡死长会话。
HISTORY_PAGE = 60
_COMPOSER_RADIUS = 18
# 压缩进度：初值为当前上下文占比，每秒减少千分之三（按整条进度计，即 0.3 个百分点）。
# 按 100ms 小步走，速率不变、观感更平滑。
COMPRESSION_DRAIN_PER_SEC = 0.3
_COMPRESSION_TICK_MS = 100
_COMPRESSION_SETTLE_MS = 450  # 完成后多退少补到压缩后占比的动画时长
_COMPRESSION_HOLD_MS = 350  # 补到位后停留片刻再撤遮罩
_DOCK_MS = 360  # 空会话居中的输入框落到底部（或回到中心）的位移时长
_HISTORY_FADE_OUT_MS = 120
_HISTORY_FADE_IN_MS = 210
_RETRY_FADE_MS = 330
_SEND_ENTER_MS = 260
_TOOL_STEPS_FADE_MS = 220
# 空白页进入历史会话只从下方轻抬一点；跟随输入框的大位移会让起点显得过低。
_HISTORY_ENTER_OFFSET = 8
_COMPOSER_WRAP_BOTTOM = 14  # 输入框外包布局的底边距
_COMPOSER_WRAP_SIDE = 16  # 输入框行与对话区左右边缘的最小间距
_COMPOSER_MAX_WIDTH = 800  # 输入框最宽多少；对话区再宽，输入框（连同高级栏）居中
_COMPOSER_MIN_WIDTH = 480  # 窄对话区里，收起高级栏后输入框至少放宽到这么宽
# 消息排在居中的一栏里，最宽与输入框一致（_COMPOSER_MAX_WIDTH）；对话区再宽只加大两侧留白。
_MESSAGE_SIDE_MIN = 32  # 消息栏与对话区左右边缘的最小留白
_MESSAGE_COLUMN_MIN = 240  # 视口还没排版（很窄）时，气泡宽度按这个栏宽估算
_MESSAGE_SPACING = 20  # 相邻两条消息的间距
_USER_BUBBLE_RATIO = 0.78  # 用户气泡最宽占栏宽的比例；短消息按文字收窄
_BUBBLE_PAD_X = 16  # 气泡内文字与左右边缘的间距
_BUBBLE_PAD_Y = 10  # 气泡内文字与上下边缘的间距
_ACTION_BUTTON_SIZE = 28  # 消息动作按钮（复制 / Fork / 编辑）的点击区
_COPIED_FEEDBACK_MS = 1500  # 复制后对勾停留多久再换回复制图标
_ADVANCED_SLIDE_MS = 220  # 高级栏向右展开 / 收回、输入框随之平移的时长
# 输入框的投影观感参数在 soft_shadow（与高级栏共用），范围罩在输入框行的
# 上下边距（10 / 14）以内，否则会被上面的消息区、下面的垫块盖住一截。
_TRANSCRIPT_BOTTOM = 12  # 消息区内容的底边距
_STICK_THRESHOLD = 24  # 距底部多少像素以内视为「停在底部」，流式输出会自动跟随
_JUMP_BUTTON_GAP = 16  # 「回到底部」按钮与消息区可见底边的间距
_JUMP_BUTTON_MS = 200  # 「回到底部」按钮进出场时长
_JUMP_BUTTON_RISE = 16  # 进场时从就位点下方多少像素浮上来（退场沉回同样距离）


class _ComposingHint(QLabel):
    """「正在构思……N秒」：本段正文落字前挂在正文位置最上方的构思计时。

    思考流也算构思的一部分——首个正文 delta（或工具开始执行、本段定稿、
    整轮收敛）才结束构思并把提示收起；计秒每秒走一格。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("正在构思……0秒", parent)
        self._seconds = 0
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
            " background: transparent; border: none;"
        )
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self) -> None:
        """从 0 秒开始计时并显示。"""
        self._seconds = 0
        self._show_seconds()
        self.show()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        self._seconds += 1
        self._show_seconds()

    def _show_seconds(self) -> None:
        self.setText(f"正在构思……{self._seconds}秒")


class _AssistantSegment(QWidget):
    """助手卡片里的一段：一轮 Agent run 中的一条模型消息。

    思考块、Markdown 正文、工具步骤 chip 都归属各自的分段——哪个分段调的
    工具，chip 就跟在哪段正文下面；段与段之间由卡片插入细分隔线。构思计时
    （``hint``）挂在本段最上方：本段还没落字时计秒，落字即收。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.message_id: str | None = None
        self.raw_text = ""  # 流式期间的纯文本累积，定稿时渲染
        self.thinking: _ThinkingBlock | None = None
        self.content: QTextBrowser | None = None
        self.tool_steps_view: ToolStepsView | None = None
        self.live_tool_steps: list[ToolStep] = []
        self._tool_motion_has_body = False
        self.hint: _ComposingHint | None = None
        self.setStyleSheet("background: transparent;")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.column = column

    def start_composing(self) -> None:
        """本段还没有内容：在正文位置顶部立起构思计时。"""
        if self.hint is None:
            hint = _ComposingHint()
            self.column.insertWidget(0, hint)
            self.hint = hint
        self.hint.start()

    def stop_composing(self) -> None:
        if self.hint is not None:
            self.hint.stop()

    def is_empty(self) -> bool:
        """本段是否还没出现过任何可见内容（正文 / 思考 / 工具步骤）。"""
        return (
            not self.raw_text
            and (self.thinking is None or not self.thinking.has_content())
            and not self.live_tool_steps
            and self.tool_steps_view is None
        )


def _set_tool_motion_spacing(
    layout: QVBoxLayout, weights: Mapping[QWidget, float] | None,
) -> None:
    """Keep gap items between visible neighbors; update only their pixel sizes."""
    if weights is None:
        if layout.spacing() == 8:
            return
        for index in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                layout.takeAt(index)
        layout.setSpacing(8)
        return
    layout.setSpacing(0)
    previous = 0.0
    seen = False
    index = 0
    changed = False
    while index < layout.count():
        item = layout.itemAt(index)
        if item is None:
            break
        spacer = item.spacerItem()
        widget_item = layout.itemAt(index + 1) if spacer is not None else item
        widget = widget_item.widget() if widget_item is not None else None
        visible = widget is not None and not widget.isHidden()
        if spacer is not None and (not seen or not visible):
            layout.takeAt(index)
            changed = True
            continue
        if not visible or widget is None:
            index += 1
            continue
        weight = weights.get(widget, 1.0)
        if seen:
            if spacer is None:
                layout.insertSpacing(index, 0)
                inserted = layout.itemAt(index)
                assert inserted is not None
                spacer = inserted.spacerItem()
                changed = True
            gap = round(8 * min(previous, weight))
            if spacer is not None and spacer.sizeHint().height() != gap:
                spacer.changeSize(0, gap, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
                changed = True
            index += 1
        previous = max(previous, weight)
        seen = True
        index += 1
    if changed:
        layout.invalidate()
        parent = layout.parentWidget()
        if parent is not None:
            parent.updateGeometry()


def _make_segment_divider() -> QFrame:
    """段与段之间的细分隔线：一眼能分出是两段模型输出，又同属一张卡。"""
    line = QFrame()
    line.setObjectName("assistantSegmentDivider")
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.qss_alpha(theme.BORDER, 0.8)}; border: none;")
    return line


# ----- 消息动作按钮的图标：16×16 坐标系里直接画，不用字体字符（缺字时会退化成方框） -----


def _paint_copy_icon(painter: QPainter) -> None:
    front = QRectF(5.5, 5.5, 8.0, 8.0)
    # 后面那张只画露出来的部分：挖掉前一张（连同一圈间隙）再画
    visible = QPainterPath()
    visible.addRect(QRectF(0.0, 0.0, 16.0, 16.0))
    covered = QPainterPath()
    covered.addRoundedRect(front.adjusted(-1.6, -1.6, 1.6, 1.6), 3.0, 3.0)
    painter.save()
    painter.setClipPath(visible.subtracted(covered))
    painter.drawRoundedRect(QRectF(2.5, 2.5, 8.0, 8.0), 2.0, 2.0)
    painter.restore()
    painter.drawRoundedRect(front, 2.0, 2.0)


def _paint_check_icon(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(3.5, 8.5)
    path.lineTo(6.5, 11.5)
    path.lineTo(12.5, 4.5)
    painter.drawPath(path)


def _paint_fork_icon(painter: QPainter) -> None:
    for x, y in ((4.5, 3.5), (11.5, 3.5), (8.0, 12.5)):
        painter.drawEllipse(QPointF(x, y), 1.8, 1.8)
    path = QPainterPath()
    path.moveTo(4.5, 5.3)
    path.cubicTo(4.5, 8.4, 8.0, 7.6, 8.0, 10.7)
    path.moveTo(11.5, 5.3)
    path.cubicTo(11.5, 8.4, 8.0, 7.6, 8.0, 10.7)
    painter.drawPath(path)


def _paint_edit_icon(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(3.0, 13.0)
    path.lineTo(3.6, 10.2)
    path.lineTo(10.4, 3.4)
    path.lineTo(12.6, 5.6)
    path.lineTo(5.8, 12.4)
    path.closeSubpath()
    path.moveTo(9.0, 4.8)
    path.lineTo(11.2, 7.0)
    painter.drawPath(path)


def _paint_retry_icon(painter: QPainter) -> None:
    # 顺时针的圆弧 + 末端箭头
    path = QPainterPath()
    path.arcMoveTo(QRectF(3.0, 3.0, 10.0, 10.0), 60.0)
    path.arcTo(QRectF(3.0, 3.0, 10.0, 10.0), 60.0, 270.0)
    painter.drawPath(path)
    head = QPainterPath()
    head.moveTo(10.2, 1.6)
    head.lineTo(10.8, 4.8)
    head.lineTo(7.6, 5.4)
    painter.drawPath(head)


_ACTION_ICONS: dict[str, Callable[[QPainter], None]] = {
    "copy": _paint_copy_icon,
    "check": _paint_check_icon,
    "fork": _paint_fork_icon,
    "edit": _paint_edit_icon,
    "retry": _paint_retry_icon,
}


class _ActionButton(QPushButton):
    """消息动作的小图标按钮（复制 / Fork / 编辑）。静止时次要色，悬停时正文色。"""

    def __init__(self, icon: str, name: str, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon = icon
        self._rest_icon = icon
        self._rest_tooltip = tooltip
        self.setAccessibleName(name)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_ACTION_BUTTON_SIZE, _ACTION_BUTTON_SIZE)
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 0;"
            f" border-radius: {theme.RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {theme.BG_SURFACE_HOVER}; }}"
        )
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.setInterval(_COPIED_FEEDBACK_MS)
        self._restore_timer.timeout.connect(self._restore)

    @property
    def icon_name(self) -> str:
        return self._icon

    def flash(self, icon: str, tooltip: str) -> None:
        """临时换一个图标（例如复制后的对勾），片刻后自动换回。"""
        self._icon = icon
        self.setToolTip(tooltip)
        self.update()
        self._restore_timer.start()

    def _restore(self) -> None:
        self._icon = self._rest_icon
        self.setToolTip(self._rest_tooltip)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        color = theme.TEXT_PRIMARY if self.underMouse() else theme.TEXT_SECONDARY
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor(color),
                1.4,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.translate((self.width() - 16) / 2, (self.height() - 16) / 2)
        _ACTION_ICONS[self._icon](painter)
        painter.end()


class _BubbleRow(QWidget):
    """一条消息的行容器，排在居中的消息栏里。

    - 用户消息：靠右的气泡，宽度随文字收窄，最宽占栏宽的 ``_USER_BUBBLE_RATIO``；
      编辑按钮在气泡左侧，悬停才显示（位置常驻，显隐时气泡不挪动）；
      这条消息发送失败时，编辑按钮再左侧常驻一个重试按钮。
    - 助手消息：铺满整栏的低对比度背景板。**一轮 Agent run 一张卡**：卡内按
      模型消息分段（``_AssistantSegment``，段间细分隔线），思考块与工具 chip
      归属各段；思考块默认折叠，工具步骤与动作栏（复制 / Fork）在卡级共享，
      动作栏整轮收敛后才出现。
    - 错误：靠左的暖红提示块。

    按钮只外发 message_id，业务逻辑（分叉）在应用层。
    """

    edit_clicked = Signal(str)  # message_id
    fork_clicked = Signal(str)  # message_id：保留起点回复，不触发生成
    regenerate_clicked = Signal(str)  # message_id
    retry_clicked = Signal(str)  # message_id：重试发送失败的用户消息

    def __init__(
        self,
        text: str,
        *,
        role: str,
        message_id: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._role = role
        self._message_id = message_id
        self._raw_text = text  # 流式期间的纯文本累积（助手卡当前段的镜像），定稿时渲染
        self._column_width = 0
        self._column_layout: QVBoxLayout | None = None  # 仅助手消息：分段所在的栏
        self._thinking: _ThinkingBlock | None = None  # 最新一段的思考块（测试与旧接口用）
        # 助手卡的分段：一轮 run 里每条模型消息一段，全部落在这张卡里
        self._segments: list[_AssistantSegment] = []
        self._segment_dividers: list[QFrame] = []
        # 一轮 Agent run 的卡级状态行：提示整轮进行到哪一步，收敛后隐藏
        self._run_state: RunStateLabel | None = None
        self._run_error: str | None = None
        # 工具步骤视图已下沉到分段；这里保留字段语义：全局隐藏只影响展示，数据仍在
        self._parent_hides_steps = False
        self._tool_steps_progress = 1.0
        self._tool_steps_animation: QVariantAnimation | None = None
        self._actions: QWidget | None = None
        self._copy_btn: _ActionButton | None = None
        self._fork_btn: _ActionButton | None = None
        self._regenerate_btn: _ActionButton | None = None
        self._can_fork = True
        self._edit_btn: _ActionButton | None = None
        self._retry_btn: _ActionButton | None = None
        self._retry_animation: QPropertyAnimation | None = None
        self._send_animation: QPropertyAnimation | None = None
        self._content: QTextBrowser | QLabel | None = None
        if role == "assistant":
            self._build_assistant()
        else:
            self._build_bubble(text)
        self._sync_actions()

    def _build_assistant(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 先挂到消息行上，避免行已经显示后再插入一个暂时 hidden 的中间容器；
        # 这样定稿时动作按钮的可见状态也能立即生效，无需等下一轮事件循环。
        panel = QFrame(self)
        panel.setObjectName("assistantMessagePanel")
        panel.setStyleSheet(
            "QFrame#assistantMessagePanel"
            f" {{ {theme.bubble_stylesheet('assistant')} }}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(_BUBBLE_PAD_X, _BUBBLE_PAD_Y, _BUBBLE_PAD_X, _BUBBLE_PAD_Y)
        layout.setSpacing(8)
        self._column_layout = layout
        self._assistant_panel = panel
        outer.addWidget(panel)

        # 正文按分段挂进 panel（一段 = 一条模型消息，见 begin_segment）；
        # 状态行与动作栏留在卡级共享，位于所有分段之后。
        run_state = RunStateLabel()
        run_state.setVisible(False)
        self._run_state = run_state
        layout.addWidget(run_state)

        # 正文下方的动作栏：复制 Markdown 原文、Fork、重新生成。流式中的半截回复不提供动作。
        bar = QWidget()
        bar.setStyleSheet("background: transparent;")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(2)
        self._copy_btn = _ActionButton("copy", "复制", "复制")
        self._copy_btn.clicked.connect(self._copy_text)
        bar_layout.addWidget(self._copy_btn)
        self._fork_btn = _ActionButton(
            "fork", "Fork", "Fork：从这里分叉出新分支（保留这条回复）"
        )
        self._fork_btn.clicked.connect(self._on_fork)
        bar_layout.addWidget(self._fork_btn)
        self._regenerate_btn = _ActionButton(
            "retry", "重新生成", "重新生成这条回复（在新分支上生成，保留原回复）"
        )
        self._regenerate_btn.clicked.connect(self._on_regenerate)
        bar_layout.addWidget(self._regenerate_btn)
        bar_layout.addStretch(1)
        bar.setVisible(False)
        self._actions = bar
        layout.addWidget(bar)

    def _build_bubble(self, text: str) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        bubble = QFrame()
        bubble.setObjectName("messageBubble")
        # 声明同时落到气泡里的文字上（取它的文字色）；文字自己的样式清掉底色与边框
        bubble.setStyleSheet(
            f"QFrame#messageBubble, QFrame#messageBubble QLabel"
            f" {{ {theme.bubble_stylesheet(self._role)} }}"
        )
        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(_BUBBLE_PAD_X, _BUBBLE_PAD_Y, _BUBBLE_PAD_X, _BUBBLE_PAD_Y)
        self._content = self._make_label(text)
        inner.addWidget(self._content)
        self._bubble = bubble
        if self._role == "user":
            # 鼠标实际落在子气泡上时，父行不一定收到 Enter；直接监听气泡，
            # 保证编辑按钮在所有平台与 Qt 版本下都稳定出现。
            bubble.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
            bubble.setMouseTracking(True)
            self._content.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
            self._content.setMouseTracking(True)
            bubble.installEventFilter(self)
            self._content.installEventFilter(self)
            edit = _ActionButton("edit", "编辑", "编辑（自动分叉为新分支）")
            edit.clicked.connect(self._on_edit)
            policy = edit.sizePolicy()
            policy.setRetainSizeWhenHidden(True)  # 悬停显隐时气泡不挪动
            edit.setSizePolicy(policy)
            edit.setVisible(False)
            self._edit_btn = edit
            retry = _ActionButton("retry", "重试", "在当前分支重试，不创建新分支")
            retry.clicked.connect(self._on_retry)
            retry.setVisible(False)
            self._retry_btn = retry
            outer.addStretch(1)
            outer.addWidget(retry, 0, Qt.AlignmentFlag.AlignVCenter)
            outer.addWidget(edit, 0, Qt.AlignmentFlag.AlignBottom)
            # 气泡上方留一格给已发送附件的缩略图（有附件时才填）
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(6)
            column.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
            self._bubble_column = column
            outer.addLayout(column)
        else:
            outer.addWidget(bubble)
            outer.addStretch(1)

    def set_attachments(self, items: list[SentAttachment]) -> None:
        """在用户气泡上方挂已发送附件的缩略图。"""
        column = getattr(self, "_bubble_column", None)
        if column is None or not items:
            return
        column.insertWidget(0, SentAttachmentStrip(items))

    def _sync_actions(self) -> None:
        """完整回复定稿且有 ID 后，才显示 Fork 与重新生成。"""
        for button in (self._fork_btn, self._regenerate_btn):
            if button is not None:
                button.setVisible(self._message_id is not None and self._can_fork)

    def set_fork_available(self, available: bool) -> None:
        self._can_fork = available
        self._sync_actions()

    # 悬停显示用户消息的编辑按钮
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched in (getattr(self, "_bubble", None), self._content)
            and event.type()
            in (
                QEvent.Type.Enter,
                QEvent.Type.HoverEnter,
                QEvent.Type.HoverMove,
                QEvent.Type.MouseMove,
            )
            and self._edit_btn is not None
            and self._message_id is not None
        ):
            self._edit_btn.setVisible(True)
        return super().eventFilter(watched, event)

    def enterEvent(self, event: QEnterEvent) -> None:
        if self._edit_btn is not None and self._message_id is not None:
            self._edit_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._edit_btn is not None:
            self._edit_btn.setVisible(False)
        super().leaveEvent(event)

    def _on_edit(self) -> None:
        if self._message_id is not None:
            self.edit_clicked.emit(self._message_id)

    def set_retry_available(self, available: bool) -> None:
        """失败或停止时露出重试按钮；需要 message_id 才能重试。"""
        if self._retry_btn is not None:
            self._retry_btn.setVisible(available and self._message_id is not None)

    def _on_retry(self) -> None:
        if self._message_id is not None:
            self.set_retry_available(False)
            self.retry_clicked.emit(self._message_id)

    def reset_for_retry(self) -> None:
        """保留回复栏，只清掉旧尝试的分段与动作状态。"""
        assert self._column_layout is not None
        self.cancel_retry_animation()
        self._settle_tool_steps_visibility()
        for segment in self._segments:
            segment.stop_composing()
        # 分段和分隔线在前，末尾两项是复用的状态行与动作栏。
        while self._column_layout.count() > 2:
            item = self._column_layout.takeAt(0)
            assert item is not None
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._segments.clear()
        self._segment_dividers.clear()
        self._content = None
        self._thinking = None
        self._raw_text = ""
        self._message_id = None
        self._run_error = None
        self.set_fork_available(False)
        if self._actions is not None:
            self._actions.hide()

    def animate_send(self) -> None:
        """One entrance for an accepted user message, independent of reply streaming."""
        self.cancel_send_animation()
        if (
            self._role != "user"
            or not self.isVisible()
            or not QApplication.isEffectEnabled(Qt.UIEffect.UI_General)
        ):
            return
        effect = MessageSendEffect(self)
        self.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"progress", self)
        animation.setDuration(_SEND_ENTER_MS)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(self.cancel_send_animation)
        self._send_animation = animation
        animation.start()

    def cancel_send_animation(self) -> None:
        if self._send_animation is None:
            return
        self._send_animation.stop()
        self._send_animation.deleteLater()
        self._send_animation = None
        self.setGraphicsEffect(None)  # type: ignore[arg-type]

    def hideEvent(self, event: QHideEvent) -> None:
        self.cancel_send_animation()
        self._settle_tool_steps_visibility()
        super().hideEvent(event)

    def animate_retry(self) -> None:
        """一次渐隐渐显，只作用于回复栏，不阻塞流式输出。"""
        self.cancel_retry_animation()
        effect = QGraphicsOpacityEffect(self._assistant_panel)
        effect.setOpacity(1.0)
        self._assistant_panel.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(_RETRY_FADE_MS)
        animation.setStartValue(1.0)
        animation.setKeyValueAt(0.36, 0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        animation.finished.connect(self.cancel_retry_animation)
        self._retry_animation = animation
        animation.start()

    def cancel_retry_animation(self) -> None:
        if self._retry_animation is None:
            return
        self._retry_animation.stop()
        self._retry_animation.deleteLater()
        self._retry_animation = None
        self._assistant_panel.setGraphicsEffect(None)  # type: ignore[arg-type]

    def _on_fork(self) -> None:
        if self._message_id is not None:
            self.fork_clicked.emit(self._message_id)

    def _on_regenerate(self) -> None:
        if self._message_id is not None:
            self.regenerate_clicked.emit(self._message_id)

    def _copy_text(self) -> None:
        """把整卡的 Markdown 原文放进剪贴板；按钮短暂换成对勾表示已复制。"""
        if self._role == "assistant":
            # 各段原文按空行连接：一段 = 一条模型消息
            text = "\n\n".join(
                segment.raw_text for segment in self._segments if segment.raw_text
            )
        else:
            text = self._raw_text
        QGuiApplication.clipboard().setText(text)
        if self._copy_btn is not None:
            self._copy_btn.flash("check", "已复制")

    # ----- 组件构造 -----

    def _make_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        # 用户输入与错误信息都按原样显示：不当 Markdown，也不把像 HTML 的内容当富文本
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet("background: transparent; border: none;")
        return label

    def _make_browser(self) -> QTextBrowser:
        browser = QTextBrowser()
        browser.setUndoRedoEnabled(False)
        browser.setFrameShape(QFrame.Shape.NoFrame)
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; }"
            "QTextBrowser QScrollBar:horizontal { height: 0px; width: 0px; margin: 0; }"
        )
        # 正文与思考块、工具步骤、动作栏左缘对齐
        browser.document().setDocumentMargin(0)
        browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 工具往返会留下空正文段。空浏览器加上主题圆角滚动条，会退化成一根
        # 没有语义的横条；这里直接拆掉横向滚动条，空段也先不占位。
        self._present_segment_body(browser, False)
        # 高度随内容自适应：documentSizeChanged 在文档完成排版后发出，
        # 比 contentsChanged 可靠（后者可能在定宽前触发，拿到错误高度）。
        # 多分段后每段一个文档，回调须指回各自的内容组件。
        browser.document().documentLayout().documentSizeChanged.connect(
            lambda size, b=browser: self._fit_browser_height(b, size)
        )
        return browser

    # ----- 访问器（测试与视图逻辑用） -----

    @property
    def label(self) -> QLabel | QTextBrowser | None:
        """内容组件。历史测试用 `.label` 取文本，保持这个名字。"""
        return self._content

    def content_text(self) -> str:
        """当前内容的纯文本形式（不论内部是 QLabel 还是 QTextBrowser）。

        助手卡按分段返回：各段渲染后的纯文本以空行连接。
        """
        if self._role == "assistant":
            return "\n\n".join(
                segment.content.toPlainText()
                for segment in self._segments
                if segment.content is not None and segment.content.toPlainText().strip()
            )
        assert self._content is not None
        if isinstance(self._content, QTextBrowser):
            return self._content.toPlainText()
        return self._content.text()

    # ----- 流式与定稿 -----

    def _latest_segment(self) -> _AssistantSegment | None:
        """最新分段；助手卡还没有分段时先建首段（用户/错误卡返回 None）。

        所有「往卡里写内容」的入口（流式增量、定稿、思考、工具步骤）都走这里，
        保证空卡在第一次有内容时自然出现第一段。
        """
        if self._role != "assistant":
            return None
        if not self._segments:
            self.begin_segment()
        return self._segments[-1]

    def begin_segment(self) -> None:
        """开启一段新的模型消息。同一轮 run 的后续消息**并入本卡**，不另开卡片。

        末一段还完全空着（预立卡时先开好、尚未落字）时直接沿用：构思计时
        继续走，不另起一段、不画分隔线。
        """
        if self._role != "assistant":
            return
        assert self._column_layout is not None
        if self._segments and self._segments[-1].is_empty():
            self._content = self._segments[-1].content
            return
        layout = self._column_layout
        _set_tool_motion_spacing(layout, None)
        # 段与段之间的细分隔线插在状态行之前；首段之前不画
        if self._segments:
            divider = _make_segment_divider()
            layout.insertWidget(layout.count() - 2, divider)
            self._segment_dividers.append(divider)
        segment = _AssistantSegment()
        browser = self._make_browser()
        segment.column.addWidget(browser)
        segment.content = browser
        layout.insertWidget(layout.count() - 2, segment)
        # 运行中动态插入的组件要显式 show，状态才立即正确（见 _add_bubble 同因）
        segment.show()
        self._segments.append(segment)
        segment.start_composing()
        self._content = browser
        self._sync_segment_presentation()

    def append_delta(self, delta: str) -> None:
        """流式期间：纯文本追加，不做 Markdown 重排。"""
        if self._role != "assistant":
            self._raw_text += delta
            assert self._content is not None
            self._content.setText(self._raw_text)
            self._fit_bubble()
            return
        segment = self._latest_segment()
        assert segment is not None and segment.content is not None
        had_text = bool(segment.raw_text.strip())
        segment.raw_text += delta
        self._raw_text = segment.raw_text
        cursor = QTextCursor(segment.content.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)
        segment.stop_composing()  # 落字即构思结束
        if not had_text:
            self._sync_segment_presentation()

    def finalize_segment(
        self, text: str, message_id: str | None = None, *, html: str | None = None
    ) -> None:
        """定稿一段模型消息（渲染 Markdown）。

        ``message_id`` 记录在该段上；整卡的 Fork 点随最新一段前移（一轮 run 的
        分叉自然落在它最后一条消息上）。动作栏不受影响——整轮收敛（``mark_final``）
        前保持隐藏。
        """
        segment = self._latest_segment()
        if segment is None:
            return
        segment.raw_text = text
        self._raw_text = text
        assert segment.content is not None
        segment.content.setHtml(html if html is not None else markdown_render.render(text))
        segment.stop_composing()
        self._sync_segment_presentation()
        if message_id is not None:
            segment.message_id = message_id
            self._message_id = message_id
            self._sync_actions()

    def finalize(self, text: str, *, reveal_actions: bool = True) -> None:
        """定稿（单段兼容入口：历史渲染与未分段的直接定稿）。"""
        if self._role == "assistant":
            self.finalize_segment(text)
            if reveal_actions:
                self.mark_final()
            return
        self._raw_text = text
        assert self._content is not None
        self._content.setText(text)
        self._fit_bubble()

    def set_run_state(self, text: str) -> None:
        """显示这张卡在当前 Agent run 中所处的阶段（卡级状态行）。"""
        if self._run_state is None:
            return
        if self._run_state.text() != text:
            self._run_state.setText(text)
        self._run_state.set_animated(True)
        self._run_state.setVisible(True)

    def set_run_error(self, message: str) -> None:
        """失败属于当前回复，直接复用状态行，不另建错误气泡。"""
        self._run_error = message
        self.set_run_state(f"⚠ {message}")
        if self._run_state is not None:
            self._run_state.set_animated(False)
        self.set_fork_available(False)
        for segment in self._segments:
            segment.stop_composing()

    def mark_final(self) -> None:
        """整轮 run 收敛后，这张卡才成为可操作的最终回复。"""
        if self._run_state is not None:
            self._run_state.setVisible(self._run_error is not None)
        if self._actions is not None:
            self._actions.setVisible(self.has_content())
        for segment in self._segments:
            segment.stop_composing()
        self._sync_segment_presentation()

    def has_content(self) -> bool:
        """这张卡是否出现过任何可见内容（正文 / 思考 / 工具步骤）。

        整轮收敛时据此撤掉没等到任何内容的预立空卡。
        """
        if self._role != "assistant":
            return bool(self._raw_text.strip())
        return any(not segment.is_empty() for segment in self._segments)

    def _ensure_thinking(self) -> _ThinkingBlock:
        """首个思考 delta 到来时才建折叠块，放在当前段正文上方（构思计时之下）。"""
        segment = self._latest_segment()
        assert segment is not None
        if segment.thinking is None:
            block = _ThinkingBlock()
            # 构思计时挂在本段最上方，思考块插在它之下
            segment.column.insertWidget(
                segment.column.indexOf(segment.hint) + 1 if segment.hint is not None else 0, block
            )
            segment.thinking = block
        self._thinking = segment.thinking
        return segment.thinking

    def append_thinking(self, delta: str) -> None:
        """追加思考内容。"""
        if self._role != "assistant":
            return
        if not delta:
            return
        block = self._ensure_thinking()
        had_content = block.has_content()
        block.append(delta)
        if not had_content and block.has_content():
            self._sync_segment_presentation()

    def set_thinking(self, text: str) -> None:
        """设置完整思考内容（加载历史时），落到最新一段。"""
        if self._role != "assistant" or not text.strip():
            return
        self._ensure_thinking().set_text(text)
        self._sync_segment_presentation()

    @property
    def _tool_steps_view(self) -> QWidget | None:
        """最新一段的工具步骤视图（兼容旧访问口径：§三.2 的测试按行读）。"""
        return self._segments[-1].tool_steps_view if self._segments else None

    def note_tool_step(
        self, name: str, tool_call_id: str, *, is_error: bool, phase: str,
        step: ToolStep | None = None,
    ) -> None:
        """按调用 ID 更新所属分段，直接使用协调器的完整审计记录。"""
        call_key = step.tool_call_id if step is not None else tool_call_id or name
        segment = next(
            (part for part in self._segments
             if any(item.tool_call_id == call_key for item in part.live_tool_steps)),
            None,
        )
        if segment is None:
            segment = self._latest_segment()
        if segment is None:
            return
        previous = next((item for item in segment.live_tool_steps
                         if item.tool_call_id == call_key), None)
        if step is None:
            step = previous or ToolStep(tool_call_id=call_key, name=name)
            if phase != "start":
                step = finish(step, is_error=is_error)
        for index, item in enumerate(segment.live_tool_steps):
            if item.tool_call_id == call_key:
                segment.live_tool_steps[index] = step
                break
        else:
            segment.live_tool_steps.append(step)
        if phase == "start":
            segment.stop_composing()
        self._set_segment_steps(segment, tuple(segment.live_tool_steps))

    def set_tool_steps(self, steps: tuple[ToolStep, ...]) -> None:
        """给最新一段挂工具步骤（§三.2）。重复调用会替换。"""
        segment = self._latest_segment()
        if segment is None or not steps:
            return
        self._set_segment_steps(segment, steps)

    def set_segment_tool_steps(self, message_id: str | None, steps: tuple[ToolStep, ...]) -> None:
        """历史重载：按 message_id 把工具步骤挂回它所属的那一段。"""
        if self._role != "assistant" or not steps:
            return
        if message_id is not None:
            for segment in self._segments:
                if segment.message_id == message_id:
                    self._set_segment_steps(segment, steps)
                    return
        self.set_tool_steps(steps)  # 没有匹配的段 id：退回最新一段

    def _set_segment_steps(self, segment: _AssistantSegment, steps: tuple[ToolStep, ...]) -> None:
        """就地更新工具步骤，保持展开状态和所在布局位置。"""
        segment.live_tool_steps = list(steps)
        if segment.tool_steps_view is not None:
            segment.tool_steps_view.update_steps(steps)
        else:
            view = make_tool_steps(steps)
            if view is not None:
                segment.tool_steps_view = view
                segment.column.addWidget(view)
        self._sync_segment_presentation()

    def set_tool_steps_visible(self, visible: bool, *, animate: bool = True) -> None:
        """工具区滑入/滑出并淡化，背景板高度和段间距同步过渡。"""
        if self._parent_hides_steps == (not visible):
            return
        self._parent_hides_steps = not visible
        if (
            not animate
            or not self.isVisible()
            or not any(segment.tool_steps_view is not None for segment in self._segments)
            or not QApplication.isEffectEnabled(Qt.UIEffect.UI_General)
        ):
            self._settle_tool_steps_visibility()
            return
        if self._tool_steps_animation is None:
            animation = QVariantAnimation(self)
            animation.setDuration(_TOOL_STEPS_FADE_MS)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            animation.valueChanged.connect(self._set_tool_steps_progress)
            animation.finished.connect(self._settle_tool_steps_visibility)
            self._tool_steps_animation = animation
        animation = self._tool_steps_animation
        animation.setDirection(
            QAbstractAnimation.Direction.Forward
            if visible else QAbstractAnimation.Direction.Backward
        )
        # 同一时间线反向，快速切换时透明度和缓动进度都不跳变。
        if animation.state() == QAbstractAnimation.State.Stopped:
            animation.start()
        elif animation.state() == QAbstractAnimation.State.Paused:
            animation.resume()
        self._sync_segment_presentation()

    def _set_tool_steps_progress(self, progress: float) -> None:
        self._tool_steps_progress = progress
        self._sync_tool_steps_motion()

    def _sync_tool_steps_motion(self) -> None:
        animating = (
            self._tool_steps_animation is not None
            and self._tool_steps_animation.state() != QAbstractAnimation.State.Stopped
        )
        progress = self._tool_steps_progress
        weights: dict[QWidget, float] = {}
        for segment in self._segments:
            tools = segment.tool_steps_view
            if tools is not None:
                tools.set_reveal_progress(progress)
            _set_tool_motion_spacing(
                segment.column, {tools: progress} if animating and tools is not None
                else {} if animating else None,
            )
            weights[segment] = 1.0 if segment._tool_motion_has_body else progress
        for divider in self._segment_dividers:
            divider.setFixedHeight(round(progress) if animating else 1)
            weights[divider] = progress
        if self._column_layout is not None:
            _set_tool_motion_spacing(self._column_layout, weights if animating else None)
        targets: list[QWidget] = [
            segment.tool_steps_view for segment in self._segments
            if segment.tool_steps_view is not None
        ]
        targets.extend(self._segment_dividers)
        for widget in targets:
            effect = widget.graphicsEffect()
            if animating:
                if not isinstance(effect, QGraphicsOpacityEffect):
                    effect = QGraphicsOpacityEffect(widget)
                    widget.setGraphicsEffect(effect)
                effect.setOpacity(self._tool_steps_progress)
            elif effect is not None:
                # 静止时不保留离屏合成效果，避免滚动/流式更新持续承担绘图开销。
                widget.setGraphicsEffect(None)  # type: ignore[arg-type]

    def _settle_tool_steps_visibility(self) -> None:
        if self._tool_steps_animation is not None:
            self._tool_steps_animation.stop()
        self._tool_steps_progress = float(not self._parent_hides_steps)
        self._sync_segment_presentation()

    def _sync_segment_presentation(self) -> None:
        """隐藏工具时收起纯工具分段和段间分割线；空正文浏览器不占位。"""
        previous_visible = False
        # 淡出期间保持纯工具段可见，避免父段提前隐藏而截断子元素动画。
        tools_visible = not self._parent_hides_steps or self._tool_steps_progress > 0.0
        for index, segment in enumerate(self._segments):
            has_text = bool(segment.raw_text.strip())
            has_thinking = segment.thinking is not None and segment.thinking.has_content()
            is_composing = segment.hint is not None and not segment.hint.isHidden()
            segment._tool_motion_has_body = has_text or has_thinking or is_composing
            has_visible_tools = tools_visible and bool(segment.live_tool_steps)
            if segment.tool_steps_view is not None:
                segment.tool_steps_view.setVisible(tools_visible)
            if segment.content is not None:
                self._present_segment_body(segment.content, has_text)
            segment_visible = has_text or has_thinking or is_composing or has_visible_tools
            segment.setVisible(segment_visible)

            if index:
                divider = self._segment_dividers[index - 1]
                divider.setVisible(
                    tools_visible and previous_visible and segment_visible
                )
            previous_visible = previous_visible or segment_visible
        for leftover in self._segment_dividers[max(0, len(self._segments) - 1):]:
            leftover.hide()
        self._sync_tool_steps_motion()

    def set_message_id(self, message_id: str) -> None:
        """定稿后补挂 message_id（流式行创建时还没有 ID），Fork 随之可用。"""
        if self._message_id is not None:
            return
        self._message_id = message_id
        self._sync_actions()

    # ----- 尺寸 -----

    def set_column_width(self, width: int) -> None:
        """消息栏宽度变了（视口缩放）：用户气泡与错误提示据此重算宽度。"""
        if width == self._column_width:
            return
        self._column_width = width
        self._fit_bubble()

    def _fit_bubble(self) -> None:
        """气泡宽度随文字收窄：取最长一行的自然宽度，超过上限才折行。

        折行 QLabel 自己估的建议宽度偏窄（按版面比例估，不会尽量摊开），
        所以宽度在这里按文字实际宽度算好后定死，高度交给 heightForWidth。
        """
        label = self._content
        if not isinstance(label, QLabel) or self._column_width <= 0:
            return
        ratio = _USER_BUBBLE_RATIO if self._role == "user" else 1.0
        limit = max(1, int(self._column_width * ratio) - 2 * _BUBBLE_PAD_X)
        label.ensurePolished()  # 先套上样式表的字体，量出来的宽度才准
        metrics = label.fontMetrics()
        natural = max(metrics.horizontalAdvance(line) for line in label.text().split("\n"))
        label.setFixedWidth(min(natural + 2, limit))

    def _suppress_body_scrollbar(self, browser: QTextBrowser) -> None:
        """主题圆角滚动条在空段里会退化成一根横条，这里直接拆掉。"""
        if browser.horizontalScrollBarPolicy() is not Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar = browser.horizontalScrollBar()
        if bar.maximum() != 0 or bar.height() != 0 or not bar.isHidden():
            bar.setRange(0, 0)
            bar.hide()
            bar.setFixedHeight(0)

    def _present_segment_body(self, browser: QTextBrowser, has_text: bool) -> None:
        """空正文不占位；有正文时只显示文档本身。"""
        self._suppress_body_scrollbar(browser)
        if not has_text:
            if browser.height() != 0 or not browser.isHidden():
                browser.setFixedHeight(0)
                browser.hide()
                self._invalidate_browser_layout(browser)
            return
        if browser.isHidden():
            browser.show()

    def _fit_browser_height(self, browser: QTextBrowser, size: QSizeF) -> None:
        """正文高度随文档自适应，空文档和横向滚动条都不占位。"""
        has_text = not browser.document().isEmpty()
        if not has_text:
            self._present_segment_body(browser, False)
            return
        height = math.ceil(size.height())
        if height <= 0:
            self._present_segment_body(browser, True)
            return
        if browser.height() != height:
            browser.setFixedHeight(height)
            self._invalidate_browser_layout(browser)
        self._present_segment_body(browser, True)

    def _invalidate_browser_layout(self, browser: QTextBrowser) -> None:
        # 固定高度变了要立即作废父级布局的尺寸缓存：正文套进分段容器后，
        # 外层布局读到缓存的旧 sizeHint 会晚一轮事件才长高，流式期间
        # 「贴底跟随 / 上翻阅读」就会读到中间态的滚动范围。显式失效让
        # 同一次布局激活就拿到新高度。
        parent = browser.parentWidget()
        if parent is not None:
            layout = parent.layout()
            if layout is not None:
                layout.invalidate()
            parent.updateGeometry()


class _BackdropHost(QWidget):
    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        view = self.parent()
        # QScrollArea reparents its widget to the viewport.
        while view is not None and not isinstance(view, ChatView):
            view = view.parent()
        engine = getattr(view, "_backdrop_engine", None)
        if engine is not None and engine.active:
            painter = QPainter(self)
            engine.paint(
                self,
                painter,
                tint=getattr(self, "_backdrop_tint", "#00000000"),
                radius=getattr(self, "_backdrop_radius", 0.0),
            )
            painter.end()


class _ContextUsageRing(QWidget):
    """输入框右侧的上下文窗口占用圆环。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._percent: float | None = None
        self._display = "上下文占用暂不可用"
        self.setFixedSize(28, 28)
        self.setToolTip(self._display)

    @property
    def percent(self) -> float | None:
        return self._percent

    def set_usage(self, percent: float | None, display: str) -> None:
        self._percent = None if percent is None else max(0.0, min(100.0, percent))
        self._display = display or "上下文占用暂不可用"
        self.setToolTip(self._display)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(5.0, 5.0, 18.0, 18.0)
        painter.setPen(
            QPen(QColor(theme.BORDER), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.drawArc(rect, 0, 360 * 16)
        if self._percent is not None and self._percent > 0:
            color = theme.ACCENT
            if self._percent >= 90:
                color = theme.DANGER_TEXT
            elif self._percent >= 80:
                color = "#f59e0b"
            painter.setPen(
                QPen(QColor(color), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            )
            painter.drawArc(rect, 90 * 16, -int(360 * 16 * self._percent / 100.0))
        painter.end()


class _AddAttachmentButton(QPushButton):
    """不用字体字符，直接绘制加号，避免缺字时退化成方框。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("添加附件")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self.isEnabled():
            color = theme.TEXT_SECONDARY
        elif self.underMouse():
            color = theme.ACCENT
        else:
            color = theme.TEXT_PRIMARY
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center = self.rect().center()
        painter.drawLine(center.x() - 6, center.y(), center.x() + 6, center.y())
        painter.drawLine(center.x(), center.y() - 6, center.x(), center.y() + 6)
        painter.end()


_PERMISSION_PRESET_TEXT = {
    PermissionPreset.CHAT_ONLY: "仅聊天",
    PermissionPreset.READ_ONLY: "自由读取",
    PermissionPreset.FULL_ACCESS: "完全访问",
    PermissionPreset.CUSTOM: "自定义",
}


class _PermissionMenu(QMenu):
    """输入区权限上拉菜单；预设直接选择，自定义交给悬浮面板。"""

    preset_selected = Signal(str)
    custom_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 圆角外需透明；必须在首次创建原生窗口前启用，不能依赖绘制时补设。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._preset = PermissionPreset.READ_ONLY
        self._actions: dict[PermissionPreset, QAction] = {}
        self._anchor: QWidget | None = None
        self._motion = PopupMotion(Qt.UIEffect.UI_AnimateMenu, self)
        self.setStyleSheet(
            f"QMenu {{ padding: 4px; border-radius: {theme.RADIUS_MD}px; }}"
            "QMenu::item { padding: 7px 22px 7px 12px; }"
            f"QMenu::item:selected {{ background: {theme.BG_SURFACE_HOVER}; }}"
        )
        group = QActionGroup(self)
        group.setExclusive(True)
        rows = (
            (PermissionPreset.CHAT_ONLY, "仅聊天", "不允许模型调用工具"),
            (PermissionPreset.READ_ONLY, "自由读取", "可读工作区与网络，不可写入或执行命令"),
            (PermissionPreset.FULL_ACCESS, "完全访问", "允许读取、写入、终端与联网"),
        )
        for preset, title, detail in rows:
            action = self.addAction(f"{title}    {detail}")
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=preset: self._select_preset(value)
            )
            self._actions[preset] = action
        self.addSeparator()
        custom = self.addAction("自定义    选择允许的能力")
        custom.setCheckable(True)
        group.addAction(custom)
        custom.triggered.connect(self._request_custom)
        self._actions[PermissionPreset.CUSTOM] = custom
        self.set_preset(self._preset)
        install_popup_material(self, radius=theme.RADIUS_MD)

    def _select_preset(self, preset: PermissionPreset) -> None:
        self.set_preset(preset)
        self.preset_selected.emit(preset.value)

    def _request_custom(self) -> None:
        self.custom_requested.emit()
        QTimer.singleShot(0, lambda: self.set_preset(self._preset))

    def set_preset(self, preset: PermissionPreset) -> None:
        self._preset = preset
        for value, action in self._actions.items():
            action.setChecked(value is preset)

    def popup_above(self, anchor: QWidget) -> None:
        self._anchor = anchor
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        self.ensurePolished()
        self.adjustSize()
        origin = anchor.mapToGlobal(QPoint(0, 0))
        target = QPoint(origin.x(), origin.y() - self.sizeHint().height() - 4)
        with self._motion.native_effect_suppressed() as animated:
            self.popup(target)
        if animated:
            self._motion.reveal(self, upward=self.y() < origin.y())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        anchor = self._anchor
        if (
            anchor is not None
            and not self.rect().contains(event.position().toPoint())
            and anchor.rect().contains(anchor.mapFromGlobal(event.globalPosition().toPoint()))
        ):
            self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        super().mousePressEvent(event)

    def setVisible(self, visible: bool) -> None:
        if not visible and self.isVisible():
            self._motion.collapse()
        super().setVisible(visible)


class _PermissionButton(QPushButton):
    """当前权限档位；点击后从输入框上方展开。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("会话权限")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_preset(PermissionPreset.READ_ONLY)

    def set_preset(self, preset: PermissionPreset) -> None:
        self.setText(_PERMISSION_PRESET_TEXT[preset])
        self.setToolTip(f"会话权限：{_PERMISSION_PRESET_TEXT[preset]}（点击更改）")


class _JumpToBottomButton(QPushButton):
    """消息区上翻阅读时浮现的「回到底部」圆钮。自绘，按绘制时的主题取色。"""

    SIZE = 36

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("回到底部")
        self.setToolTip("回到底部")
        self._progress = 0.0  # 进出场进度：0 = 沉在下方且透明，1 = 就位且不透明

    @property
    def progress(self) -> float:
        return self._progress

    def set_progress(self, progress: float) -> None:
        self._progress = progress
        self.update()

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        hovered = self.underMouse()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._progress)
        painter.setPen(QPen(QColor(theme.ACCENT if hovered else theme.BORDER), 1.0))
        painter.setBrush(QColor(theme.BG_SURFACE_HOVER if hovered else theme.BG_ELEVATED))
        painter.drawEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        color = theme.ACCENT if hovered else theme.TEXT_PRIMARY
        painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center = QRectF(self.rect()).center()
        x, y = center.x(), center.y()
        painter.drawLine(QPointF(x, y - 6), QPointF(x, y + 5))
        painter.drawLine(QPointF(x - 5, y), QPointF(x, y + 5))
        painter.drawLine(QPointF(x + 5, y), QPointF(x, y + 5))
        painter.end()


class _CompressionOverlay(QWidget):
    """压缩进行中盖在组合输入框上的遮罩：整个输入框当进度条，中间写「正在压缩」。

    遮罩本身接住鼠标事件，下面的输入框点不到；输入框另外也会被禁用。
    右下角的停止键留在遮罩之上，用来中止这一次压缩。
    """

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._progress = 0.0  # 0–100
        self._label = "正在压缩"
        self._stop = QPushButton("■", self)
        self._stop.setFixedSize(36, 36)
        self._stop.setProperty("iconOnly", True)
        self._stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop.setToolTip("停止压缩")
        self._stop.clicked.connect(self.cancel_requested.emit)
        self.hide()

    @property
    def progress(self) -> float:
        return self._progress

    def set_progress(self, percent: float) -> None:
        self._progress = max(0.0, min(100.0, percent))
        self.update()

    def set_label(self, text: str) -> None:
        self._label = text
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._stop.move(
            self.width() - self._stop.width() - 12,
            self.height() - self._stop.height() - 10,
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(rect, _COMPOSER_RADIUS, _COMPOSER_RADIUS)
        painter.setClipPath(clip)
        mask = QColor(theme.BG_SURFACE)
        mask.setAlpha(215)
        painter.fillRect(rect, mask)
        fill = QColor(theme.ACCENT)
        fill.setAlpha(80)
        painter.fillRect(
            QRectF(rect.left(), rect.top(), rect.width() * self._progress / 100.0, rect.height()),
            fill,
        )
        painter.setClipping(False)
        font = painter.font()
        font.setPixelSize(theme.FS_BASE)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.drawText(
            self.rect().adjusted(48, 0, -48, 0), Qt.AlignmentFlag.AlignCenter, self._label
        )
        painter.end()


def _local_paths(mime: QMimeData) -> list[str]:
    if not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]


class _ComposerInput(QPlainTextEdit):
    """输入正文。Ctrl+V 时剪贴板里的文件 / 图片转成附件，其余照常粘贴成文本。

    判定顺序：本地文件（资源管理器里复制的文件）→ 有文本则按文本粘贴 →
    纯图片（截图、浏览器里「复制图片」）。Word / Excel 复制的内容同时带文本
    和位图，这时应当粘贴文本，所以文本优先于图片。
    """

    files_pasted = Signal(list)  # 本地路径
    image_pasted = Signal(bytes)  # PNG 字节
    image_paste_failed = Signal(str)
    image_paste_busy = Signal(bool)
    paste_generation: Callable[[], int]

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        if _local_paths(source) or source.hasImage():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        paths = _local_paths(source)
        if paths:
            self.files_pasted.emit(paths)
            return
        if source.hasText() and source.text():
            super().insertFromMimeData(source)
            return
        if source.hasImage():
            image = QImage(source.imageData())
            if not image.isNull():
                from limbowave.ui.background_tasks import BackgroundTasks

                def encode() -> bytes:
                    from io import BytesIO

                    from PIL import Image

                    # Convert the independent QImage snapshot off-thread, then encode
                    # raw pixels without involving Qt widgets or its image-writer plugins.
                    pixels = image.convertToFormat(QImage.Format.Format_RGBA8888)
                    converted = Image.frombytes(
                        "RGBA", (pixels.width(), pixels.height()), bytes(pixels.constBits()),
                        "raw", "RGBA", pixels.bytesPerLine(),
                    )
                    output = BytesIO()
                    converted.save(output, format="PNG")
                    return output.getvalue()

                generation = getattr(self, "paste_generation", lambda: 0)()
                self.image_paste_busy.emit(True)

                def ready(raw: bytes) -> None:
                    if generation == getattr(self, "paste_generation", lambda: 0)():
                        self.image_pasted.emit(raw)
                    self.image_paste_busy.emit(False)

                def failed(exc: Exception) -> None:
                    self.image_paste_busy.emit(False)
                    self.image_paste_failed.emit(str(exc))

                jobs = BackgroundTasks(self)
                jobs.submit(encode, ready, failed)
            return
        super().insertFromMimeData(source)


class _ComposerFrame(QFrame):
    """Zcode 风格组合输入框，并负责文件拖放反馈。"""

    files_dropped = Signal(list)
    height_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag_active = False
        self._backdrop: BackdropEngine | None = None
        self._backdrop_tint = "#00000000"
        self._backdrop_radius = 0.0
        self.setAcceptDrops(True)
        self.setObjectName("composer")
        self._drop_hint = QLabel("松开以添加附件", self)
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._drop_hint.setStyleSheet(
            f"background: {theme.card_surface()}; color: {theme.TEXT_PRIMARY};"
            f" border: 1px dashed {theme.ACCENT}; border-radius: 14px;"
            f" font-size: {theme.FS_SMALL}px; font-weight: 600;"
        )
        self._drop_hint.hide()
        self.compression_overlay = _CompressionOverlay(self)
        self._refresh_style()

    @property
    def drag_active(self) -> bool:
        return self._drag_active

    def _set_drag_active(self, active: bool) -> None:
        if self._drag_active == active:
            return
        self._drag_active = active
        self._drop_hint.setVisible(active)
        if active:
            self._drop_hint.raise_()
        self._refresh_style()

    def _refresh_style(self) -> None:
        border = theme.ACCENT if self._drag_active else theme.BORDER
        surface = "transparent" if self._backdrop is not None else theme.card_surface()
        self.setStyleSheet(
            f"QFrame#composer {{ background: {surface}; border: 1px solid {border};"
            f" border-radius: {_COMPOSER_RADIUS}px; }}"
        )

    def set_backdrop(
        self, engine: BackdropEngine | None, *, tint: str = "#00000000", radius: float = 0.0
    ) -> None:
        """有背景图时，输入框自己画一块模糊 + 着色的背景板（周围保持背景图原样）。"""
        self._backdrop = engine
        self._backdrop_tint = tint
        self._backdrop_radius = radius
        self._refresh_style()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        engine = self._backdrop
        if engine is not None and engine.active:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(
                QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                _COMPOSER_RADIUS,
                _COMPOSER_RADIUS,
            )
            painter.setClipPath(clip)
            engine.paint(self, painter, tint=self._backdrop_tint, radius=self._backdrop_radius)
            painter.end()
        # 边框画在背景板之上
        super().paintEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._drop_hint.setGeometry(self.rect().adjusted(7, 7, -7, -7))
        self.compression_overlay.setGeometry(self.rect())
        self.height_changed.emit(event.size().height())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            self._set_drag_active(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = _local_paths(event.mimeData())
        self._set_drag_active(False)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()


def _composer_span(available: int, panel: int, progress: float) -> tuple[int, int]:
    """输入框在输入框行里的宽度与左侧偏移。

    ``panel`` 是右侧并排高级栏占的宽度（含间距），``progress`` 是它的展开进度。
    展开时输入框与高级栏并排、整体居中；收起时同宽的输入框单独居中。
    对话区窄到放不下时，收起态的输入框放宽到 ``_COMPOSER_MIN_WIDTH``，不白白让位。
    """
    expanded = max(0, min(_COMPOSER_MAX_WIDTH, available - panel))
    collapsed = max(expanded, min(available, _COMPOSER_MIN_WIDTH))
    width = round(collapsed + (expanded - collapsed) * progress)
    group = width + round(panel * progress)
    return width, max(0, (available - group) // 2)


class _ComposerRowLayout(QLayout):
    """输入框所在的一行：输入框限宽居中，高级栏展开时向左让出右侧并排的位置。

    让位只影响摆放，不计入最小尺寸：宽对话区里两侧留白很大，
    若算进最小尺寸，会反过来撑住窗口、缩不回去。
    """

    def __init__(self) -> None:
        super().__init__()
        self._item: QLayoutItem | None = None
        self._panel = 0
        self._progress = 0.0

    def set_side_panel(self, width: int, progress: float) -> None:
        """设置右侧并排高级栏的宽度（含间距）与展开进度，并立即重排。"""
        self._panel = width
        self._progress = progress
        if not self.geometry().isEmpty():
            self.setGeometry(self.geometry())

    def addItem(self, item: QLayoutItem) -> None:
        self._item = item

    def count(self) -> int:
        return 0 if self._item is None else 1

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._item if index == 0 else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if index != 0:
            return None
        item, self._item = self._item, None
        return item

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation.Horizontal

    def sizeHint(self) -> QSize:
        return self._with_margins(self._item.sizeHint() if self._item else QSize(0, 0))

    def minimumSize(self) -> QSize:
        return self._with_margins(self._item.minimumSize() if self._item else QSize(0, 0))

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        if self._item is None:
            return
        area = self.contentsRect()
        width, offset = _composer_span(area.width(), self._panel, self._progress)
        # The composer width is a layout decision, not a content size hint. In
        # particular, attachment chips can report a very wide minimum size;
        # honoring it here lets them push the composer past its configured cap.
        width = min(area.width(), width)
        offset = min(offset, area.width() - width)
        self._item.setGeometry(QRect(area.x() + offset, area.y(), width, area.height()))

    def _with_margins(self, size: QSize) -> QSize:
        margins = self.contentsMargins()
        return QSize(
            size.width() + margins.left() + margins.right(),
            size.height() + margins.top() + margins.bottom(),
        )


class ChatView(QWidget):
    """最小聊天视图。通过信号表达意图，经方法接收展示更新。"""

    message_submitted = Signal(str)
    stop_requested = Signal()
    # 分支动作（Task 3.2）：只外发 message_id，业务在应用层；分支切换入口在左栏
    edit_message_requested = Signal(str)  # message_id
    edit_submitted = Signal(str, str)  # message_id, 新文本（附件在附件栏里）
    fork_requested = Signal(str)  # message_id
    regenerate_requested = Signal(str)  # message_id
    retry_requested = Signal(str)  # message_id：重试发送失败的用户消息
    # 压缩（Phase 5）：长按压缩按钮确认后外发（业务在应用层）
    compress_requested = Signal()
    files_dropped = Signal(list)  # 拖放或 Ctrl+V 粘贴的本地文件
    image_pasted = Signal(bytes)  # Ctrl+V 粘贴的剪贴板图片（PNG 字节）
    logical_model_changed = Signal(str)
    model_site_selected = Signal(str, str)
    permission_preset_changed = Signal(str)
    permission_custom_requested = Signal()
    # 工具步骤一键隐藏（§三.2）：只切展示，不删数据
    tool_steps_toggle_requested = Signal()
    # 输入框停靠：空会话居中，有内容后落到底部。第二个参数表示是否同时展开高级栏；
    # 加载历史只改变布局，不应覆盖用户当前的展开 / 收起状态。
    docked_changed = Signal(bool, bool)
    advanced_toggle_requested = Signal()  # 输入框右上角的高级栏开关被点击
    # 高级栏展开进度（0 = 收起，1 = 展开），动画中逐帧发出；上层据此摆放高级栏
    advanced_progress_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self._read_only = read_only
        self._docked = False
        self._advanced_expanded = False
        self._dock_progress = 0.0  # 0 = 居中，1 = 停靠底部
        self._dock_anim: QVariantAnimation | None = None
        self._advanced_progress = 0.0  # 0 = 收起，1 = 展开
        self._advanced_anim: QVariantAnimation | None = None
        self._side_panel_width = 0  # 输入框右侧并排高级栏占的宽度（含间距）
        self._shadow_painted = QRect()  # 上次画输入框阴影的范围，输入框挪动时连同旧位置一起重画
        self._stream_row: _BubbleRow | None = None
        self._run_rows: list[_BubbleRow] = []
        self._run_tail: _BubbleRow | None = None
        self._active_tool_calls: set[str] = set()
        self._rows: list[_BubbleRow] = []
        self._column_width = 0  # 消息栏宽度，跟视口走（见 _fit_column）
        self._tool_steps_hidden = False
        self._execution_mode = "builtin"
        self._usage_action = "none"
        self._available = True
        self._pending_pastes = 0
        self._history_loading = False
        self._history_loading_overlay_requested = False
        self._busy = False
        self._status_text = "就绪"
        self._permission_preset = PermissionPreset.READ_ONLY
        self._compressing = False
        self._compression_start = 0.0
        self._compression_anim: QVariantAnimation | None = None
        self._compression_timer = QTimer(self)
        self._compression_timer.setInterval(_COMPRESSION_TICK_MS)
        self._compression_timer.timeout.connect(self._drain_compression)
        # 压缩的思考过程悬浮窗：首个思考 delta 到来才弹出；用户关掉后本轮不再弹
        self._thinking_panel: CompressionThinkingPanel | None = None
        self._thinking_panel_dismissed = False
        # 长会话滞性化（Task 0.2）：完整历史 + 当前已物化的起点
        self._full_history: list[HistoryEntry] = []
        self._history_offset = 0
        # 用户消息 id → 已发送附件（由上层注入）；没有注入时不显示缩略图
        self._attachment_resolver: Callable[[str], list[SentAttachment]] | None = None
        self._history_transition: QParallelAnimationGroup | None = None
        self._history_opacity: QGraphicsOpacityEffect | None = None
        self._history_transition_generation = 0
        self._history_rest_pos: QPoint | None = None
        self._build()
        if read_only:
            self.set_available(False)
            self._composer.setEnabled(False)
            self._input.setPlaceholderText("另一会话正在生成；可切回查看进度，完成后即可在这里继续")
        self._fit_column()

    def set_backdrop_engine(
        self,
        engine: BackdropEngine | None,
        *,
        tint: str = "#00000000",
        radius: float = 0.0,
    ) -> None:
        """有背景图时整个对话区透出背景图；模糊 + 着色的背景板只给输入框。"""
        self._backdrop_engine = engine
        self._transcript_host.setStyleSheet(
            "background: transparent;" if engine else f"background: {theme.BG_APP};"
        )
        self._scroll.viewport().setStyleSheet("background: transparent;" if engine else "")
        self._composer.set_backdrop(engine, tint=tint, radius=radius)
        self._transcript_host.update()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        engine: BackdropEngine | None = getattr(self, "_backdrop_engine", None)
        if engine is not None and engine.active:
            # 输入框行两侧与消息区、底部垫块是同一张背景图（不着色），看不出分层
            engine.paint(self, painter, tint="#00000000", radius=0.0)
        self._paint_composer_shadow(painter)
        painter.end()

    def _composer_shadow_bounds(self) -> QRect:
        return soft_shadow.shadow_bounds(self._composer.geometry())

    def _paint_composer_shadow(self, painter: QPainter) -> None:
        """输入框下方一圈很淡的投影：逐层外扩的圆角矩形叠出由深到浅的晕染。"""
        soft_shadow.paint_soft_shadow(
            painter,
            QRectF(self._composer.geometry()),
            _COMPOSER_RADIUS,
            QRectF(self.rect()),
        )

    # ---------- 布局 ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 分支切换在左栏：双击会话行展开分支（见 sidebar）

        # 消息区
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # 对话页与每段 Markdown 正文都不显示横向滚动条。
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transcript_host = _BackdropHost()
        self._transcript_host.setStyleSheet(f"background: {theme.BG_APP};")
        self._transcript = QVBoxLayout(self._transcript_host)
        # 左右留白由 _fit_column 按视口宽度算：消息栏居中，两侧至少 _MESSAGE_SIDE_MIN
        self._transcript.setContentsMargins(
            _MESSAGE_SIDE_MIN, 24, _MESSAGE_SIDE_MIN, _TRANSCRIPT_BOTTOM
        )
        self._transcript.setSpacing(_MESSAGE_SPACING)
        self._transcript.addStretch(1)
        self._scroll.setWidget(self._transcript_host)
        root.addWidget(self._scroll, 1)
        self._history_loading_overlay = HistoryLoadingOverlay(self._scroll.viewport())
        # 贴底跟随：用户停在底部时流式输出自动跟随；一旦往上滚就不再强拉回底部，
        # 滚回底部后恢复跟随。内容增高靠 rangeChanged 跟进（布局是延迟的，
        # 追加内容当下读到的 maximum() 还是旧值）。
        self._stick_to_bottom = True
        # 上翻阅读时浮在消息区底部中央；挂在视口上，不随内容滚动。
        self._jump_btn = _JumpToBottomButton(self._scroll.viewport())
        self._jump_btn.clicked.connect(lambda: self._scroll_to_bottom(force=True))
        self._jump_btn.hide()
        self._jump_shown = False
        self._jump_anim: QVariantAnimation | None = None
        scroll_bar = self._scroll.verticalScrollBar()
        # Qt may emit valueChanged before rangeChanged while deferred rich-text
        # layout grows the document. Keep the previous range so that "at the
        # old bottom" is not mistaken for a user scroll away from the bottom.
        self._scroll_maximum = scroll_bar.maximum()
        scroll_bar.valueChanged.connect(self._on_scroll_value_changed)
        scroll_bar.rangeChanged.connect(self._on_scroll_range_changed)

        # Zcode 风格组合输入框：附件、逻辑模型、上下文圆环和发送合并到单一容器。
        # 输入框限宽居中；高级栏展开时并排在它右侧（高级栏本身由上层摆放）。
        self._composer_row = _ComposerRowLayout()
        self._composer_row.setContentsMargins(
            _COMPOSER_WRAP_SIDE, 10, _COMPOSER_WRAP_SIDE, _COMPOSER_WRAP_BOTTOM
        )
        self._composer = _ComposerFrame()
        self._composer.files_dropped.connect(self.files_dropped.emit)
        self._composer.compression_overlay.cancel_requested.connect(self.stop_requested.emit)
        composer = QVBoxLayout(self._composer)
        composer.setContentsMargins(14, 10, 12, 10)
        composer.setSpacing(6)

        # 顶部一行：左侧附件条 + 正文，右上角是高级栏开关（原地切换展开/收起）。
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(6)
        top.addLayout(text_column, 1)
        self._advanced_btn = QPushButton()
        self._advanced_btn.setFixedHeight(24)
        self._advanced_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._advanced_btn.clicked.connect(self.advanced_toggle_requested.emit)
        top.addWidget(self._advanced_btn, 0, Qt.AlignmentFlag.AlignTop)
        self._refresh_advanced_btn()

        # 编辑已发送消息时的提示条：输入框与附件栏借给这次编辑，发送即分叉
        self._editing_id: str | None = None
        self._edit_banner = QWidget()
        banner_layout = QHBoxLayout(self._edit_banner)
        banner_layout.setContentsMargins(0, 0, 0, 0)
        banner_layout.setSpacing(6)
        banner_label = QLabel("正在编辑已发送的消息，发送后将分叉为新分支")
        banner_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"
            " background: transparent;"
        )
        banner_layout.addWidget(banner_label)
        banner_layout.addStretch(1)
        cancel_edit = QPushButton("取消编辑")
        cancel_edit.setProperty("flat", True)
        cancel_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_edit.clicked.connect(self.cancel_edit)
        banner_layout.addWidget(cancel_edit)
        self._edit_banner.setVisible(False)
        text_column.addWidget(self._edit_banner)

        self._attachment_bar = AttachmentBar()
        text_column.addWidget(self._attachment_bar)

        self._input = _ComposerInput()
        self._input.files_pasted.connect(self.files_dropped.emit)
        self._input.image_pasted.connect(self.image_pasted.emit)
        self._input.image_paste_failed.connect(self.add_error)
        self._input.image_paste_busy.connect(self._on_image_paste_busy)
        attachment_ref = weakref.ref(self._attachment_bar)
        self._input.paste_generation = lambda: (
            bar.generation if (bar := attachment_ref()) is not None else -1
        )
        self._input.setPlaceholderText("提出后续修改要求")
        self._input.setMinimumHeight(54)
        self._input.setMaximumHeight(120)
        self._input.setTabChangesFocus(True)
        # 文件拖放统一交给组合输入框；否则 QTextEdit 会把本地路径插成文本。
        self._input.setAcceptDrops(False)
        self._input.setFrameShape(QFrame.Shape.NoFrame)
        self._input.setStyleSheet(
            f"background: transparent; color: {theme.TEXT_PRIMARY}; border: none;"
            f" font-size: {theme.FS_BASE}px;"
        )
        self._input.installEventFilter(self)
        # 视口尺寸变化时重新摆放「回到底部」按钮，输入框挪动时重画它的阴影
        # （都在 _input 之后装，eventFilter 会读它）
        self._scroll.viewport().installEventFilter(self)
        self._composer.installEventFilter(self)
        self._input.textChanged.connect(self._sync_send_button)
        text_column.addWidget(self._input)
        composer.addLayout(top)

        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self._attach_btn = _AddAttachmentButton()
        self._attach_btn.setFixedSize(30, 30)
        self._attach_btn.setProperty("flat", True)
        self._attach_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT_PRIMARY};"
            f" border-radius: {theme.RADIUS_MD}px; padding: 0; }}"
            # 悬浮底色须写进自身样式表：自身规则无条件压过全局 QSS 的 [flat]:hover
            f"QPushButton:hover {{ background: {theme.tool_hover_surface()};"
            f" color: {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_SECONDARY}; }}"
        )
        self._attach_btn.setToolTip("添加附件，也可以把文件拖到输入框")
        # 点击弹出上拉菜单选择来源，而不是直接打开文件选择器
        bar = self._attachment_bar
        self._attach_menu = AttachmentMenu(self)
        self._attach_menu.images_requested.connect(bar.attach_images_requested.emit)
        self._attach_menu.documents_requested.connect(bar.attach_documents_requested.emit)
        self._attach_menu.folder_requested.connect(bar.attach_folder_requested.emit)
        self._attach_menu.snippet_requested.connect(bar.attach_snippet_requested.emit)
        self._attach_btn.clicked.connect(lambda: self._attach_menu.popup_above(self._attach_btn))
        tools.addWidget(self._attach_btn)

        self._permission_btn = _PermissionButton()
        self._permission_btn.setFixedHeight(30)
        self._permission_btn.setProperty("flat", True)
        self._permission_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FS_SMALL}px; padding: 2px 6px;"
            f" border-radius: {theme.RADIUS_MD}px; }}"
            f"QPushButton:hover {{ background: {theme.tool_hover_surface()};"
            f" color: {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_SECONDARY}; }}"
        )
        self._permission_menu = _PermissionMenu(self)
        self._permission_menu.preset_selected.connect(self.permission_preset_changed.emit)
        self._permission_menu.custom_requested.connect(self.permission_custom_requested.emit)
        self._permission_btn.clicked.connect(
            lambda: self._permission_menu.popup_above(self._permission_btn)
        )
        tools.addWidget(self._permission_btn)

        self._compress_btn = CompressButton()
        self._compress_btn.setEnabled(False)
        self._compress_btn.confirmed.connect(self.compress_requested.emit)
        self._compress_btn.setVisible(False)
        tools.addWidget(self._compress_btn)
        tools.addStretch(1)

        self._usage_bar = _ContextUsageRing()
        tools.addWidget(self._usage_bar)
        self._logical_model = ModelSelector()
        self._logical_model.setMinimumContentsLength(12)
        self._logical_model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._logical_model.addItem("逻辑模型")
        self._logical_model.setToolTip("按厂牌选择逻辑模型；右键模型显示名选择站点")
        # 静止时无框；悬停/展开时由 theme_effects 的边框层渐显边框。
        self._logical_model.setProperty("comboFramelessAtRest", True)
        self._logical_model.setStyleSheet(
            f"QComboBox {{ background: transparent; color: {theme.TEXT_PRIMARY};"
            f" font-size: {theme.FS_SMALL}px; border: none; padding: 3px 18px 3px 4px; }}"
            "QComboBox::drop-down { background: transparent; border: none; width: 16px; }"
            "QComboBox::down-arrow { image: none; }"
            f"QComboBox QAbstractItemView {{ background: {theme.BG_SURFACE};"
            f" color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER};"
            f" selection-background-color: {theme.BG_SURFACE_HOVER}; }}"
        )
        self._logical_model.currentIndexChanged.connect(self._on_logical_model_changed)
        self._logical_model.model_site_selected.connect(self.model_site_selected.emit)
        tools.addWidget(self._logical_model)

        self._stop_btn = QPushButton("■")
        self._stop_btn.setFixedSize(36, 36)
        self._stop_btn.setProperty("iconOnly", True)
        self._stop_btn.setToolTip("停止生成")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        tools.addWidget(self._stop_btn)

        self._send_btn = QPushButton("↑")
        self._send_btn.setFixedSize(36, 36)
        self._send_btn.setProperty("accent", True)
        self._send_btn.setProperty("iconOnly", True)
        self._send_btn.setDefault(True)
        self._send_btn.setToolTip("发送（Ctrl+Enter）")
        self._send_btn.clicked.connect(self._on_send)
        tools.addWidget(self._send_btn)
        composer.addLayout(tools)
        self._composer_row.addWidget(self._composer)
        root.addLayout(self._composer_row)
        # 空会话时底部垫一块可变高度的空白，把输入框抬到对话区正中。
        # 这块空白属于对话区：和消息区一样直接透出背景图，不铺输入栏的模糊底板。
        self._composer_lift = _BackdropHost()
        self._composer_lift.setFixedHeight(0)
        root.addWidget(self._composer_lift)
        self._composer.height_changed.connect(lambda _height: self._apply_lift())
        self._sync_send_button()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """输入框的 Ctrl+Enter 发送（Enter 换行）。

        占位文本一直承诺这个快捷键，但此前**从未实现**——真缺陷。
        """
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_column()
            self._place_jump_button()
        if watched is self._composer and event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            # 阴影画在输入框外面，跟着挪动时旧位置和新位置都要重画
            bounds = self._composer_shadow_bounds()
            self.update(self._shadow_painted.united(bounds))
            self._shadow_painted = bounds
        if watched is self._input and event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if (
                key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and key_event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self._on_send()
                return True  # 已处理，不要再插入换行
        return super().eventFilter(watched, event)

    def _on_image_paste_busy(self, busy: bool) -> None:
        self._pending_pastes = max(0, self._pending_pastes + (1 if busy else -1))
        self._sync_send_button()

    def _on_send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text or self._history_loading or self._pending_pastes > 0:
            return
        self._input.clear()
        self._set_docked(True)
        editing_id = self._editing_id
        if editing_id is not None:
            # 附件栏留给上层装配载荷后再清空（与普通发送一致）
            self._set_editing(None)
            self.edit_submitted.emit(editing_id, text)
            return
        self.message_submitted.emit(text)

    # ---------- 编辑已发送的消息 ----------

    @property
    def editing_message_id(self) -> str | None:
        return self._editing_id

    def begin_edit(self, message_id: str, text: str) -> None:
        """把原文放进输入框进入编辑态；附件栏由上层预先填好原附件，可照常增删。"""
        self._set_editing(message_id)
        self._input.setPlainText(text)
        cursor = self._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._input.setTextCursor(cursor)
        self._input.setFocus()

    def cancel_edit(self) -> None:
        """放弃编辑：清掉借用的输入框与附件栏。"""
        if self._editing_id is None:
            return
        self._set_editing(None)
        self._input.clear()
        self._attachment_bar.clear()

    def _set_editing(self, message_id: str | None) -> None:
        self._editing_id = message_id
        self._edit_banner.setVisible(message_id is not None)

    def _sync_send_button(self) -> None:
        """发送按钮只在可用、空闲且确有文本时点亮。"""
        has_text = bool(self._input.toPlainText().strip())
        self._send_btn.setEnabled(
            self._available and not self._busy and not self._compressing
            and not self._history_loading and not self._pending_pastes and has_text
        )

    def _on_logical_model_changed(self, index: int) -> None:
        if index < 0 or self._logical_model.signalsBlocked():
            return
        model_id = self._logical_model.currentData()
        if isinstance(model_id, str) and model_id:
            self.logical_model_changed.emit(model_id)

    # ---------- 展示更新（由控制器驱动）----------

    @property
    def attachments(self) -> AttachmentBar:
        """附件栏（供上层接线：添加/移除/读取待发送附件）。"""
        return self._attachment_bar

    @property
    def permission_preset(self) -> PermissionPreset:
        return self._permission_preset

    @property
    def permission_anchor(self) -> QWidget:
        return self._permission_btn

    def set_permission_preset(self, preset: PermissionPreset | str) -> None:
        """回显当前会话权限档位，不反向触发用户意图信号。"""
        try:
            value = preset if isinstance(preset, PermissionPreset) else PermissionPreset(preset)
        except ValueError:
            value = PermissionPreset.READ_ONLY
        self._permission_preset = value
        self._permission_btn.set_preset(value)
        self._permission_menu.set_preset(value)

    @property
    def execution_mode(self) -> str:
        """当前执行模式（"builtin" / "terminal"）。"""
        return self._execution_mode

    def set_execution_mode(self, mode: str) -> None:
        """记录执行模式（"builtin" / "terminal"）。

        模式的可见提示在右侧高级栏（点「模式」时显示），这里只存状态。
        """
        self._execution_mode = mode if mode in ("builtin", "terminal") else "builtin"

    @property
    def usage_action(self) -> str:
        """当前占用对应的动作（none/hint/preview/block）。发送前据此判断。"""
        return self._usage_action

    def set_usage_action(self, action: str) -> None:
        """记录占用动作（none/hint/preview/block）。"""
        self._usage_action = action

    def set_context_usage(self, percent: float | None, display: str) -> None:
        """更新输入框右侧的上下文窗口圆环。"""
        self._usage_bar.set_usage(percent, display)

    def set_logical_model(self, logical_model: str) -> None:
        """选中当前逻辑模型，实际站点留在高级栏。"""
        index = self._logical_model.findData(logical_model)
        if index < 0 and logical_model:
            self._logical_model.addItem(logical_model, logical_model)
            index = self._logical_model.count() - 1
        self._logical_model.blockSignals(True)
        self._logical_model.setCurrentIndex(max(0, index))
        self._logical_model.blockSignals(False)

    def set_logical_models(
        self, models: list[tuple[str, str]], current: str = "", *,
        sites: Mapping[str, Sequence[ModelSite]] | None = None,
        current_endpoint: str = "", overridden: bool = False,
    ) -> None:
        """刷新按厂牌分组的逻辑模型；站点只出现在模型行的右键菜单中。"""
        self._logical_model.set_sites(
            sites or {}, current_model=current,
            current_endpoint=current_endpoint, overridden=overridden,
        )
        # 按 id 去重；同名条目以先出现的为准，itemData 始终是 id
        seen: dict[str, str] = {}
        for model_id, name in models:
            if model_id and model_id not in seen:
                seen[model_id] = name or model_id
        self._logical_model.blockSignals(True)
        self._logical_model.clear()
        if not seen:
            self._logical_model.addItem("逻辑模型")
            self._logical_model.setEnabled(False)
        else:
            for model_id, name in seen.items():
                self._logical_model.addItem(name, model_id)
            self._logical_model.setEnabled(True)
            index = self._logical_model.findData(current)
            self._logical_model.setCurrentIndex(index if index >= 0 else 0)
        self._logical_model.blockSignals(False)

    def set_compress_available(self, available: bool) -> None:
        self._compress_btn.setEnabled(available)

    def note_tool_step(
        self, name: str, tool_call_id: str, *, is_error: bool, phase: str,
        step: ToolStep | None = None,
    ) -> None:
        """实时累积工具步骤到本轮最近的消息，并同步就地阶段提示。"""
        if self._run_tail is None or self._compression_owns_stream():
            return
        call_key = tool_call_id or name
        self._run_tail.note_tool_step(
            name, tool_call_id, is_error=is_error, phase=phase, step=step
        )
        if phase == "start":
            self._active_tool_calls.add(call_key)
            self._run_tail.set_run_state(f"● 正在运行工具：{name}…")
        else:
            self._active_tool_calls.discard(call_key)
            if self._active_tool_calls:
                self._run_tail.set_run_state("● 仍有工具正在运行…")
            else:
                outcome = "执行失败" if is_error else "执行完成"
                self._run_tail.set_run_state(f"● 工具{outcome}，Agent 正在继续…")

    def set_tool_steps_visible(self, visible: bool) -> None:
        """全局隐藏/显示工具步骤（§三.2 的一键隐藏）。**只影响展示**。"""
        if self._tool_steps_hidden == (not visible):
            return
        self._tool_steps_hidden = not visible
        viewport = self._scroll.viewport()
        bounds = viewport.rect().adjusted(0, -64, 0, 64)
        # Snapshot before geometry changes; offscreen rows need no compositor/timeline.
        animated = {
            row for row in self._rows
            if row.isVisible() and bounds.intersects(
                QRect(row.mapTo(viewport, QPoint()), row.size())
            )
        }
        for row in self._rows:
            row.set_tool_steps_visible(visible, animate=row in animated)

    def set_available(self, available: bool, hint: str = "") -> None:
        self._available = available
        self._sync_input_enabled()
        if available:
            self.set_status("就绪")
        else:
            self.set_status(hint or "未配置可用的模型内核")

    def _sync_input_enabled(self) -> None:
        """输入框、附件与权限按钮：内核可用且不在压缩中才可操作。"""
        enabled = (
            self._available and not self._compressing and not self._history_loading
            and not self._read_only
        )
        self._input.setEnabled(enabled)
        self._attach_btn.setEnabled(enabled)
        self._permission_btn.setEnabled(enabled)
        self._sync_send_button()

    @property
    def history_loading(self) -> bool:
        return self._history_loading

    def set_history_loading(
        self, loading: bool, text: str = "正在加载会话…", *, show_overlay: bool = True
    ) -> None:
        """独立于模型 busy/available 的加载态；保留旧历史与草稿，不创建助手卡片。"""
        was_requested = self._history_loading_overlay_requested
        self._history_loading = loading
        self._history_loading_overlay_requested = loading and show_overlay
        if loading:
            self._history_loading_overlay.set_text(text)
        if self._history_loading_overlay_requested:
            self._set_docked(True, reveal_advanced=False)
        elif was_requested and not (self._rows or self._full_history):
            self._set_docked(False, reveal_advanced=False)
        self._sync_history_loading_overlay()
        self._transcript_host.setEnabled(not loading)
        self._composer.setEnabled(not loading and not self._read_only)
        self._sync_input_enabled()

    def _sync_history_loading_overlay(self) -> None:
        show = (
            self._history_loading_overlay_requested
            and self._docked
            and self._dock_anim is None
            and self._dock_progress == 1.0
        )
        if show:
            layout = self.layout()
            if layout is not None:
                layout.activate()
        self._history_loading_overlay.set_loading(show)

    @property
    def status_text(self) -> str:
        return self._status_text

    def set_status(self, text: str) -> None:
        """输入框上方的状态栏已移除；保留入口给上层现有调用，文本只记录不展示。"""
        self._status_text = text

    # ---------- 压缩进度（整个输入框当进度条） ----------

    @property
    def compressing(self) -> bool:
        return self._compressing

    @property
    def compression_progress(self) -> float:
        return self._composer.compression_overlay.progress

    def begin_compression(self, percent: float | None) -> None:
        """开始压缩：禁用输入框，盖上「正在压缩」遮罩。

        进度初值是当前上下文占窗口的百分比，之后每秒减少千分之三（按整条计）。
        """
        if self._compression_anim is not None:
            self._compression_anim.stop()
            self._compression_anim = None
        self._reset_thinking_panel()
        self._compressing = True
        self._compression_start = max(0.0, min(100.0, percent or 0.0))
        overlay = self._composer.compression_overlay
        overlay.set_label("正在压缩")
        overlay._stop.setEnabled(True)
        overlay.set_progress(self._compression_start)
        overlay.show()
        overlay.raise_()
        self._compression_timer.start()
        self._sync_input_enabled()

    def _drain_compression(self) -> None:
        overlay = self._composer.compression_overlay
        step = COMPRESSION_DRAIN_PER_SEC * _COMPRESSION_TICK_MS / 1000
        overlay.set_progress(overlay.progress - step)

    def note_compression_stopping(self) -> None:
        """用户已要求中止：先改文案并禁用停止键，等这一轮真正收尾再撤遮罩。"""
        overlay = self._composer.compression_overlay
        overlay.set_label("正在停止")
        overlay._stop.setEnabled(False)
        if self._thinking_panel is not None:
            self._thinking_panel.note_stopping()

    def finish_compression(self, percent: float | None, *, stopped: bool = False) -> None:
        """压缩结束：进度多退少补到压缩后占比，停一下再撤遮罩、恢复输入。

        ``percent`` 为 None（失败或占比不可知）时回到起点——原上下文没动。
        ``stopped`` 为真表示用户中止了这一轮，思考悬浮窗改记「压缩已停止」。
        """
        if not self._compressing:
            return
        if self._thinking_panel is not None:
            if stopped:
                self._thinking_panel.note_stopped()
            else:
                self._thinking_panel.note_finished()
        self._compression_timer.stop()
        overlay = self._composer.compression_overlay
        target = self._compression_start if percent is None else max(0.0, min(100.0, percent))
        anim = QVariantAnimation(self)
        anim.setStartValue(overlay.progress)
        anim.setEndValue(target)
        anim.setDuration(_COMPRESSION_SETTLE_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda value: overlay.set_progress(float(value)))
        anim.finished.connect(
            lambda: QTimer.singleShot(_COMPRESSION_HOLD_MS, lambda: self._end_compression(anim))
        )
        self._compression_anim = anim
        anim.start()

    def _end_compression(self, anim: QVariantAnimation) -> None:
        if self._compression_anim is not anim:
            return  # 期间又开始了新一轮压缩，这一轮的收尾作废
        self._compression_anim = None
        anim.deleteLater()
        self._composer.compression_overlay.hide()
        self._compressing = False
        self._sync_input_enabled()

    # ---------- 压缩的思考过程（单独的悬浮窗，不进气泡） ----------

    @property
    def compression_thinking_panel(self) -> CompressionThinkingPanel | None:
        """本轮压缩当前开着的思考悬浮窗。没弹出、或已被用户关掉时返回 None。"""
        return self._thinking_panel

    def _compression_owns_stream(self) -> bool:
        """眼下的助手流是否是压缩模型在输出。

        压缩期间输入框被遮罩，不会开始新一轮对话；但开始压缩时可能正有一条回复
        在流式（``busy``）——那条回复照旧进它自己的气泡，不被压缩接管。
        """
        return self._compressing and not self._busy

    def _show_compression_thinking(self, delta: str) -> None:
        if not delta or self._thinking_panel_dismissed:
            return
        if self._thinking_panel is None:
            self._thinking_panel = self._open_thinking_panel()
        self._thinking_panel.append(delta)

    def _open_thinking_panel(self) -> CompressionThinkingPanel:
        panel = CompressionThinkingPanel(self.window())
        panel.closed.connect(lambda: self._on_thinking_panel_closed(panel))
        panel.cancel_requested.connect(self.stop_requested.emit)
        panel.popup()
        return panel

    def _on_thinking_panel_closed(self, panel: CompressionThinkingPanel) -> None:
        if panel is self._thinking_panel:
            # 用户关掉的（关闭按钮 / Esc / 点外部）：本轮不再弹出
            self._thinking_panel = None
            self._thinking_panel_dismissed = True

    def _reset_thinking_panel(self) -> None:
        """新一轮压缩：收起上一轮还开着的悬浮窗，本轮从头来。"""
        panel, self._thinking_panel = self._thinking_panel, None
        self._thinking_panel_dismissed = False
        if panel is not None:
            panel.close_panel()

    def set_busy(self, busy: bool, *, reuse_row: _BubbleRow | None = None) -> None:
        was_busy = self._busy
        self._busy = busy
        if busy and not was_busy:
            self._run_rows.clear()
            self._run_tail = reuse_row
            if reuse_row is not None:
                self._run_rows.append(reuse_row)
            self._active_tool_calls.clear()
            # 一轮开始就先把本轮卡片和首段立起来：回复将要出现的位置立即给出
            # 「正在构思……N秒」，模型首 token 之前的等待不再是一片空白；
            # 真正的 assistant_start 到来时沿用这段（begin_segment 会识别空段）。
            self.begin_assistant()
            if self._stream_row is not None:
                self._stream_row.begin_segment()
        elif not busy and was_busy:
            self._settle_assistant_run()
        self._stop_btn.setVisible(busy)
        self._stop_btn.setEnabled(busy)
        self._send_btn.setVisible(not busy)
        self._sync_send_button()

    def set_attachment_resolver(
        self, resolver: Callable[[str], list[SentAttachment]] | None
    ) -> None:
        """注入「用户消息 id → 已发送附件」的查询，用于在气泡上方画缩略图。"""
        self._attachment_resolver = resolver

    def refresh_sent_attachments(self, message_id: str, items: list[SentAttachment]) -> None:
        """Apply a late attachment DTO only to rows still displaying that message."""
        for row in self._rows:
            if row._role == "user" and row._message_id == message_id:
                row.set_attachments(items)

    def add_user_message(self, text: str, message_id: str | None = None) -> None:
        self._clear_retry()  # 新的一轮开始，之前的失败不再提供重试
        self._set_docked(True)
        row = self._add_bubble(text, role="user", message_id=message_id)
        row.animate_send()
        self._scroll_to_bottom(force=True)

    def retry_user_message(self, text: str, message_id: str, retry_of_message_id: str) -> None:
        """已接受的手动重试：复用一问一答的位置，不追加重发气泡。"""
        user = next((row for row in reversed(self._rows) if row._role == "user"), None)
        if user is not None and user._message_id == message_id:
            return  # 已应用的重试事件：不要追加气泡，也不要清掉已到达的新 token。
        if user is None or user._message_id != retry_of_message_id:
            # 过期事件或来源不在展示窗口：不能降级成新发送，也不能覆盖新一轮。
            return
        self._clear_retry()
        user._message_id = message_id
        tail = self._rows[self._rows.index(user) + 1 :]
        assistant = next((row for row in tail if row._role == "assistant"), None)
        # 同时撤掉本次失败的错误行及可能残留的独立提示。
        start = self._transcript.indexOf(user) + 1
        for index in reversed(range(start, self._transcript.count() - 1)):
            item = self._transcript.itemAt(index)
            assert item is not None
            widget = item.widget()
            if widget is None or widget is assistant:
                continue
            if isinstance(widget, _BubbleRow):
                self._drop_row(widget)
            else:
                self._transcript.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
        if assistant is not None:
            assistant.reset_for_retry()
        self.set_busy(True, reuse_row=assistant)
        if self._stream_row is not None:
            self._stream_row.animate_retry()
        self._scroll_to_bottom()

    def begin_assistant(self) -> None:
        if self._compression_owns_stream():
            return  # 压缩模型的输出不建气泡
        self._set_docked(True)
        if self._busy and self._run_tail is not None:
            # 同一轮 run 的下一条模型消息：并入已有卡片，长任务不碎成多张卡
            self._run_tail.begin_segment()
            self._stream_row = self._run_tail
        else:
            self._stream_row = self._add_bubble("", role="assistant")
            self._run_tail = self._stream_row
            self._run_rows.append(self._stream_row)
        if self._busy:
            self._run_tail.set_run_state("● Agent 正在思考下一步…")
        self._scroll_to_bottom()

    def append_assistant_delta(self, text: str) -> None:
        if self._compression_owns_stream():
            # 摘要正文留给压缩预览；思考悬浮窗只切到「正在生成摘要」
            if self._thinking_panel is not None:
                self._thinking_panel.note_summarizing()
            return
        if self._stream_row is None:
            self.begin_assistant()
        assert self._stream_row is not None
        self._stream_row.append_delta(text)
        if self._busy:
            self._stream_row.set_run_state("● 正在生成内容…")
        self._scroll_to_bottom()

    def append_thinking_delta(self, text: str) -> None:
        """思考流：追加到当前助手气泡的折叠思考块；压缩期间改进单独的悬浮窗。"""
        if self._compression_owns_stream():
            self._show_compression_thinking(text)
            return
        if self._stream_row is None:
            self.begin_assistant()
        assert self._stream_row is not None
        self._stream_row.append_thinking(text)
        if self._busy:
            self._stream_row.set_run_state("● Agent 正在继续思考…")
        self._scroll_to_bottom()

    def end_assistant(
        self,
        text: str,
        message_id: str | None = None,
        *,
        stop_reason: str | None = None,
        user_message_id: str | None = None,
    ) -> None:
        if self._compression_owns_stream():
            return
        if self._stream_row is None:
            if self._busy and self._run_tail is not None:
                # 未经流式（没有 begin）的模型消息同样并入当前 run 的卡片
                self._run_tail.begin_segment()
                row = self._run_tail
            else:
                row = self._add_bubble("", role="assistant")
        else:
            row = self._stream_row
        row.finalize_segment(text, message_id)
        row.set_fork_available(stop_reason not in {"aborted", "error"})
        if stop_reason in {"aborted", "error"} and user_message_id is not None:
            self.set_retry_available(user_message_id)
        if self._busy:
            row.set_run_state("● 这段内容已生成，Agent 仍在处理…")
        else:
            row.mark_final()
        self._stream_row = None
        self._scroll_to_bottom()

    def _settle_assistant_run(self) -> None:
        """整轮收敛：本轮的卡片露出动作栏、收起状态行。

        没等到任何内容的预立卡片（如发起即失败、尚无首 token 就中断）直接
        撤掉，不留空面板。
        """
        for row in list(self._run_rows):
            row.mark_final()
            if not row.has_content() and row._run_error is None:
                self._drop_row(row)
        self._run_rows.clear()
        self._run_tail = None
        self._active_tool_calls.clear()
        self._stream_row = None

    def _drop_row(self, row: _BubbleRow) -> None:
        """把一条（还没内容的）消息行从消息区撤下。"""
        row.cancel_retry_animation()
        for segment in row._segments:
            segment.stop_composing()
        if self._stream_row is row:
            self._stream_row = None
        if row in self._rows:
            self._rows.remove(row)
        self._transcript.removeWidget(row)
        # 先脱离子树再延迟删除，免得旧组件在删除前还画在原处
        row.setParent(None)
        row.deleteLater()

    def add_retry_notice(
        self, reason: str, *, attempt: int, delay_ms: int, max_attempts: int
    ) -> None:
        """自动重试在当前回复卡内更新，不追加提示行或保留旧尝试分段。"""
        self._clear_retry()  # 自动重试中，手动重试按钮先收起
        if self._run_tail is None:
            self.begin_assistant()
        row = self._run_tail
        if row is None:
            return  # 压缩占用流时不建立聊天气泡。
        # 只清当前回复之后的失败提示，不影响之前的会话历史。
        for failed_row in self._rows[self._rows.index(row) + 1 :]:
            if failed_row._role == "error":
                self._drop_row(failed_row)
        row.reset_for_retry()
        self._active_tool_calls.clear()
        self._stream_row = row
        row.begin_segment()
        delay = f"，{delay_ms / 1000:.1f}s 后重试" if delay_ms else ""
        row.set_run_state(f"↻ {reason}——正在第 {attempt}/{max_attempts} 次尝试{delay}")
        self._scroll_to_bottom()

    def add_error(self, message: str, *, retry_message_id: str | None = None) -> None:
        """运行失败原位显示；无关联请求的应用错误才使用独立提示。"""
        if retry_message_id is not None:
            user = next((row for row in reversed(self._rows) if row._role == "user"), None)
            if user is not None and user._message_id == retry_message_id:
                tail = self._rows[self._rows.index(user) + 1 :]
                assistant = next((row for row in tail if row._role == "assistant"), None)
                if assistant is not None:
                    assistant.set_run_error(message)
                    self.set_retry_available(retry_message_id)
                    self._scroll_to_bottom()
                    return
        self._add_bubble(f"⚠ {message}", role="error")
        if retry_message_id is not None:
            self.set_retry_available(retry_message_id)
        self._scroll_to_bottom()

    def set_retry_available(self, user_message_id: str) -> None:
        """在最近的用户消息旁提供原分支重试，不把部分回复当作 Fork。"""
        for row in reversed(self._rows):
            if row._role == "user":
                if row._message_id != user_message_id:
                    return
                row.set_retry_available(True)
                break
        else:
            return
        for row in reversed(self._rows):
            if row._role == "user":
                break
            if row._role == "assistant":
                row.set_fork_available(False)

    def _clear_retry(self) -> None:
        for row in self._rows:
            row.set_retry_available(False)

    def clear_transcript(self) -> None:
        """清空消息区与已加载的历史（切换会话前调用）。只影响展示。

        内部只清**组件与历史缓存**；``_render_window`` 复用同一个清理逻辑时
        不能顺手清掉 ``_full_history``（否则窗口重渲染会把自己清空）。
        """
        self.cancel_edit()  # 被编辑的消息不在新会话里
        self._cancel_history_transition()
        self._full_history = []
        self._history_offset = 0
        self._reset_rows()
        self._set_docked(False)

    def _reset_rows(self) -> None:
        """只清组件，不动历史缓存。"""
        self._stream_row = None
        self._run_rows.clear()
        self._run_tail = None
        self._active_tool_calls.clear()
        for row in self._rows:
            row.cancel_send_animation()
            row.cancel_retry_animation()
            for segment in row._segments:
                segment.stop_composing()
        self._rows.clear()
        while self._transcript.count() > 1:  # 保留末尾的 stretch
            item = self._transcript.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                # 先脱离子树再延迟删除：deleteLater 要等事件循环，
                # 期间组件仍会被 findChildren 找到（会读到已移除的旧行）
                widget.setParent(None)
                widget.deleteLater()

    def set_branch_actions_enabled(self, enabled: bool) -> None:
        enabled = enabled and not self._read_only
        for row in self._rows:
            for button in (row._fork_btn, row._regenerate_btn):
                if button is not None:
                    button.setEnabled(enabled)

    def apply_branch_history(self, messages: Sequence[HistoryEntry], anchor_id: str) -> bool:
        """Reuse the visible prefix on regeneration; never reread SQLite or rerender it."""
        anchor = next((row for row in self._rows if row._message_id == anchor_id), None)
        payload = list(messages)
        if anchor is None:
            return False
        ids = {item.message_id for item in payload}
        include_anchor = anchor_id in ids
        retained = self._rows[:self._rows.index(anchor) + int(include_anchor)]
        if any(row._message_id not in ids for row in retained) or (
            payload and (not retained or retained[-1]._message_id != payload[-1].message_id)
        ):
            # Caller must incrementally render a different paging/retry window.
            return False
        self._cancel_history_transition()
        self.cancel_edit()
        start = self._transcript.indexOf(anchor) + int(include_anchor)
        for index in reversed(range(start, self._transcript.count() - 1)):
            item = self._transcript.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, _BubbleRow):
                self._drop_row(widget)
            elif widget is not None:
                self._transcript.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
        self._full_history = payload
        self._history_offset = min(self._history_offset, len(payload))
        self._run_rows.clear()
        self._run_tail = None
        self._stream_row = None
        self._active_tool_calls.clear()
        self._clear_retry()
        self.sync_compression_markers(payload)
        self._set_docked(bool(payload), reveal_advanced=False)
        self._scroll_to_bottom(force=True)
        return True

    def sync_compression_markers(self, messages: Sequence[HistoryEntry]) -> None:
        """Refresh persisted boundaries without replacing bubbles or interrupting a stream."""
        for index in reversed(range(self._transcript.count() - 1)):
            item = self._transcript.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, CompressionDivider):
                self._transcript.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()
        self._full_history = list(messages)
        self._history_offset = min(self._history_offset, len(self._full_history))
        entries = {entry.message_id: entry for entry in messages if entry.compressions}
        for row in self._rows:
            entry = entries.get(row._message_id)
            if entry is not None:
                self._insert_compression_markers(entry, self._transcript.indexOf(row) + 1)

    def _insert_compression_markers(self, entry: HistoryEntry, index: int) -> None:
        for context in entry.compressions:
            divider = CompressionDivider(context)
            self._transcript.insertWidget(index, divider)
            divider.show()
            index += 1

    def load_history(
        self, messages: Sequence[HistoryEntry | tuple[str, str, str, str | None]]
    ) -> None:
        """渲染一段既有消息历史。

        **长会话滞性化**（Task 0.2：2,000 条消息要能接受地滚动）：
        每条消息一个富文本组件（含 markdown 的 QTextBrowser），全量物化会
        让长会话卡死。这里只物化**最近 ``HISTORY_PAGE`` 条**，更早的折叠成
        顶部一个「加载更早的消息」入口——点一次多放一页。

        **一轮 run 一卡**：``run_id`` 相同且相邻的助手条目渲染进同一张卡片
        （见 ``HistoryEntry``）。旧 4 元组载荷不带 run_id，行为不变。
        """
        self._cancel_history_transition()
        self._load_history_now(messages)

    @staticmethod
    def _history_entries(
        messages: Sequence[HistoryEntry | tuple[str, str, str, str | None]],
    ) -> list[HistoryEntry]:
        """兼容旧 4 元组与新具名载荷：统一成 HistoryEntry。"""
        return [
            item if isinstance(item, HistoryEntry) else HistoryEntry(*item) for item in messages
        ]

    def _load_history_now(
        self, messages: Sequence[HistoryEntry | tuple[str, str, str, str | None]]
    ) -> None:
        self._prepare_history_window(messages)
        self._render_window()
        self._scroll_to_bottom(force=True)

    def _prepare_history_window(
        self, messages: Sequence[HistoryEntry | tuple[str, str, str, str | None]]
    ) -> None:
        self.cancel_edit()  # 换了会话或分支，正在编辑的消息已不在眼前
        self._set_docked(
            bool(messages) or self._history_loading_overlay_requested, reveal_advanced=False
        )
        self._full_history = self._history_entries(messages)
        self._history_offset = self._align_window_start(
            max(0, len(self._full_history) - HISTORY_PAGE)
        )

    async def load_history_incrementally(
        self, messages: Sequence[HistoryEntry], *, rendered: Mapping[str, str] | None = None
    ) -> bool:
        """数据预取后分批创建 Qt 控件，每行/分段让出事件循环，加载动画可继续刷新。"""
        self._cancel_history_transition()
        generation = self._history_transition_generation
        self._prepare_history_window(messages)
        for _step in self._render_window_steps(rendered):
            await asyncio.sleep(0)
            if generation != self._history_transition_generation:
                return False  # 主题重绘/清空等更新已经接管视图，丢弃迟到的渲染。
        self._scroll_to_bottom(force=True)
        if self.isVisible():
            self._animate_history_in(generation, slide=False)
        return True

    def _align_window_start(self, offset: int) -> int:
        """翻页边界不切断 run：窗口起点落在某轮中间时，回退到该轮首条消息。

        这样整轮消息始终完整出现在窗口里；hidden 计数随之减少，「加载更早」
        的剩余条目仍然正确。run 不跨用户消息，所以回退一定在最近的用户消息处停住。
        """
        history = self._full_history
        while (
            0 < offset < len(history)
            and history[offset].run_id is not None
            and history[offset - 1].run_id == history[offset].run_id
        ):
            offset -= 1
        return offset

    def transition_history(
        self,
        messages: Sequence[HistoryEntry | tuple[str, str, str, str | None]],
        *,
        on_loaded: Callable[[], None] | None = None,
    ) -> None:
        """切换历史：已有内容先渐隐，新内容再渐显；空白进入时附带短距离上滑。"""
        self._history_transition_generation += 1
        generation = self._history_transition_generation
        had_content = bool(self._rows or self._full_history)
        payload = self._history_entries(messages)
        for row in self._rows:
            row.cancel_send_animation()
        self._stop_history_animation(normalize=True)
        if not self.isVisible():
            self._load_history_now(payload)
            if on_loaded is not None:
                on_loaded()
            return

        def replace_and_enter() -> None:
            if generation != self._history_transition_generation:
                return
            self._load_history_now(payload)
            if on_loaded is not None:
                on_loaded()
            self._ensure_history_opacity().setOpacity(0.0)
            QTimer.singleShot(
                0,
                lambda: self._animate_history_in(generation, slide=not had_content),
            )

        if had_content:
            self._animate_history_out(generation, replace_and_enter)
        else:
            replace_and_enter()

    def _animate_history_out(self, generation: int, finished: Callable[[], None]) -> None:
        effect = self._ensure_history_opacity()
        group = QParallelAnimationGroup(self)
        opacity = QPropertyAnimation(effect, b"opacity", group)
        opacity.setDuration(_HISTORY_FADE_OUT_MS)
        opacity.setStartValue(effect.opacity())
        opacity.setEndValue(0.0)
        opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        group.addAnimation(opacity)

        def done() -> None:
            if generation == self._history_transition_generation:
                finished()

        group.finished.connect(done)
        self._history_transition = group
        group.start()

    def _animate_history_in(self, generation: int, *, slide: bool) -> None:
        if generation != self._history_transition_generation:
            return
        rest = self._transcript_host.pos()
        self._history_rest_pos = rest
        start = rest + QPoint(0, _HISTORY_ENTER_OFFSET if slide else 0)
        self._transcript_host.move(start)
        effect = self._ensure_history_opacity()
        group = QParallelAnimationGroup(self)
        opacity = QPropertyAnimation(effect, b"opacity", group)
        opacity.setDuration(_HISTORY_FADE_IN_MS)
        opacity.setStartValue(0.0)
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        position = QPropertyAnimation(self._transcript_host, b"pos", group)
        position.setDuration(_HISTORY_FADE_IN_MS)
        position.setStartValue(start)
        position.setEndValue(rest)
        position.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(opacity)
        group.addAnimation(position)

        def done() -> None:
            if generation != self._history_transition_generation:
                return
            self._transcript_host.move(rest)
            self._history_rest_pos = None
            self._history_transition = None
            self._remove_history_opacity()

        group.finished.connect(done)
        self._history_transition = group
        group.start()

    def _cancel_history_transition(self) -> None:
        self._history_transition_generation += 1
        self._stop_history_animation(normalize=True)

    def _stop_history_animation(self, *, normalize: bool) -> None:
        if self._history_transition is not None:
            self._history_transition.stop()
            self._history_transition.deleteLater()
            self._history_transition = None
        if normalize:
            if self._history_rest_pos is not None:
                self._transcript_host.move(self._history_rest_pos)
                self._history_rest_pos = None
            self._remove_history_opacity()

    def _ensure_history_opacity(self) -> QGraphicsOpacityEffect:
        if self._history_opacity is None:
            effect = QGraphicsOpacityEffect(self._transcript_host)
            effect.setOpacity(1.0)
            self._transcript_host.setGraphicsEffect(effect)
            self._history_opacity = effect
        return self._history_opacity

    def _remove_history_opacity(self) -> None:
        if self._history_opacity is None:
            return
        # Qt 接受 nullptr 来卸载效果；PySide6 的类型存根漏掉了这个合法重载。
        self._transcript_host.setGraphicsEffect(None)  # type: ignore[arg-type]
        self._history_opacity = None

    def _render_window(self) -> None:
        for _step in self._render_window_steps():
            pass

    def _render_window_steps(self, rendered: Mapping[str, str] | None = None) -> Iterator[None]:
        """按当前窗口渲染：同步重绘和分批加载共享合卡逻辑。"""
        self._reset_rows()
        hidden = self._history_offset
        if hidden > 0:
            self._add_load_earlier(hidden)
        window = self._full_history[hidden:]
        index = 0
        while index < len(window):
            entry = window[index]
            if entry.role == "user":
                row = self._add_bubble("", role="user", message_id=entry.message_id)
                row.finalize(entry.content)
                row.set_retry_available(entry.retry_available)
                if entry.thinking:
                    row.set_thinking(entry.thinking)
                self._insert_compression_markers(entry, self._transcript.count() - 1)
                index += 1
                yield
                continue
            # 助手条目：向后收拢同 run 的消息，一并渲染进同一张卡
            run_id = entry.run_id
            count = 1
            while (
                run_id is not None
                and index + count < len(window)
                and window[index + count].run_id == run_id
                and not window[index + count - 1].compressions
            ):
                count += 1
            row = self._add_bubble("", role="assistant")
            for offset in range(count):
                item = window[index + offset]
                parts = item.segments or (AssistantMessageSegment(
                    content=item.content, thinking=item.thinking,
                    tool_call_ids=tuple(step.tool_call_id for step in item.tool_steps),
                ),)
                steps_by_id = {step.tool_call_id: step for step in item.tool_steps}
                for part in parts:
                    row.begin_segment()
                    row.finalize_segment(
                        part.content, item.message_id,
                        html=rendered.get(part.content) if rendered is not None else None,
                    )
                    if part.thinking:
                        row.set_thinking(part.thinking)
                    row.set_tool_steps(tuple(steps_by_id[key] for key in part.tool_call_ids
                                             if key in steps_by_id))
                    if not item.segments and item.tool_steps:
                        # 旧版只存了最终正文。无法恢复丢失的中间文本，但工具应在总结之前。
                        segment = row._segments[-1]
                        if segment.tool_steps_view is not None and segment.content is not None:
                            segment.column.insertWidget(
                                segment.column.indexOf(segment.content), segment.tool_steps_view
                            )
                    yield
                row.set_fork_available(item.can_fork)
            row.mark_final()
            self._insert_compression_markers(window[index + count - 1],
                                             self._transcript.count() - 1)
            index += count

    def _add_load_earlier(self, hidden: int) -> None:
        """顶部「加载更早的消息」入口。只影响展示——数据早就在内存里。"""
        button = QPushButton(f"加载更早的 {min(hidden, HISTORY_PAGE)} 条消息（还有 {hidden} 条）")
        button.setProperty("flat", True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self._load_earlier)
        self._transcript.insertWidget(self._transcript.count() - 1, button)

    def _load_earlier(self) -> None:
        self._history_offset = self._align_window_start(
            max(0, self._history_offset - HISTORY_PAGE)
        )
        self._render_window()

    # ---------- 高级栏开关 ----------

    @property
    def advanced_expanded(self) -> bool:
        return self._advanced_expanded

    @property
    def advanced_progress(self) -> float:
        """高级栏展开进度：0 = 收起，1 = 展开；动画中介于两者之间。"""
        return self._advanced_progress

    def set_side_panel_width(self, width: int) -> None:
        """输入框右侧并排高级栏占的宽度（含与输入框的间距）；展开时输入框为它让位。"""
        self._side_panel_width = max(0, width)
        self._composer_row.set_side_panel(self._side_panel_width, self._advanced_progress)

    def set_advanced_expanded(self, expanded: bool) -> None:
        """同步右上角开关，并把输入框平移到展开 / 收起后的位置（可见时带动画）。"""
        self._advanced_expanded = expanded
        self._refresh_advanced_btn()
        if self._advanced_anim is not None:
            self._advanced_anim.stop()
            self._advanced_anim = None
        target = 1.0 if expanded else 0.0
        if not self.isVisible() or self._advanced_progress == target:
            self._on_advanced_progress(target)
            return
        anim = QVariantAnimation(self)
        anim.setDuration(_ADVANCED_SLIDE_MS)
        anim.setStartValue(self._advanced_progress)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.valueChanged.connect(self._on_advanced_progress)
        anim.finished.connect(self._on_advanced_finished)
        self._advanced_anim = anim
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _on_advanced_progress(self, value: object) -> None:
        self._advanced_progress = float(cast(float, value))
        self._composer_row.set_side_panel(self._side_panel_width, self._advanced_progress)
        self.advanced_progress_changed.emit(self._advanced_progress)

    def _on_advanced_finished(self) -> None:
        self._advanced_anim = None

    def _refresh_advanced_btn(self) -> None:
        """箭头指向点击后高级栏的去向：收起时向右展开，展开时向左收回。"""
        expanded = self._advanced_expanded
        button = self._advanced_btn
        button.setText("高级 ⟨" if expanded else "高级 ⟩")
        button.setToolTip("收起高级栏" if expanded else "展开高级栏")
        color = theme.ACCENT if expanded else theme.TEXT_SECONDARY
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {color};"
            f" border: 1px solid {theme.BORDER}; border-radius: 12px; padding: 0 9px;"
            f" font-size: {theme.FS_SMALL}px; }}"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )

    # ---------- 输入框停靠 ----------

    @property
    def docked(self) -> bool:
        """输入框是否停靠在底部；False 为空会话的居中态。"""
        return self._docked

    @property
    def composer_lift(self) -> int:
        return self._composer_lift.height()

    def _set_docked(self, docked: bool, *, reveal_advanced: bool = True) -> None:
        if docked == self._docked:
            return
        self._docked = docked
        if self._dock_anim is not None:
            self._dock_anim.stop()
            self._dock_anim = None
        target = 1.0 if docked else 0.0
        if self.isVisible():
            anim = QVariantAnimation(self)
            anim.setDuration(_DOCK_MS)
            anim.setStartValue(self._dock_progress)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            anim.valueChanged.connect(self._on_dock_progress)
            anim.finished.connect(self._on_dock_finished)
            self._dock_anim = anim
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        else:
            self._on_dock_progress(target)
        self.docked_changed.emit(docked, reveal_advanced)
        self._sync_history_loading_overlay()

    def _on_dock_progress(self, value: object) -> None:
        self._dock_progress = float(cast(float, value))
        self._apply_lift()

    def _on_dock_finished(self) -> None:
        self._dock_anim = None
        self._sync_history_loading_overlay()
        if self._docked:
            self._scroll_to_bottom()

    def _apply_lift(self) -> None:
        """居中态让输入框中线对齐对话区中线；停靠态抬升为 0。"""
        centered = max(0, (self.height() - self._composer.height()) // 2 - _COMPOSER_WRAP_BOTTOM)
        lift = round(centered * (1.0 - self._dock_progress))
        if lift != self._composer_lift.height():
            self._composer_lift.setFixedHeight(lift)

    # ---------- 内部 ----------

    def _add_bubble(self, text: str, *, role: str, message_id: str | None = None) -> _BubbleRow:
        row = _BubbleRow(text, role=role, message_id=message_id)
        row.edit_clicked.connect(self.edit_message_requested)
        row.fork_clicked.connect(self.fork_requested)
        row.regenerate_clicked.connect(self.regenerate_requested)
        row.retry_clicked.connect(self.retry_requested)
        if self._read_only:
            for button in (row._edit_btn, row._retry_btn, row._fork_btn, row._regenerate_btn):
                if button is not None:
                    button.setEnabled(False)
        row.set_column_width(self._column_width)
        row.set_tool_steps_visible(not self._tool_steps_hidden)
        if role == "user" and message_id is not None and self._attachment_resolver is not None:
            row.set_attachments(self._attachment_resolver(message_id))
        self._rows.append(row)
        # 在 stretch 之前插入
        self._transcript.insertWidget(self._transcript.count() - 1, row)
        # 运行中的可见布局动态插入组件时，Qt 可能要到下一轮事件循环才自动 show；
        # 显式显示可让定稿后动作栏与背景板立即进入正确的可见状态。
        row.show()
        return row

    def _fit_column(self) -> None:
        """按视口宽度摆消息栏：最宽与输入框一致并居中，窄时两侧至少留 _MESSAGE_SIDE_MIN。"""
        width = self._scroll.viewport().width()
        side = max(_MESSAGE_SIDE_MIN, (width - _COMPOSER_MAX_WIDTH) // 2)
        margins = self._transcript.contentsMargins()
        if margins.left() != side or margins.right() != side:
            margins.setLeft(side)
            margins.setRight(side)
            self._transcript.setContentsMargins(margins)
        column = max(_MESSAGE_COLUMN_MIN, width - 2 * side)
        if column != self._column_width:
            self._column_width = column
            for row in self._rows:
                row.set_column_width(column)

    def _scroll_to_bottom(self, *, force: bool = False) -> None:
        """滚到底部。非 ``force`` 时只在贴底跟随状态下生效，用户上翻阅读时不打扰。"""
        if force:
            self._stick_to_bottom = True
        if self._stick_to_bottom:
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        self._sync_jump_button()

    def _on_scroll_value_changed(self, value: int) -> None:
        bar = self._scroll.verticalScrollBar()
        if (
            self._docked
            and self._dock_anim is not None
            and value < bar.maximum() - _STICK_THRESHOLD
        ):
            # Mark the user's intent before settling the dock layout. Finishing
            # the layout can synchronously grow the scroll range; if this flag
            # is cleared afterwards, rangeChanged wrongly snaps back to bottom.
            self._stick_to_bottom = False
            # 用户在输入框落底动画期间主动上翻时，先把停靠动画结算到终点；
            # 否则视口仍在变高，会覆盖回到底部按钮自身的上浮位移。
            self._dock_anim.stop()
            self._dock_anim = None
            self._on_dock_progress(1.0)
            layout = self.layout()
            if layout is not None:
                layout.activate()
            self._sync_history_loading_overlay()
            bar = self._scroll.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - _STICK_THRESHOLD
        self._sync_jump_button()

    def _on_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        bar = self._scroll.verticalScrollBar()
        previous_maximum = self._scroll_maximum
        self._scroll_maximum = maximum
        # Rich text and tool rows settle one event-loop turn after history is
        # inserted. If the viewport was at the previous bottom, follow the new
        # bottom even when valueChanged arrived first and temporarily cleared
        # _stick_to_bottom. A genuinely scrolled-up viewport remains anchored.
        was_at_previous_bottom = bar.value() >= previous_maximum - _STICK_THRESHOLD
        range_shrank_to_bottom = (
            maximum < previous_maximum
            and bar.value() >= maximum - _STICK_THRESHOLD
        )
        if self._stick_to_bottom or was_at_previous_bottom or range_shrank_to_bottom:
            self._stick_to_bottom = True
            bar.setValue(maximum)
        self._sync_jump_button()

    def _sync_jump_button(self) -> None:
        """只在离开底部时显示「回到底部」按钮：从下方浮出渐显，沉回下方渐隐。"""
        shown = not self._stick_to_bottom and self._scroll.verticalScrollBar().maximum() > 0
        if shown == self._jump_shown:
            return
        self._jump_shown = shown
        button = self._jump_btn
        # 退场途中不再接收点击，免得点到一个正在消失的按钮
        button.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not shown)
        if self._jump_anim is not None:
            self._jump_anim.stop()
            self._jump_anim = None
        if shown and not button.isVisible():
            self._place_jump_button()  # 先摆到起点，免得第一帧落在旧位置
            button.show()
            button.raise_()
        target = 1.0 if shown else 0.0
        if not self.isVisible():
            self._on_jump_progress(target)
            self._on_jump_anim_finished()
            return
        anim = QVariantAnimation(self)
        anim.setDuration(_JUMP_BUTTON_MS)
        anim.setStartValue(button.progress)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic if shown else QEasingCurve.Type.InCubic)
        anim.valueChanged.connect(self._on_jump_progress)
        anim.finished.connect(self._on_jump_anim_finished)
        self._jump_anim = anim
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _on_jump_progress(self, value: object) -> None:
        self._jump_btn.set_progress(float(cast(float, value)))
        self._place_jump_button()

    def _on_jump_anim_finished(self) -> None:
        self._jump_anim = None
        if not self._jump_shown:
            self._jump_btn.hide()

    def _place_jump_button(self) -> None:
        """底部居中，停在消息区可见底边上方；进出场时按进度下沉。"""
        viewport = self._scroll.viewport()
        size = _JumpToBottomButton.SIZE
        x = (viewport.width() - size) // 2
        y = viewport.height() - size - _JUMP_BUTTON_GAP
        sink = round((1.0 - self._jump_btn.progress) * _JUMP_BUTTON_RISE)
        self._jump_btn.move(x, max(0, y) + sink)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """高度变化时重算输入框居中态的抬升；消息栏宽度跟视口走（见 _fit_column）。"""
        super().resizeEvent(event)
        self._apply_lift()
