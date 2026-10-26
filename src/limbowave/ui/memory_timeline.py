"""A local, read-only gallery of retained memories, separate from configuration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.memory_service import MemoryService
from limbowave.domain.footprint import MemoryFootprint, memory_datetime
from limbowave.ui import theme
from limbowave.ui.backdrop import BackdropEngine
from limbowave.ui.background_tasks import BackgroundTasks

PAGE_SIZE = 40
TITLE_COLLAPSE_OFFSET = 32
TITLE_MOTION_MS = 180
UNBOUNDED_HEIGHT = 16777215
SOURCE_LABELS = {
    "user": "亲手记下",
    "model": "AI 记住",
    "promoted": "收为全局",
    "builtin": "内置约定",
}
PRIVACY_TEXT = (
    "记忆正文通过资料库密钥加密保存在本机。浏览、搜索足迹只读取本地数据，"
    "不会为此调用 AI 或上传画廊内容。\n"
    "这不等于记忆永不离开设备：用于对话的记忆可能随请求发送给你选择的模型；"
    "主动导出、备份或开启远程访问也可能使数据离开本机。\n"
    "这里只呈现当前保留的记忆，不展示已删除或已归档条目、历史快照和会话级覆盖。"
    "未改动的分支副本合并展示，分支间不同正文分别展示。"
)


def _label(text: str, name: str = "", *, selectable: bool = False) -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setObjectName(name)
    label.setProperty("footprintLabel", True)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    if selectable:
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
    return label


def _local_time(value: str | None) -> datetime | None:
    parsed = memory_datetime(value)
    if parsed is None:
        return None
    try:
        return parsed.astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def _date_key(row: MemoryFootprint) -> str:
    value = _local_time(row.memory.created_at)
    return value.strftime("%Y-%m-%d") if value else "日期未记录"


def _timestamp(value: str | None) -> str:
    parsed = _local_time(value)
    return parsed.strftime("%Y.%m.%d  %H:%M") if parsed else "日期未记录"


class FootprintCard(QFrame):
    def __init__(self, row: MemoryFootprint, navigate: Callable[[object], None]) -> None:
        super().__init__()
        self.setObjectName("footprintCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.row = row
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        top = QHBoxLayout()
        kind = SOURCE_LABELS.get(row.source_kind, "已保存")
        scope = "全局记忆" if row.scope == "global" else "会话记忆"
        top.addWidget(_label(f"{kind}  ·  {scope}", "footprintSource"), 1)
        created = _local_time(row.memory.created_at)
        top.addWidget(_label(created.strftime("%H:%M") if created else "时间未知", "footprintMeta"))
        layout.addLayout(top)
        content = row.memory.content
        lines = content.splitlines()
        preview = "\n".join(lines[:5])[:220]
        truncated = preview != content
        self.content = _label(
            preview + "…" if truncated else content, "footprintContent", selectable=True
        )
        self.content.setAccessibleName(f"记忆正文：{kind}")
        layout.addWidget(self.content)
        if truncated:
            expand = QPushButton("展开全文")
            expand.setObjectName("footprintExpand")
            expand.setProperty("flat", True)
            expand.setCheckable(True)
            expand.setAccessibleName("展开或收起这条记忆的完整正文")

            def toggle(checked: bool) -> None:
                self.content.setText(content if checked else preview + "…")
                expand.setText("收起全文" if checked else "展开全文")

            expand.toggled.connect(toggle)
            layout.addWidget(expand, 0, Qt.AlignmentFlag.AlignLeft)
        details = [f"保存于 {_timestamp(row.memory.created_at)}"]
        if row.written_at:
            details.append(f"关联对话写于 {_timestamp(row.written_at)}")
        if row.memory.updated_at:
            details.append(f"最近整理于 {_timestamp(row.memory.updated_at)}")
        self.details = _label("\n".join(details), "footprintMeta")
        layout.addWidget(self.details)
        if row.origin is not None:
            origin = row.origin
            source = QPushButton("回到对话  ↗")
            source.setObjectName("footprintOpenSource")
            source.setProperty("flat", True)
            source.setAccessibleName(f"回到对话：{origin.conversation_title}")
            # Do not put private plaintext into rich-text auto-detected tooltips.
            source.clicked.connect(
                lambda: navigate(
                    (
                        origin.conversation_id,
                        origin.branch_id,
                        row.source_message_id,
                        "",
                    )
                )
            )
            footer = QHBoxLayout()
            title = _label(f"{origin.conversation_title} · {origin.branch_title}", "footprintMeta")
            footer.addWidget(title, 1)
            footer.addWidget(source)
            layout.addLayout(footer)
        elif row.source_kind in {"model", "promoted"}:
            layout.addWidget(_label("来源对话已不可用，保存的记忆仍在这里。", "footprintMeta"))
        if len(row.locations) > 1:
            layout.addWidget(
                _label(f"同一片段保留在 {len(row.locations)} 个分支中", "footprintMeta")
            )


class _HeaderViewport(QWidget):
    """Clip the upper portion of a natural-height header and reclaim its layout space."""

    fraction_changed = Signal(float)

    def __init__(self, panel: QFrame) -> None:
        super().__init__()
        self.setObjectName("footprintHeaderViewport")
        self._panel = panel
        self._fraction = 0.0
        self._syncing = False
        self._panel.setParent(self)
        self._panel.installEventFilter(self)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self.sync_geometry)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _get_fraction(self) -> float:
        return self._fraction

    def _set_fraction(self, value: float) -> None:
        self._fraction = max(0.0, min(0.5, value))
        self.fraction_changed.emit(self._fraction)
        self.sync_geometry()

    clipFraction = Property(float, _get_fraction, _set_fraction)

    def sizeHint(self) -> QSize:
        hint = self._panel.sizeHint()
        return QSize(hint.width(), max(0, round(hint.height() * (1.0 - self._fraction))))

    def minimumSizeHint(self) -> QSize:
        hint = self._panel.minimumSizeHint()
        return QSize(hint.width(), max(0, round(hint.height() * (1.0 - self._fraction))))

    def sync_geometry(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            layout = self._panel.layout()
            if layout is None:
                return
            # Before QStackedWidget assigns page geometry, this host may briefly
            # be narrower than its controls. Measuring wrapped text at that width
            # creates a huge fixed height and permanently expands the main window.
            # Respect the same minimum width the panel layout itself will enforce.
            width = max(self.width(), layout.totalMinimumSize().width())
            height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else -1
            height = max(0, height if height >= 0 else layout.totalSizeHint().height())
            offset = round(height * self._fraction)
            self._panel.setGeometry(0, -offset, width, height)
            visible_height = height - offset
            if self.minimumHeight() != visible_height or self.maximumHeight() != visible_height:
                self.setFixedHeight(visible_height)
        finally:
            self._syncing = False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.sync_geometry()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._panel and event.type() in (
            QEvent.Type.LayoutRequest,
            QEvent.Type.StyleChange,
            QEvent.Type.FontChange,
        ):
            self._layout_timer.start(0)
        return super().eventFilter(watched, event)


class MemoryTimelinePage(QWidget):
    close_requested = Signal()
    manage_requested = Signal()
    source_requested = Signal(object)

    def __init__(self, service: MemoryService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("memoryTimelinePage")
        self._service = service
        self._backdrop_engine: BackdropEngine | None = None
        self._jobs = BackgroundTasks(self)
        self._generation = 0
        self._disposed = False
        self._entries: list[MemoryFootprint] = []
        self._filtered: list[MemoryFootprint] = []
        self._visible_count = PAGE_SIZE
        self._loading = False
        self._updating_timeline = False
        self._title_collapsed = False
        self._title_settling = False
        self.cards: list[FootprintCard] = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._filter)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(12)
        self._header_panel = QFrame()
        self._header_viewport = _HeaderViewport(self._header_panel)
        self._header_viewport.fraction_changed.connect(
            lambda value: root.setContentsMargins(24, round(16 * (1.0 - value * 2)), 24, 16)
        )
        self._header_panel.setObjectName("footprintHeaderPanel")
        header = QVBoxLayout(self._header_panel)
        header.setContentsMargins(18, 14, 18, 14)
        header.setSpacing(10)
        root.addWidget(self._header_viewport)

        navigation = QHBoxLayout()
        back = QPushButton("返回对话")
        back.setObjectName("footprintReturn")
        back.setProperty("flat", True)
        back.setAccessibleName("返回对话")
        back.clicked.connect(self.close_requested.emit)
        self._compact_title = _label("足迹", "footprintCompactTitle")
        self._compact_title.hide()
        navigation.addWidget(self._compact_title)
        navigation.addStretch(1)
        self.privacy_button = QPushButton("本机加密保存 · 详情")
        self.privacy_button.setIcon(self._lock_icon())
        self.privacy_button.setIconSize(QSize(16, 16))
        self.privacy_button.setObjectName("footprintPrivacyBadge")
        self.privacy_button.setCheckable(True)
        self.privacy_button.setAccessibleName("本机加密保存，展开隐私与数据边界说明")
        navigation.addWidget(self.privacy_button)
        manage = QPushButton("管理记忆")
        manage.setProperty("flat", True)
        manage.clicked.connect(self.manage_requested.emit)
        navigation.addWidget(manage)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setAccessibleName("刷新本地足迹")
        self.refresh_button.clicked.connect(self.reload)
        navigation.addWidget(self.refresh_button)
        # Same navigation convention as settings: the return action is last, top-right.
        navigation.addWidget(back)
        header.addLayout(navigation)

        self._title_region = QWidget()
        self._title_region.setObjectName("footprintTitleRegion")
        self._title_region.setMinimumHeight(0)
        self._title_region.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        words = QVBoxLayout(self._title_region)
        words.setContentsMargins(0, 0, 0, 0)
        words.setSpacing(8)
        words.addWidget(_label("FOOTPRINTS  /  记忆与时间", "footprintEyebrow"))
        words.addWidget(_label("你留下的，都有回响。", "footprintTitle"))
        words.addWidget(
            _label("一些想法，一段文字。沿着时间，重新遇见被记住的自己。", "footprintSubtitle")
        )
        self.summary = _label("正在打开你的记忆…", "footprintSummary")
        words.addWidget(self.summary)
        self._local_hint = _label(
            "仅本地浏览 · 记忆用于对话时，可能随请求发送给所选模型。",
            "footprintLocalHint",
        )
        words.addWidget(self._local_hint)
        header.addWidget(self._title_region)
        self._title_animation = QPropertyAnimation(self._title_region, b"maximumHeight", self)
        self._title_animation.setDuration(TITLE_MOTION_MS)
        self._title_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._header_animation = QPropertyAnimation(self._header_viewport, b"clipFraction", self)
        self._header_animation.setDuration(TITLE_MOTION_MS)
        self._header_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._header_motion = QParallelAnimationGroup(self)
        self._header_motion.addAnimation(self._title_animation)
        self._header_motion.addAnimation(self._header_animation)
        self._header_motion.finished.connect(self._finish_title_motion)
        self._title_settle_timer = QTimer(self)
        self._title_settle_timer.setSingleShot(True)
        self._title_settle_timer.setInterval(20)
        self._title_settle_timer.timeout.connect(self._finish_title_layout)

        self.privacy = _label(PRIVACY_TEXT, "footprintPrivacy")
        self.privacy.hide()
        self.privacy_button.toggled.connect(self.privacy.setVisible)
        header.addWidget(self.privacy)
        self.search = QLineEdit()
        self.search.setObjectName("footprintSearch")
        self.search.setPlaceholderText("寻找一段文字、一个想法，或一场对话…")
        self.search.setAccessibleName("搜索记忆正文或来源对话")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda: self._search_timer.start())
        header.addWidget(self.search)
        controls = QHBoxLayout()
        self.scope_filter = self._combo(
            "记忆范围",
            [("全部记忆", "all"), ("全局记忆", "global"), ("会话记忆", "session")],
        )
        self.source_filter = self._combo(
            "记忆来源",
            [("所有来源", "all"), *[(label, key) for key, label in SOURCE_LABELS.items()]],
        )
        self.month_filter = self._combo("保存月份", [("所有月份", "all")])
        self.order = self._combo("时间顺序", [("最近在前", "newest"), ("最早在前", "oldest")])
        for combo in (self.scope_filter, self.source_filter, self.month_filter, self.order):
            controls.addWidget(combo, 1)
            combo.currentIndexChanged.connect(self._filter)
        header.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setAccessibleName("正在读取本地加密记忆")
        header.addWidget(self.progress)

        self._reading_panel = QFrame()
        self._reading_panel.setObjectName("footprintReadingPanel")
        reading = QVBoxLayout(self._reading_panel)
        reading.setContentsMargins(0, 0, 0, 0)
        reading.setSpacing(10)
        root.addWidget(self._reading_panel, 1)
        self.status = _label("", "footprintStatus")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        reading.addWidget(self.status)
        self.reset_button = QPushButton("清除筛选")
        self.reset_button.clicked.connect(self.clear_filters)
        reading.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("footprintScroll")
        self._scroll.viewport().setObjectName("footprintViewport")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setAccessibleName("按保存日期排列的记忆时间线")
        self._scroll.viewport().installEventFilter(self)
        scrollbar = self._scroll.verticalScrollBar()
        scrollbar.installEventFilter(self)
        scrollbar.valueChanged.connect(self._scroll_position_changed)
        self._body = QWidget()
        self._body.setObjectName("footprintBody")
        self._body.setMaximumWidth(1040)
        self._timeline = QVBoxLayout(self._body)
        self._timeline.setContentsMargins(6, 8, 12, 16)
        self._timeline.setSpacing(26)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._body)
        reading.addWidget(self._scroll, 1)
        self.more_button = QPushButton("继续沿着时间看看")
        self.more_button.clicked.connect(self._more)
        reading.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape.activated.connect(self.close_requested.emit)
        self._find = QShortcut(QKeySequence.StandardKey.Find, self)
        self._find.activated.connect(self._focus_search)
        for control in (back, manage, self.refresh_button, self.privacy_button, self.search):
            control.installEventFilter(self)
        self._header_viewport.sync_geometry()
        self.reload()

    def _set_title_collapsed(self, collapsed: bool, *, animated: bool = True) -> None:
        if self._disposed:
            return
        if collapsed == self._title_collapsed and animated:
            return
        self._header_motion.stop()
        self._title_collapsed = collapsed
        self._compact_title.setVisible(collapsed)
        start = self._title_region.height() if self._title_region.isVisible() else 0
        if not collapsed:
            self._title_region.show()
        target = 0 if collapsed else self._title_region.sizeHint().height()
        if not animated:
            self._title_region.setMaximumHeight(0 if collapsed else UNBOUNDED_HEIGHT)
            self._title_region.setVisible(not collapsed)
            self._header_viewport._set_fraction(0.5 if collapsed else 0.0)
            return
        self._title_region.setMaximumHeight(start)
        self._title_animation.setStartValue(start)
        self._title_animation.setEndValue(target)
        self._header_animation.setStartValue(self._header_viewport._get_fraction())
        self._header_animation.setEndValue(0.5 if collapsed else 0.0)
        self._header_motion.start()

    def _finish_title_motion(self) -> None:
        # Hiding/restoring the layout item changes the scroll range once more.
        # Don't treat the resulting clamped value as a new user scroll to the top.
        self._title_settling = True
        self._title_region.setVisible(not self._title_collapsed)
        if not self._title_collapsed:
            self._title_region.setMaximumHeight(UNBOUNDED_HEIGHT)
        self._header_viewport.sync_geometry()
        self._title_settle_timer.start()

    def _finish_title_layout(self) -> None:
        self._header_viewport.sync_geometry()
        self._title_settling = False

    def _scroll_position_changed(self, value: int) -> None:
        if (
            self._disposed
            or self._loading
            or self._updating_timeline
            or self._title_settling
            or self._header_motion.state() == QAbstractAnimation.State.Running
        ):
            return
        if value > TITLE_COLLAPSE_OFFSET and not self._title_collapsed:
            self._set_title_collapsed(True)
        elif value == 0 and self._title_collapsed:
            self._set_title_collapsed(False)

    def _focus_search(self) -> None:
        self._set_title_collapsed(False, animated=False)
        self.search.setFocus()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            not self._disposed
            and self._title_collapsed
            and event.type() == QEvent.Type.FocusIn
            and isinstance(watched, QWidget)
            and self._header_panel.isAncestorOf(watched)
        ):
            # A keyboard-focused control must not remain clipped above the window.
            self._set_title_collapsed(False, animated=False)
        if not self._disposed and self._title_collapsed:
            scrollbar = self._scroll.verticalScrollBar()
            at_top = scrollbar.value() == 0
            if (
                at_top
                and watched in (self._scroll.viewport(), scrollbar)
                and isinstance(event, QWheelEvent)
                and (event.angleDelta().y() > 0 or event.pixelDelta().y() > 0)
            ):
                # After a short list expands its viewport, its range may already be zero.
                self._set_title_collapsed(False)
            elif (
                at_top
                and watched is scrollbar
                and isinstance(event, QKeyEvent)
                and event.key() in (Qt.Key.Key_Home, Qt.Key.Key_Up, Qt.Key.Key_PageUp)
            ):
                self._set_title_collapsed(False)
        return super().eventFilter(watched, event)

    def set_backdrop_engine(self, engine: BackdropEngine) -> None:
        """Reuse the window's cached wallpaper; no separate image or worker lifecycle."""
        self._backdrop_engine = engine
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        engine = self._backdrop_engine
        if engine is not None and engine.active:
            engine.paint(self, painter, tint="#00000000", radius=0.0)
        else:
            # A static, palette-derived slate/paper gradient is also the loading fallback.
            gradient = QLinearGradient(QPointF(0, 0), QPointF(self.width(), self.height()))
            for stop, color in zip(
                (0.0, 0.55, 1.0),
                theme.footprint_background_colors(),
                strict=True,
            ):
                gradient.setColorAt(stop, QColor(color))
            painter.fillRect(self.rect(), gradient)
        painter.end()

    @staticmethod
    def _lock_icon() -> QIcon:
        # Native geometry, not an emoji or a glyph-dependent security promise.
        image = QPixmap(32, 32)
        image.setDevicePixelRatio(2.0)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.TEXT_SECONDARY), 1.25))
        painter.drawArc(QRectF(4, 1, 8, 9), 0, 180 * 16)
        painter.drawRoundedRect(QRectF(2.5, 6, 11, 8), 2, 2)
        painter.drawLine(QPointF(8, 9), QPointF(8, 11))
        painter.end()
        return QIcon(image)

    @staticmethod
    def _combo(name: str, values: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        combo.setAccessibleName(name)
        for label, value in values:
            combo.addItem(label, value)
        return combo

    def dispose(self) -> None:
        """Invalidate in-flight callbacks and release the page's plaintext projection."""
        self._disposed = True
        self._header_motion.stop()
        self._header_viewport._layout_timer.stop()
        self._title_settle_timer.stop()
        self._generation += 1
        self._search_timer.stop()
        self._entries.clear()
        self._filtered.clear()
        self._clear_cards()
        self.search.clear()
        self._search_timer.stop()

    def reload(self) -> None:
        if self._disposed:
            return
        self._generation += 1
        generation = self._generation
        self._loading = True
        self.refresh_button.setEnabled(False)
        self.progress.show()
        self._entries.clear()
        self._filtered.clear()
        self._clear_cards()
        self.more_button.hide()
        self.reset_button.hide()
        self.summary.setText("正在打开你的记忆…")
        self.status.setText("正在读取本机加密资料库…")
        self.status.show()

        def apply(rows: list[MemoryFootprint]) -> None:
            if generation != self._generation or self._disposed:
                return
            self._loading = False
            self.refresh_button.setEnabled(True)
            self.progress.hide()
            self._entries = list(rows)
            self._update_months()
            self._filter()

        def failed(_exc: Exception) -> None:
            if generation != self._generation or self._disposed:
                return
            self._loading = False
            self.refresh_button.setEnabled(True)
            self.progress.hide()
            self.summary.setText("足迹暂时未能打开")
            self.status.setText("读取失败，你的记忆未被改动。请点击“刷新”重试。")
            self.status.show()

        self._jobs.submit(self._service.timeline, apply, failed)

    def _update_months(self) -> None:
        selected = self.month_filter.currentData()
        months = sorted(
            {date[:7] for row in self._entries if (date := _date_key(row)) != "日期未记录"},
            reverse=True,
        )
        self.month_filter.blockSignals(True)
        self.month_filter.clear()
        self.month_filter.addItem("所有月份", "all")
        for month in months:
            self.month_filter.addItem(month.replace("-", "年") + "月", month)
        if any(_date_key(row) == "日期未记录" for row in self._entries):
            self.month_filter.addItem("日期未记录", "unknown")
        self.month_filter.setCurrentIndex(max(0, self.month_filter.findData(selected)))
        self.month_filter.blockSignals(False)

    def clear_filters(self) -> None:
        self.search.clear()
        for combo in (self.scope_filter, self.source_filter, self.month_filter):
            combo.setCurrentIndex(0)
        self._search_timer.stop()
        self._filter()

    def _filter(self) -> None:
        if self._loading or self._disposed:
            return
        query = self.search.text().strip().casefold()
        scope, source, month = (
            combo.currentData()
            for combo in (
                self.scope_filter,
                self.source_filter,
                self.month_filter,
            )
        )
        rows = []
        for row in self._entries:
            date = _date_key(row)
            places = (*row.locations, *((row.origin,) if row.origin else ()))
            searchable = "\n".join(
                [
                    row.memory.content,
                    *[f"{loc.conversation_title} {loc.branch_title}" for loc in places],
                ]
            ).casefold()
            if (scope != "all" and scope != row.scope) or (
                source != "all" and source != row.source_kind
            ):
                continue
            if month == "unknown" and date != "日期未记录":
                continue
            if month not in {"all", "unknown"} and not date.startswith(month):
                continue
            if query and query not in searchable:
                continue
            rows.append(row)
        if self.order.currentData() == "oldest":
            known = [row for row in rows if _local_time(row.memory.created_at) is not None]
            unknown = [row for row in rows if _local_time(row.memory.created_at) is None]
            rows = [*reversed(known), *unknown]
        self._filtered = rows
        self._visible_count = PAGE_SIZE
        days = {_date_key(row) for row in self._entries} - {"日期未记录"}
        self.summary.setText(
            f"{len(self._entries)} 段记忆  /  {len(days)} 个有记录的日子"
            f"    ·    当前找到 {len(rows)} 段"
        )
        self.status.setVisible(not rows)
        self.status.setText(
            "还没有足迹，但这里已经为你留好了位置。\n在对话中保存记忆，或点击“管理记忆”添加第一段。"
            if not self._entries
            else "没有找到这段足迹。\n换个词，或清除筛选再看看。"
        )
        self.reset_button.setVisible(bool(self._entries) and not rows)
        self._set_title_collapsed(False, animated=False)
        self._render()
        self._scroll.verticalScrollBar().setValue(0)

    def _clear_cards(self) -> None:
        self.cards.clear()
        while self._timeline.count():
            item = self._timeline.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _render(self) -> None:
        self._updating_timeline = True
        try:
            self._clear_cards()
            previous = ""
            group_layout: QVBoxLayout | None = None
            for row in self._filtered[: self._visible_count]:
                day = _date_key(row)
                if day != previous:
                    group = QWidget()
                    group.setObjectName("footprintDay")
                    group_layout = QVBoxLayout(group)
                    group_layout.setContentsMargins(20, 0, 0, 0)
                    group_layout.setSpacing(12)
                    moment = _local_time(row.memory.created_at)
                    heading = (
                        f"{moment.day:02d}  /  {moment.year}年{moment.month:02d}月"
                        f"  ·  星期{'一二三四五六日'[moment.weekday()]}"
                        if moment
                        else day
                    )
                    group_layout.addWidget(_label(heading, "footprintDayHeading"))
                    self._timeline.addWidget(group)
                    previous = day
                card = FootprintCard(row, self.source_requested.emit)
                self.cards.append(card)
                assert group_layout is not None
                group_layout.addWidget(card)
            self._timeline.addStretch(1)
            remaining = len(self._filtered) - self._visible_count
            self.more_button.setVisible(remaining > 0)
            self.more_button.setText(f"继续沿着时间看看  ·  还有 {remaining} 段")
        finally:
            self._updating_timeline = False

    def _more(self) -> None:
        position = self._scroll.verticalScrollBar().value()
        self._visible_count += PAGE_SIZE
        self._render()
        self._scroll.verticalScrollBar().setValue(position)
