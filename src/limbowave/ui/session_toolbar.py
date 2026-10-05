"""右侧会话工具栏（Phase 10 / 设计计划 §三.1、Task 8.2/8.3）。

**高级栏**（约占可用宽度 15%，默认展开、可收起，窗口缩小时自动收起）：

- 实际调用站点（逻辑模型放在主输入框；这里是与「思考」同款的上拉下拉框，
  行序即回退顺序，当前站点打勾，切换即会话级覆盖）
- 思考强度（会话级，走内核 ``set_thinking_level``）
- 压缩入口（上下文占用放在主输入框圆环）。点击**不直接压缩**：行内其余按钮
  渐隐、压缩按钮滑到行首（复用模式/步骤提示的位移 + 渐隐编排），右侧出现
  滑动确认滑块；从左滑到最右才外发 ``compress_requested``，5 秒内没滑完按
  取消处理，确认态里再点一次压缩按钮也能取消

工具栏只展示与发意图；数据由上层经方法灌入。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QParallelAnimationGroup,
    QPersistentModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSequentialAnimationGroup,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QResizeEvent,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import soft_shadow, theme
from limbowave.ui.backdrop import BackdropEngine
from limbowave.ui.floating import FloatingPanel
from limbowave.ui.popup_motion import UpwardComboBox
from limbowave.ui.slide_confirm import SlideConfirm
from limbowave.ui.thinking_combo import HOLD_PROGRESS_ROLE, ThinkingComboBox

# 思考强度等级（合同 §1.2 确认的取值）
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

# 站点选择悬浮窗底部的固定说明（§四.3 的设计约束，不是可配置文案）
ROUTE_NOTE = "失败时不自动跨站点重发；是否切换由你决定。"


@dataclass(frozen=True)
class RouteCandidate:
    """路由候选行（§四.3）：bindings 的顺序就是回退顺序。

    ``current`` 行不可点（已在用）；``restore`` 行是会话覆盖激活时的默认绑定，
    点击外发**空串**——应用层用它清除覆盖、回到配置默认。
    展示一律用 ``endpoint_name``（站点显示名），``endpoint_id`` 只作选中值。
    """

    endpoint_id: str
    endpoint_name: str  # 站点显示名（EndpointConfig.name）
    model_id: str
    label: str  # 「默认」/「备用 N」
    current: bool = False
    override: bool = False  # 当前是否为会话级覆盖
    restore: bool = False


class _ThinkingLevelDelegate(QStyledItemDelegate):
    """仅为不可用的思考等级叠加浅灰蒙版，不改变其他弹层。"""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        row_option = QStyleOptionViewItem(option)
        if not enabled:
            row_option.state &= ~(
                QStyle.StateFlag.State_Enabled
                | QStyle.StateFlag.State_Selected
                | QStyle.StateFlag.State_MouseOver
            )
        super().paint(painter, row_option, index)
        if not enabled:
            painter.save()
            painter.fillRect(option.rect, QColor(210, 210, 210, 90))
            progress = index.data(HOLD_PROGRESS_ROLE)
            if isinstance(progress, (int, float)) and progress > 0:
                strip = QRect(option.rect)
                strip.setTop(strip.bottom() - 2)
                strip.setWidth(round(strip.width() * progress))
                painter.fillRect(strip, QColor(theme.ACCENT))
            painter.restore()


class _RouteComboBox(UpwardComboBox):
    """站点选择下拉框：弹层行的点击语义自己接管。

    Qt 对带勾选项的下拉弹层有既定怪癖：真实点击弹层行**不产生选中**
    （探针实测 currentIndexChanged 不触发、弹层直接收起，行文本与勾选块
    都一样）。所以不指望 currentIndexChanged，而是在弹层 viewport 上装
    过滤器，把「松开在某行上」直接映射成选中该候选。
    """

    candidate_picked = Signal(object)  # 被选中的候选行（QStandardItem）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view().viewport().installEventFilter(self)
        # 键盘路径（弹层里方向键 + 回车）也要能换站点
        self.activated.connect(self._pick)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.view().viewport()
            and isinstance(event, QMouseEvent)
            and event.type() == QEvent.Type.MouseButtonRelease
        ):
            row = self.view().indexAt(event.position().toPoint()).row()
            if row is not None and row >= 0:
                # 自己接管：吞掉默认处理（它不会产生选中），按选择收场
                self._pick(row)
                return True
        return super().eventFilter(watched, event)

    def _pick(self, row: int) -> None:
        model = self.model()
        assert isinstance(model, QStandardItemModel)
        item = model.item(row)
        if item is None:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        if value is None:
            return  # 占位说明行（「未配置模型」等）不可选
        # 当前项显示与勾选跟选择走；「恢复默认」行是动作行，不打勾
        if row != self.currentIndex():
            self.setCurrentIndex(row)
        for other_row in range(model.rowCount()):
            other = model.item(other_row)
            if other is not None and other.isCheckable():
                other.setCheckState(
                    Qt.CheckState.Checked
                    if other_row == row and value
                    else Qt.CheckState.Unchecked
                )
        self.hidePopup()
        self.candidate_picked.emit(item)


TOOLBAR_WIDTH = 260  # 收起/展开的固定宽度（约可用宽度 15% 的常见值）
TOOLBAR_RADIUS = 18
NOTICE_MS = 2500  # 模式/步骤切换提示停留多久
NOTICE_MOVE_MS = 240  # 被点的按钮滑到行首/滑回原位
NOTICE_FADE_MS = 160  # 其余按钮与提示文字的渐隐渐显
SLIDER_CONFIRM_MS = 5000  # 滑动确认的超时：5 秒内没滑到最右按取消处理
SLIDER_HINT = "向右滑动开始压缩"  # 滑块轨道里的提示文字


class ToolbarShadowLayer(QWidget):
    """垫在高级栏之下的投影层：画在栏外的柔和晕染，观感与输入框一致。

    高级栏压在不透明的消息区上，``QGraphicsDropShadowEffect`` 之外的方案
    需要一块能画出栏几何之外的画布，所以由宿主（``content_host``）摆成
    高级栏的兄弟控件，垫在它下面。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")
        # 与高级栏共用展开进度，投影范围与浓度一起过渡。
        self._progress = 0.0

    def sync_geometry(self, toolbar: SessionToolbar, progress: float) -> None:
        """贴住高级栏当前露出的部分，复用展开进度渐显 / 渐隐。"""
        self._progress = max(0.0, min(1.0, progress))
        geometry = toolbar.geometry()
        geometry.setWidth(round(geometry.width() * self._progress))
        self.setGeometry(soft_shadow.shadow_bounds(geometry))
        self.setVisible(self._progress > 0.0)
        # 进度可能只改变透明度（宽度取整后没变），仍需逐帧重画。
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._progress <= 0.0:
            return
        painter = QPainter(self)
        painter.setOpacity(self._progress)
        body = QRectF(
            soft_shadow.SHADOW_SPREAD,
            soft_shadow.SHADOW_SPREAD - soft_shadow.SHADOW_OFFSET,
            float(self.width() - 2 * soft_shadow.SHADOW_SPREAD),
            float(self.height() - 2 * soft_shadow.SHADOW_SPREAD),
        )
        soft_shadow.paint_soft_shadow(painter, body, TOOLBAR_RADIUS, QRectF(self.rect()))
        painter.end()


class SessionToolbar(QFrame):
    """右侧会话工具栏。默认展开、可收起。

    栏上不画投影：控件画不出自身几何之外，柔和阴影由外置的
    :class:`ToolbarShadowLayer` 垫在栏下绘制，观感与输入框一致。
    """

    collapse_toggled = Signal(bool)  # True = 收起
    thinking_level_changed = Signal(str)
    thinking_force_requested = Signal(str)
    endpoint_override_requested = Signal(str)  # endpoint_id；空串 = 清除覆盖回默认
    compress_requested = Signal()
    attach_requested = Signal()
    mode_toggle_requested = Signal()  # 切换执行模式（内置工具 ↔ 直接终端）
    permissions_requested = Signal()  # 查看/撤销本会话授权
    memory_requested = Signal()
    tool_steps_toggle_requested = Signal()  # 隐藏/显示工具步骤
    export_requested = Signal()  # 打开导出悬浮窗

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backdrop_engine: BackdropEngine | None = None
        self._backdrop_tint = "#00000000"
        self._backdrop_radius = 0.0
        self._collapsed = False
        self._own_toggle = True  # False：展开/收起开关由外部提供
        self._session_available = False
        self._thinking_supported: bool | None = None
        self._available_thinking_levels: tuple[str, ...] = ()
        self._thinking_locked_level: str | None = None
        self._route_candidates: list[RouteCandidate] = []
        self._route_note: str | None = None
        self._endpoint_panel: FloatingPanel | None = None
        self._context_usage_text = ""  # 占用详情，附在压缩按钮的悬停提示里
        self.setFixedWidth(TOOLBAR_WIDTH)
        self.setStyleSheet(
            f"SessionToolbar {{ background: {theme.BG_SURFACE};"
            f" border: 1px solid {theme.BORDER}; border-radius: {TOOLBAR_RADIUS}px; }}"
        )
        self._build()

    def set_backdrop(
        self, engine: BackdropEngine | None, *, tint: str = "#00000000", radius: float = 0.0
    ) -> None:
        self._backdrop_engine = engine
        self._backdrop_tint = tint
        self._backdrop_radius = radius
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self._backdrop_engine is not None and self._backdrop_engine.active:
            painter = QPainter(self)
            clip = QPainterPath()
            clip.addRoundedRect(
                QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                TOOLBAR_RADIUS,
                TOOLBAR_RADIUS,
            )
            painter.setClipPath(clip)
            self._backdrop_engine.paint(
                self, painter, tint=self._backdrop_tint, radius=self._backdrop_radius
            )
            painter.end()

    # ---------- 布局 ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(5)

        header = QHBoxLayout()
        title = QLabel("高级")
        title.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px; border: none;"
        )
        header.addWidget(title)
        header.addStretch(1)
        self._collapse_btn = QPushButton("⟩")
        self._collapse_btn.setFixedSize(22, 22)
        self._collapse_btn.setToolTip("收起工具栏")
        self._collapse_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" color: {theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        self._collapse_btn.clicked.connect(self._on_collapse)
        header.addWidget(self._collapse_btn)
        root.addLayout(header)

        self._body = QWidget()
        # 必须限定到自身：无选择器的 background 会级联进思考下拉的弹层窗口
        # （弹层是 combo 的子对象），压过全局 QSS 的 QComboBox QAbstractItemView，
        # 弹层便不再画主题底色、露出系统擦除色。
        self._body.setObjectName("sessionToolbarBody")
        self._body.setStyleSheet("#sessionToolbarBody { background: transparent; }")
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)

        # 输入框展示逻辑模型；高级栏的「实际站点」是可点值，点击呼出独立
        # 悬浮窗——栏宽有限放不下完整绑定信息，下拉框放进悬浮窗里用整行宽度。
        endpoint_row = QHBoxLayout()
        endpoint_row.setSpacing(6)
        endpoint_row.addWidget(_section_label("实际站点"))
        self._endpoint_value = QPushButton("—")
        self._endpoint_value.setObjectName("value-实际站点")
        self._endpoint_value.setToolTip("当前实际调用的站点；点击在悬浮窗里切换")
        self._endpoint_value.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; padding: 0;"
            f" text-align: left; color: {theme.TEXT_PRIMARY};"
            f" font-size: {theme.FS_SMALL}px; }}"
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
        )
        self._endpoint_value.clicked.connect(self._open_endpoint_panel)
        endpoint_row.addWidget(self._endpoint_value, 1)
        body.addLayout(endpoint_row)

        # 思考强度
        thinking_row = QHBoxLayout()
        thinking_row.setSpacing(6)
        thinking_row.addWidget(_section_label("思考"))
        self._thinking = ThinkingComboBox()
        self._thinking.force_requested.connect(self.thinking_force_requested.emit)
        self._thinking.setStyleSheet("QComboBox { background: transparent; }")
        self._thinking.setItemDelegate(_ThinkingLevelDelegate(self._thinking))
        self._thinking.addItems(list(THINKING_LEVELS))
        self._thinking.currentTextChanged.connect(self.thinking_level_changed.emit)
        thinking_row.addWidget(self._thinking, 1)
        body.addLayout(thinking_row)

        self._compress_btn = QPushButton("压缩")
        self._compress_btn.setProperty("flat", True)
        # 点击不直接压缩：先进滑动确认，滑到最右才发 compress_requested（防误触）
        self._compress_btn.clicked.connect(self._on_compress_clicked)

        # Agent 会话级操作（按会话隔离：状态随会话切换恢复）
        self._mode_btn = QPushButton("模式")
        self._mode_btn.setProperty("flat", True)
        self._mode_btn.setToolTip("当前：内置工具；点击切换执行模式")
        self._mode_btn.clicked.connect(self.mode_toggle_requested.emit)
        self._steps_btn = QPushButton("步骤")
        self._steps_btn.setProperty("flat", True)
        self._steps_btn.setToolTip("一键隐藏/显示工具步骤（只影响展示，审计数据不删）")
        self._steps_btn.clicked.connect(self.tool_steps_toggle_requested.emit)
        self._memory_btn = QPushButton("记忆")
        self._memory_btn.setProperty("flat", True)
        self._memory_btn.clicked.connect(self.memory_requested.emit)
        self._action_buttons = (
            self._compress_btn,
            self._mode_btn,
            self._steps_btn,
            self._memory_btn,
        )
        # 切换提示：点「模式」/「步骤」后，其余按钮渐隐，被点的按钮滑到行首，
        # 右侧空出来的地方渐显切换结果；过一会儿按原路退回。
        self._action_row = _ActionRow(self._action_buttons)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._end_notice)
        # 压缩滑动确认：5 秒内没滑到最右按取消处理（与确认共用退场动画）
        self._slider_timer = QTimer(self)
        self._slider_timer.setSingleShot(True)
        self._slider_timer.setInterval(SLIDER_CONFIRM_MS)
        self._slider_timer.timeout.connect(self._on_slider_timeout)
        self._action_row.slider_confirmed.connect(self._on_slider_confirmed)
        self._refresh_compress_tooltip()
        body.addWidget(self._action_row)

        self._export_btn = QPushButton("导出会话")
        self._export_btn.setToolTip("导出当前分支，或选择其他会话与分支")
        self._export_btn.clicked.connect(self.export_requested.emit)
        body.addWidget(self._export_btn)

        body.addStretch(1)
        root.addWidget(self._body, 1)

        # 收起态：只留一个展开按钮
        self._expand_btn = QPushButton("⟨")
        self._expand_btn.setFixedSize(22, 22)
        self._expand_btn.setToolTip("展开工具栏")
        self._expand_btn.setStyleSheet(self._collapse_btn.styleSheet())
        self._expand_btn.clicked.connect(self._on_collapse)
        self._expand_btn.setVisible(False)
        root.addWidget(self._expand_btn)

    # ---------- 展示更新（由上层驱动） ----------

    def set_model_info(self, logical_model: str, endpoint: str) -> None:
        # 保留 logical_model 参数以兼容上层现有调用；它只在主输入框展示。
        _ = logical_model
        self._endpoint_value.setText(endpoint or "—")

    def set_route_candidates(
        self, candidates: Sequence[RouteCandidate], *, note: str | None = None
    ) -> None:
        """存下路由候选（§四.3）：「实际站点」点击时在独立悬浮窗里展开。

        行序即回退顺序；覆盖激活时默认绑定换成「恢复默认」行（值为空串）。
        无候选时下拉框只剩 ``note``（「未配置模型」等）且不可选。
        """
        self._route_candidates = list(candidates)
        self._route_note = note

    def _open_endpoint_panel(self) -> None:
        """「实际站点」→ 独立悬浮窗：栏宽有限放不下完整绑定信息，
        下拉框放进悬浮窗用整行宽度展示。选完即关窗；点窗外部由悬浮窗收起。"""
        old = self._endpoint_panel
        if old is not None:
            # 悬浮窗关闭即自毁（deleteLater）：包装可能还指着已销毁的 C++ 对象，
            # 对死包装再调 deleteLater 会让整个槽炸掉——表现为之后再也点不开。
            # 替换路径同样要先收掉可能还开着的下拉弹层（见下方 closed 接线）。
            with suppress(RuntimeError):
                for combo in old.findChildren(UpwardComboBox):
                    combo.hidePopup()
                old.deleteLater()
            self._endpoint_panel = None
        panel = FloatingPanel(self.window(), "选择站点", width=420)

        def _on_closed() -> None:
            # 关面板要**先收掉还开着的下拉弹层**：带着激活 popup 销毁面板
            # 会留下悬空的弹出链和鼠标抓取，之后「实际站点」按钮点不开。
            # closed 在 close_panel 开始时同步发出：此刻面板和下拉框都还活着，
            # 正好按「先弹层、后面板」的顺序收场。
            with suppress(RuntimeError):
                combo.hidePopup()
            self._endpoint_panel = None

        panel.closed.connect(_on_closed)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        caption = QLabel("本会话实际使用的站点；行序即回退顺序，当前站点已勾选。")
        caption.setWordWrap(True)
        caption.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px; border: none;"
        )
        column.addWidget(caption)

        combo = _RouteComboBox()
        combo.setStyleSheet("QComboBox { background: transparent; }")
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        current_index = 0
        if not self._route_candidates:
            placeholder = QStandardItem(self._route_note or "（未配置模型）")
            placeholder.setEnabled(False)
            model.appendRow(placeholder)
        else:
            for index, candidate in enumerate(self._route_candidates):
                model.appendRow(_candidate_item(candidate))
                if candidate.current:
                    current_index = index
        combo.setCurrentIndex(current_index)  # 连信号前定当前项，不会误发
        combo.candidate_picked.connect(
            lambda item: self._on_panel_endpoint_picked(panel, item)
        )
        column.addWidget(combo)

        note = QLabel(ROUTE_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; border: none;"
        )
        column.addWidget(note)
        panel.content_layout.addLayout(column)
        panel.popup(self._endpoint_value)
        self._endpoint_panel = panel

    def _on_panel_endpoint_picked(self, panel: FloatingPanel, item: QStandardItem) -> None:
        """选中候选行：外发 endpoint_id（空串 = 清除覆盖回默认）并关窗。

        选中当前项由 :meth:`_RouteComboBox._pick` 自然去重（同值也重发，
        应用层是幂等覆盖）；这里只管转发与收场。
        """
        value = item.data(Qt.ItemDataRole.UserRole)
        if value is None:
            return
        self.endpoint_override_requested.emit(str(value))
        panel.close_panel()

    def set_thinking_level(self, level: str) -> None:
        """设置当前等级（程序化设置不触发变更信号）。"""
        if level not in THINKING_LEVELS:
            return
        self._thinking.blockSignals(True)
        self._thinking.setCurrentText(level)
        self._thinking.blockSignals(False)

    def set_thinking_capability(
        self,
        supported: bool | None,
        *,
        locked_level: str | None = None,
        available_levels: tuple[str, ...] = (),
        runtime_levels: tuple[str, ...] | None = None,
    ) -> None:
        """按已验证等级限制控件；显式锁定等级不可由用户覆盖。"""
        self._thinking.cancel_hold()
        self._thinking_supported = supported
        self._thinking_locked_level = locked_level
        self._available_thinking_levels = available_levels
        model = self._thinking.model()
        if isinstance(model, QStandardItemModel):
            for index, level in enumerate(THINKING_LEVELS):
                item = model.item(index)
                if item is not None:
                    enabled = not available_levels or level == "off" or level in available_levels
                    if supported is False:
                        enabled = level == "off"
                    if locked_level is not None:
                        enabled = level == locked_level
                    if runtime_levels is not None:
                        enabled = enabled and level in runtime_levels
                    item.setEnabled(enabled)
                    item.setToolTip("" if enabled else "当前等级不可用；按住 3 秒可临时强制解锁")
        if locked_level:
            self.set_thinking_level(locked_level)
            self._thinking.setToolTip(
                f"该绑定固定为 {locked_level}，常规选择不可切换；可长按置灰项 3 秒临时试用"
            )
        elif supported is False:
            self._thinking.setToolTip("能力探测确认该实际模型不支持思考")
        elif supported is None:
            self._thinking.setToolTip("思考能力尚未确认；可选等级以当前运行时为准")
        else:
            self._thinking.setToolTip("会话级思考强度")
        self._refresh_thinking_enabled()

    def _refresh_thinking_enabled(self) -> None:
        if not self._session_available:
            self._thinking.cancel_hold()
        # 即使 reasoning 未开放也能展开，置灰项仅接受长按试用。
        self._thinking.setEnabled(self._session_available)

    def set_context_usage(self, text: str) -> None:
        # 占用已改为主输入框圆环；高级栏只把详情附在压缩入口的提示里。
        self._context_usage_text = text
        self._refresh_compress_tooltip()

    def set_execution_mode(self, mode: str, *, is_fallback_shell: bool = False) -> None:
        """在紧凑按钮的提示里回显当前执行模式（shell 回退情况也放在这里，§10.1）。"""
        current = "终端" if mode == "terminal" else "内置工具"
        if mode == "terminal" and is_fallback_shell:
            current += "（Powershell5.1兼容）"
        self._mode_btn.setToolTip(f"当前：{current}；点击切换执行模式")

    def set_tool_steps_hidden(self, hidden: bool) -> None:
        """在紧凑按钮的提示里回显工具步骤显隐。"""
        self._steps_btn.setToolTip("显示工具步骤" if hidden else "隐藏工具步骤")

    # ---------- 切换提示 ----------

    @property
    def notice_text(self) -> str:
        """当前显示的切换提示；没有提示时为空串。"""
        return self._action_row.notice_text

    def show_mode_notice(self, text: str) -> None:
        """模式切换提示：模式按钮滑到行首，右侧显示 ``text``。"""
        self._show_notice(self._mode_btn, text)

    def show_steps_notice(self, text: str) -> None:
        """步骤显隐提示：步骤按钮滑到行首，右侧显示 ``text``。"""
        self._show_notice(self._steps_btn, text)

    def _show_notice(self, source: QPushButton, text: str) -> None:
        self._cancel_slider_confirm()  # 确认态让位给提示（无动画复位）
        self._action_row.show_notice(source, text)
        # 提示期间再点一次会重新计时，不会中途被收回
        self._notice_timer.start(NOTICE_MS)

    def _end_notice(self) -> None:
        self._notice_timer.stop()
        self._action_row.hide_notice()

    # ---------- 压缩滑动确认 ----------

    def _on_compress_clicked(self) -> None:
        """压缩按钮：点击先进入滑动确认，确认态里再点一次 = 取消。"""
        if self._action_row.slider_active:
            self._slider_timer.stop()
            self._action_row.hide_slider_confirm()
            self._refresh_compress_tooltip()
            return
        self._notice_timer.stop()  # 还没收起的模式/步骤提示先收掉
        self._action_row.show_slider_confirm(self._compress_btn, SLIDER_HINT)
        self._slider_timer.start()
        self._refresh_compress_tooltip()

    def _on_slider_confirmed(self) -> None:
        """滑块滑到最右：收起确认态，外发压缩意图（业务在应用层）。"""
        self._slider_timer.stop()
        self._action_row.hide_slider_confirm()
        self._refresh_compress_tooltip()
        self.compress_requested.emit()

    def _on_slider_timeout(self) -> None:
        """5 秒内没滑到最右：按取消处理，播退场动画。"""
        self._slider_timer.stop()
        self._action_row.hide_slider_confirm()
        self._refresh_compress_tooltip()

    def _cancel_slider_confirm(self) -> None:
        """无动画退出压缩确认态（模式/步骤提示要接管行首时调用）。"""
        self._slider_timer.stop()
        self._action_row.snap_slider_away()
        self._refresh_compress_tooltip()

    def _refresh_compress_tooltip(self) -> None:
        """压缩按钮提示：确认态提示如何取消；常态说明两段式用法（附占用详情）。"""
        if self._action_row.slider_active:
            self._compress_btn.setToolTip("再次点击取消；向右滑到最右开始压缩")
            return
        usage = self._context_usage_text
        base = "压缩上下文（点击后向右滑动确认，完成后弹预览）"
        self._compress_btn.setToolTip(f"{usage}\n{base}" if usage else base)

    def set_session_available(self, available: bool) -> None:
        """没有可用内核时禁用会话级控件（空会话仍可调，设置直接作用于内核）。"""
        self._session_available = available
        self._refresh_thinking_enabled()

    # ---------- 收起/展开 ----------

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def _on_collapse(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        # 外部开关负责整栏的显隐与展开动画；收起时保持原样，才能完整地播完收回
        if self._own_toggle:
            self._body.setVisible(not collapsed)
            self._collapse_btn.setVisible(not collapsed)
            self._expand_btn.setVisible(collapsed)
            self.setFixedWidth(44 if collapsed else TOOLBAR_WIDTH)
        self.collapse_toggled.emit(collapsed)

    def use_external_toggle(self) -> None:
        """展开/收起改由外部开关（输入框右上角）负责时，隐藏栏内自带的按钮。

        此后收起不再缩成窄栏，整栏的显隐与动画交给外部。
        """
        self._own_toggle = False
        self._collapse_btn.setVisible(False)
        self._expand_btn.setVisible(False)
        self._body.setVisible(True)
        self.setFixedWidth(TOOLBAR_WIDTH)

    def maybe_auto_collapse(self, available_width: int) -> None:
        """响应式退化（§8.2）：窗口不够宽时自动收起，够宽时**不自动展开**
        （避免用户手动收起后又被自动打开）。"""
        if available_width < 900 and not self._collapsed:
            self.set_collapsed(True)


class _ActionRow(QWidget):
    """高级栏的动作按钮行。

    按钮位置自己算、不走布局——布局会在每次重排时把按钮拽回原位，没法做位移动画。
    常态：按钮等分整行。提示态：被点的按钮占第一格，其余按钮隐藏，右侧放提示文字。
    压缩确认态：与提示态同一套位移 + 渐隐编排，右侧的提示文字换成滑动确认滑块。
    """

    SPACING = 0
    NOTICE_GAP = 8  # 提示文字与行首按钮之间的留白
    SLIDER_ENTER_OFFSET = 14  # 滑块进场时从右侧滑入的距离（出场滑回同样距离）

    slider_confirmed = Signal()  # 滑块被拖到最右：压缩确认

    def __init__(self, buttons: tuple[QPushButton, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._buttons = buttons
        for button in buttons:
            button.setParent(self)
            # 全局按钮样式左右各 16px 内边距，四等分后放不下两个字、会被裁；这里去掉
            button.setStyleSheet("QPushButton { padding-left: 0; padding-right: 0; margin: 0; }")
            # 效果默认 0.7 透明度；不显式置满，首次提示动画前按钮会一直偏淡三成
            effect = QGraphicsOpacityEffect(button)
            effect.setOpacity(1.0)
            button.setGraphicsEffect(effect)
        self.notice = QLabel("", self)
        self.notice.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_SMALL}px; border: none;"
        )
        self.notice.setGraphicsEffect(QGraphicsOpacityEffect(self.notice))
        self.notice.hide()
        self._notice_full = ""  # 完整提示文字；显示的可能是省略版
        self._source: QPushButton | None = None  # 提示态下被点的按钮；常态为 None
        self._anim: QAbstractAnimation | None = None
        # 压缩二段确认的滑块：与提示文字占用同一块区域，互斥出现
        self.slider = SlideConfirm(self)
        slider_effect = QGraphicsOpacityEffect(self.slider)
        slider_effect.setOpacity(1.0)
        self.slider.setGraphicsEffect(slider_effect)
        self.slider.hide()
        self.slider.confirmed.connect(self.slider_confirmed.emit)
        self._slider_source: QPushButton | None = None  # 确认态下被点的按钮；常态为 None

    # ---------- 尺寸与摆放 ----------

    def sizeHint(self) -> QSize:
        height = max(button.sizeHint().height() for button in self._buttons)
        width = sum(button.sizeHint().width() for button in self._buttons)
        return QSize(width + self.SPACING * (len(self._buttons) - 1), height)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())

    def _slots(self) -> list[QRect]:
        count = len(self._buttons)
        width = max(0, (self.width() - self.SPACING * (count - 1)) // count)
        return [QRect(i * (width + self.SPACING), 0, width, self.height()) for i in range(count)]

    def _notice_rect(self) -> QRect:
        left = self._slots()[0].right() + 1 + self.NOTICE_GAP
        return QRect(left, 0, max(0, self.width() - left), self.height())

    def _place(self) -> None:
        """按当前状态直接摆放（无动画）。"""
        slots = self._slots()
        for button, slot in zip(self._buttons, slots, strict=True):
            button.setGeometry(slots[0] if button is self._source else slot)
        self.notice.setGeometry(self._notice_rect())
        if self._slider_source is not None:
            self.slider.setGeometry(self._notice_rect())
        self._refresh_notice_text()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._skip_anim()  # 尺寸变了，动画的起止点都作废，直接跳到终态
        self._place()

    # ---------- 提示 ----------

    @property
    def animating(self) -> bool:
        return self._anim is not None

    @property
    def slider_active(self) -> bool:
        """是否处于压缩滑动确认态。"""
        return self._slider_source is not None

    @property
    def notice_text(self) -> str:
        """完整提示文字（显示时可能因放不下而带省略号）。"""
        return self._notice_full if self._source is not None else ""

    def _set_notice_text(self, text: str) -> None:
        self._notice_full = text
        self._refresh_notice_text()

    def _refresh_notice_text(self) -> None:
        """放不下时省略号截断，完整文字进悬停提示。"""
        full = self._notice_full
        shown = self.notice.fontMetrics().elidedText(
            full, Qt.TextElideMode.ElideRight, self._notice_rect().width()
        )
        self.notice.setText(shown)
        self.notice.setToolTip(full if shown != full else "")

    def show_notice(self, source: QPushButton, text: str) -> None:
        if self._source is source:
            self._crossfade_notice(text)
            return
        self._skip_anim()
        if self._source is not None:
            self._snap_normal()  # 另一个按钮的提示还在：先无动画复位
        self.snap_slider_away()  # 压缩确认态还开着：无动画复位（计时器由上层停）
        self._source = source
        self.notice.setGeometry(self._notice_rect())
        self._set_notice_text(text)
        others = [button for button in self._buttons if button is not source]

        group = QParallelAnimationGroup(self)
        group.addAnimation(self._slide(source, self._slots()[0].topLeft()))
        for button in others:
            group.addAnimation(self._fade(button, 1.0, 0.0))
        self.notice.show()
        # 提示文字等按钮让出位置后再浮现
        group.addAnimation(self._fade(self.notice, 0.0, 1.0, delay=NOTICE_FADE_MS // 2))

        def _done() -> None:
            for button in others:
                button.hide()
            _opacity(self.notice).setOpacity(1.0)

        self._run(group, _done)

    def _crossfade_notice(self, text: str) -> None:
        """提示态下再点行首按钮：旧文字渐隐 → 换字 → 新文字渐显。"""
        if text == self._notice_full:
            return
        self._skip_anim()  # 进场或上一次换字还没放完：先落到终态
        fade_out = self._fade(self.notice, 1.0, 0.0)
        fade_out.finished.connect(lambda: self._set_notice_text(text))
        group = QSequentialAnimationGroup(self)
        group.addAnimation(fade_out)
        group.addAnimation(self._fade(self.notice, 0.0, 1.0, prime=False))

        def _done() -> None:
            self._set_notice_text(text)  # 被跳过时 fade_out 的回调未必触发，这里兜底
            _opacity(self.notice).setOpacity(1.0)

        self._run(group, _done)

    def hide_notice(self) -> None:
        source = self._source
        if source is None:
            return
        self._skip_anim()
        self._source = None
        others = [button for button in self._buttons if button is not source]
        slots = self._slots()

        group = QParallelAnimationGroup(self)
        group.addAnimation(self._fade(self.notice, 1.0, 0.0))
        group.addAnimation(self._slide(source, slots[self._buttons.index(source)].topLeft()))
        for button in others:
            button.show()
            group.addAnimation(self._fade(button, 0.0, 1.0, delay=NOTICE_FADE_MS // 2))

        def _done() -> None:
            self.notice.hide()
            self._set_notice_text("")
            # 终态一律精确落位：别让按钮停在 0.99 之类的半透明上
            for button in self._buttons:
                _opacity(button).setOpacity(1.0)
            self._place()

        self._run(group, _done)

    def _snap_normal(self) -> None:
        self._source = None
        for button in self._buttons:
            _opacity(button).setOpacity(1.0)
            button.show()
        self.notice.hide()
        self._set_notice_text("")
        self._place()

    # ---------- 压缩滑动确认 ----------

    def show_slider_confirm(self, source: QPushButton, hint: str) -> None:
        """确认态进场：``source`` 滑到行首，其余按钮渐隐，右侧滑块渐显滑入。

        与 ``show_notice`` 同一套位移 + 渐隐编排；提示文字换成可拖动的滑块。
        已在确认态时不动（收回由上层的 5 秒计时器或再次点击负责）。
        """
        if self._slider_source is source:
            return
        self._skip_anim()
        if self._source is not None:
            self._snap_normal()  # 模式/步骤的提示还开着：先无动画复位
        self.snap_slider_away()
        self._slider_source = source
        self.slider.set_hint(hint)
        self.slider.reset()
        target = self._notice_rect()
        self.slider.setGeometry(target.translated(self.SLIDER_ENTER_OFFSET, 0))
        others = [button for button in self._buttons if button is not source]

        group = QParallelAnimationGroup(self)
        group.addAnimation(self._slide(source, self._slots()[0].topLeft()))
        for button in others:
            group.addAnimation(self._fade(button, 1.0, 0.0))
        self.slider.show()
        self.slider.raise_()
        # 滑块从右侧滑入并渐显，等按钮让位后再浮现（与提示文字同拍）
        group.addAnimation(self._fade(self.slider, 0.0, 1.0, delay=NOTICE_FADE_MS // 2))
        group.addAnimation(self._slide(self.slider, target.topLeft()))

        def _done() -> None:
            for button in others:
                button.hide()
            _opacity(self.slider).setOpacity(1.0)
            self.slider.setGeometry(target)

        self._run(group, _done)

    def hide_slider_confirm(self) -> None:
        """确认态退场：滑块渐隐滑出，其余按钮原位渐显。

        确认与取消共用这条退场路径；退场时手柄先弹回起点，取消的收势也看得见。
        不在确认态时是空操作。
        """
        source = self._slider_source
        if source is None:
            return
        self._skip_anim()
        self._slider_source = None
        self.slider.snap_back()
        others = [button for button in self._buttons if button is not source]

        group = QParallelAnimationGroup(self)
        group.addAnimation(self._fade(self.slider, 1.0, 0.0))
        group.addAnimation(
            self._slide(
                self.slider,
                self._notice_rect().translated(self.SLIDER_ENTER_OFFSET, 0).topLeft(),
            )
        )
        for button in others:
            button.show()
            group.addAnimation(self._fade(button, 0.0, 1.0, delay=NOTICE_FADE_MS // 2))

        def _done() -> None:
            self.slider.hide()
            self.slider.reset()
            _opacity(self.slider).setOpacity(1.0)
            # 终态一律精确落位：别让按钮停在 0.99 之类的半透明上
            for button in self._buttons:
                _opacity(button).setOpacity(1.0)
            self._place()

        self._run(group, _done)

    def snap_slider_away(self) -> None:
        """无动画退出确认态（提示或另一来源要接管行首时先复位）。"""
        if self._slider_source is None:
            return
        self._slider_source = None
        self.slider.hide()
        self.slider.reset()
        _opacity(self.slider).setOpacity(1.0)
        for button in self._buttons:
            _opacity(button).setOpacity(1.0)
            button.show()
        self._place()

    # ---------- 动画 ----------

    def _slide(self, widget: QWidget, end: QPoint) -> QPropertyAnimation:
        anim = QPropertyAnimation(widget, b"pos")
        anim.setDuration(NOTICE_MOVE_MS)
        anim.setStartValue(widget.pos())
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        return anim

    def _fade(
        self, widget: QWidget, start: float, end: float, *, delay: int = 0, prime: bool = True
    ) -> QAbstractAnimation:
        """``prime``：立刻把透明度落到起点，免得动画开跑前闪一帧。
        排在序列后段的渐变要关掉它，否则会抢先盖掉前一段的效果。"""
        effect = _opacity(widget)
        if prime:
            effect.setOpacity(start)
        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(NOTICE_FADE_MS)
        anim.setStartValue(start)
        anim.setEndValue(end)
        if not delay:
            return anim
        seq = QSequentialAnimationGroup()
        seq.addPause(delay)
        seq.addAnimation(anim)
        return seq

    def _run(self, group: QAbstractAnimation, on_done: Callable[[], None]) -> None:
        def _finished() -> None:
            if self._anim is group:
                self._anim = None
            on_done()
            group.deleteLater()

        group.finished.connect(_finished)
        self._anim = group
        group.start()

    def _skip_anim(self) -> None:
        """把正在跑的动画直接跳到终点（终态由它自己的 finished 收尾）。"""
        anim = self._anim
        if anim is not None:
            anim.setCurrentTime(anim.totalDuration())


def _opacity(widget: QWidget) -> QGraphicsOpacityEffect:
    return cast(QGraphicsOpacityEffect, widget.graphicsEffect())


def _candidate_item(candidate: RouteCandidate) -> QStandardItem:
    """悬浮窗下拉框的候选行：宽窗里放得下完整绑定，勾选标记当前站点。

    展示用站点显示名；行值存 UserRole：endpoint_id（切换仍按 id）；
    「恢复默认」行是空串（应用层清除覆盖）。
    """
    if candidate.restore:
        text = f"恢复默认：{candidate.endpoint_name} → {candidate.model_id}"
        value: str | None = ""
        tip = "清除会话级覆盖，回到配置里的默认站点（下一轮生效）"
        checkable = False
    else:
        text = f"{candidate.label}：{candidate.endpoint_name} → {candidate.model_id}"
        if candidate.override:
            text += "（会话覆盖）"
        value = candidate.endpoint_id
        state_tip = (
            "当前实际使用的站点" if candidate.current else "点击切换到该站点（下一轮生效）"
        )
        tip = state_tip
        checkable = True
    item = QStandardItem(text)
    item.setData(value, Qt.ItemDataRole.UserRole)
    item.setToolTip(tip)
    item.setCheckable(checkable)
    if checkable:
        item.setCheckState(
            Qt.CheckState.Checked if candidate.current else Qt.CheckState.Unchecked
        )
    return item


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; border: none;"
    )
    return label
