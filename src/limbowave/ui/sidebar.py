"""左栏：会话列表、新建会话、历史搜索、设置与日志入口。

架构约束（设计计划 §三.1 / §二.1）：
- 本视图持有**展示态**（列表里显示什么），不持有权威业务状态。
- 用户意图一律经信号外发：``conversation_selected`` / ``new_conversation_requested``
  / ``search_changed`` / ``settings_requested`` / ``request_log_requested``
  / ``export_requested`` / ``backup_requested`` / ``restore_requested``。
- 列表内容由上层（app 接线）经 :meth:`show_conversations` / :meth:`show_search_results`
  灌入；视图不自己查询仓库。

当前会话指示：消息区正在显示的会话行左侧画一条强调色竖条、标题加粗
（由上层经 :meth:`set_active` 标记，和鼠标选中高亮区分开）。

分支：双击会话行展开/收起它的分支子行（展开时发 ``branches_requested``，上层经
:meth:`set_branches` 灌入），单击分支子行外发 ``branch_switch_requested``。

视觉：会话行是双行排版（标题 + 副信息）。用 ``QStyledItemDelegate`` 绘制，
而不是 ``setItemWidget``——行组件在列表里会引入额外的布局与事件开销，
绘制代理是 Qt 对「富文本行」的正规做法。样式令牌在 :mod:`theme`。
"""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QMargins,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.backdrop import BackdropEngine

# 列表项 data 角色
ROLE_CONVERSATION_ID = Qt.ItemDataRole.UserRole
ROLE_HIT_KIND = Qt.ItemDataRole.UserRole + 1  # "conversation" | "hit" | "branch"
ROLE_TITLE = Qt.ItemDataRole.UserRole + 2
ROLE_SUBTITLE = Qt.ItemDataRole.UserRole + 3
ROLE_BRANCH_ID = Qt.ItemDataRole.UserRole + 4
ROLE_ACTIVE = Qt.ItemDataRole.UserRole + 5  # 是否是消息区正在显示的会话/分支
ROLE_BRANCH_REVEAL = Qt.ItemDataRole.UserRole + 6

ROW_HEIGHT = 54  # 双行排版的行高
BRANCH_ROW_HEIGHT = 30  # 分支子行单行


class _ConversationDelegate(QStyledItemDelegate):
    """双行绘制：主行标题（主文字色）+ 副行信息（次要文字色）。分支子行单行缩进。"""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        # 基类先画 选中/悬停 背景
        super().paint(painter, option, index)

        title = index.data(ROLE_TITLE) or ""
        subtitle = index.data(ROLE_SUBTITLE) or ""
        if not title:
            return

        rect: QRect = option.rect
        active = bool(index.data(ROLE_ACTIVE))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if index.data(ROLE_HIT_KIND) == "branch":
            painter.setOpacity(float(index.data(ROLE_BRANCH_REVEAL) or 0.0))
            self._paint_branch(painter, option, rect, title, subtitle, active)
            painter.restore()
            return

        title_font = QFont(option.font)
        title_font.setPixelSize(13)
        title_font.setBold(active)
        subtitle_font = QFont(option.font)
        subtitle_font.setPixelSize(11)

        text_left = rect.left() + 12
        text_width = rect.width() - 24

        painter.setFont(title_font)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.drawText(
            QRect(text_left, rect.top() + 9, text_width, 20),
            Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        painter.setFont(subtitle_font)
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.drawText(
            QRect(text_left, rect.top() + 30, text_width, 16),
            Qt.AlignmentFlag.AlignVCenter,
            subtitle,
        )

        painter.restore()

    @staticmethod
    def _paint_branch(
        painter: QPainter,
        option: QStyleOptionViewItem,
        rect: QRect,
        title: str,
        subtitle: str,
        active: bool,
    ) -> None:
        """分支子行：缩进圆点（当前分支实心强调色）+ 标签 + 右侧消息数。"""
        dot_x = rect.left() + 24
        dot_y = rect.center().y()
        painter.setPen(QPen(QColor(theme.ACCENT if active else theme.TEXT_SECONDARY), 1.5))
        if active:
            painter.setBrush(QColor(theme.ACCENT))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPoint(dot_x, dot_y), 3, 3)

        font = QFont(option.font)
        font.setPixelSize(12)
        font.setBold(active)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_PRIMARY if active else theme.TEXT_SECONDARY))
        text_rect = QRect(dot_x + 10, rect.top(), rect.right() - dot_x - 20, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, title)
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, subtitle
        )

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        height = BRANCH_ROW_HEIGHT if index.data(ROLE_HIT_KIND) == "branch" else ROW_HEIGHT
        return QSize(option.rect.width(), height)


def _search_icon() -> QIcon:
    """绘制图标，避免系统缺少 emoji 字体时按钮只剩一条竖线。"""
    image = QPixmap(20, 20)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(theme.TEXT_PRIMARY), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawEllipse(3, 2, 11, 11)
    painter.drawLine(13, 13, 18, 18)
    painter.end()
    return QIcon(image)


class Sidebar(QWidget):
    """会话导航栏。搜索框有内容时列表显示搜索结果，否则显示会话列表。"""

    conversation_selected = Signal(str)  # conversation_id
    new_conversation_requested = Signal()
    search_requested = Signal()  # 搜索按钮被点：上层呼出搜索悬浮窗
    search_changed = Signal(str)  # 悬浮窗里的查询词变化（保留旧通道）
    settings_requested = Signal()
    rename_requested = Signal(str)  # conversation_id（输入框由上层做）
    delete_requested = Signal(str)  # conversation_id（确认框由上层做）
    branches_requested = Signal(str)  # conversation_id：展开分支时请上层灌入
    branch_switch_requested = Signal(str, str)  # (conversation_id, branch_id)
    branch_rename_requested = Signal(str, str)  # (conversation_id, branch_id)
    branch_delete_requested = Signal(str, str)  # (conversation_id, branch_id)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backdrop_engine: BackdropEngine | None = None
        self._backdrop_tint = "#00000000"
        self._backdrop_radius = 0.0
        self._searching = False
        self._loading_branches: set[str] = set()
        self._active_conversation_id: str | None = None
        self._active_branch_id: str | None = None
        self._expanded: set[str] = set()
        self._branch_animations: dict[str, QVariantAnimation] = {}
        self._defer_branch_insert: set[str] = set()
        # conversation_id -> [(branch_id, label, message_count)]
        self._branches: dict[str, list[tuple[str, str, int]]] = {}
        self._build()

    # ---------- 布局 ----------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 12, 10, 10)
        root.setSpacing(8)

        # 顶部：新建会话（左）+ 搜索（右）。搜索是按钮呼出悬浮窗，不再常驻输入框。
        top = QHBoxLayout()
        top.setSpacing(6)
        self._new_btn = QPushButton("＋ 新建会话")
        self._new_btn.setProperty("accent", True)
        self._new_btn.clicked.connect(self.new_conversation_requested.emit)
        top.addWidget(self._new_btn, 1)
        self._search_btn = QPushButton()
        self._search_btn.setIcon(_search_icon())
        self._search_btn.setFixedSize(44, 38)
        self._search_btn.setStyleSheet("QPushButton { padding: 0; margin: 0; }")
        self._search_btn.setToolTip("搜索历史")
        self._search_btn.clicked.connect(self.search_requested.emit)
        top.addWidget(self._search_btn)
        root.addLayout(top)

        self._list = QListWidget()
        # 光效轮廓与全局 QListWidget::item 的圆角、外边距保持一致，分支行也复用。
        self._list.setProperty("revealItemRadius", theme.RADIUS_MD)
        self._list.setProperty("revealItemMargins", QMargins(2, 1, 2, 1))
        self._list.setItemDelegate(_ConversationDelegate(self._list))
        self._list.currentItemChanged.connect(self._on_current_changed)
        self._list.itemDoubleClicked.connect(self._on_double_clicked)
        self._list.setSpacing(2)
        # 分支子行更矮，行高不统一，不能 setUniformItemSizes
        # 右键菜单：重命名 / 删除（Task 3.1）
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._active_indicator = QFrame(self._list.viewport())
        self._active_indicator.setObjectName("conversationActiveIndicator")
        self._active_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._active_indicator.setStyleSheet(
            f"background: {theme.ACCENT}; border: none; border-radius: 1px;"
        )
        self._active_indicator.hide()
        self._indicator_animation = QPropertyAnimation(
            self._active_indicator, b"geometry", self
        )
        self._indicator_animation.setDuration(260)
        self._indicator_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._list.viewport().installEventFilter(self)
        self._list.verticalScrollBar().valueChanged.connect(
            lambda _value: self._sync_active_indicator(animated=False)
        )
        root.addWidget(self._list, 1)
        self._branch_loading = QWidget(self)
        loading_layout = QHBoxLayout(self._branch_loading)
        loading_layout.setContentsMargins(6, 0, 6, 0)
        loading_layout.addWidget(QLabel("正在加载分支…"))
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedHeight(4)
        progress.setAccessibleName("正在加载分支")
        loading_layout.addWidget(progress, 1)
        root.addWidget(self._branch_loading)
        self._branch_loading.hide()

        # 左下角：唯一入口「设置」（站点与模型 / 数据 / 请求日志都收进设置页面）
        self._settings_btn = QPushButton("设置")
        self._settings_btn.setProperty("flat", True)
        self._settings_btn.clicked.connect(self.settings_requested.emit)
        root.addWidget(self._settings_btn)

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
            self._backdrop_engine.paint(
                self, painter, tint=self._backdrop_tint, radius=self._backdrop_radius
            )
            painter.end()

    def restyle(self) -> None:
        """图标不是 QSS，切换主题时重新绘制。"""
        self._search_btn.setIcon(_search_icon())
        self._active_indicator.setStyleSheet(
            f"background: {theme.ACCENT}; border: none; border-radius: 1px;"
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._list.viewport() and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self._sync_active_indicator(animated=False)
        return super().eventFilter(watched, event)

    # ---------- 展示更新（由上层驱动） ----------

    def show_conversations(self, rows: list[tuple[str, str, int]]) -> None:
        """显示会话列表。每行 (conversation_id, title, message_count)。

        重建列表时保留展开状态与当前会话指示，且不外发选中信号。
        """
        self._searching = False
        self._stop_branch_animations()
        self._expanded &= {row[0] for row in rows}
        self._list.blockSignals(True)
        self._list.clear()
        for conversation_id, title, count in rows:
            item = QListWidgetItem()
            item.setData(ROLE_CONVERSATION_ID, conversation_id)
            item.setData(ROLE_HIT_KIND, "conversation")
            item.setData(ROLE_TITLE, title)
            item.setData(ROLE_SUBTITLE, f"{count} 条消息")
            item.setData(ROLE_ACTIVE, conversation_id == self._active_conversation_id)
            item.setToolTip(f"{title}\n双击展开/收起分支")
            item.setSizeHint(QSize(-1, ROW_HEIGHT))
            self._list.addItem(item)
            if conversation_id == self._active_conversation_id:
                self._list.setCurrentItem(item)
            if conversation_id in self._expanded:
                self._insert_branch_rows(item)
        self._list.blockSignals(False)
        self._sync_active_indicator(animated=False)

    def show_search_results(self, rows: list[tuple[str, str]]) -> None:
        """显示搜索结果。每行 (conversation_id, 摘要文本)。"""
        self._searching = True
        self._stop_branch_animations()
        self._indicator_animation.stop()
        self._active_indicator.hide()
        self._list.clear()
        if not rows:
            item = QListWidgetItem("（无匹配）")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            return
        for conversation_id, snippet in rows:
            item = QListWidgetItem()
            item.setData(ROLE_CONVERSATION_ID, conversation_id)
            item.setData(ROLE_HIT_KIND, "hit")
            item.setData(ROLE_TITLE, snippet)
            item.setData(ROLE_SUBTITLE, "搜索命中")
            item.setSizeHint(QSize(-1, ROW_HEIGHT))
            self._list.addItem(item)

    def set_active(self, conversation_id: str | None, branch_id: str | None = None) -> None:
        """标记消息区正在显示的会话（和分支）。纯展示，不外发信号。"""
        previous_conversation_id = self._active_conversation_id
        self._active_conversation_id = conversation_id
        self._active_branch_id = branch_id
        if self._searching:
            return
        self._list.blockSignals(True)
        current: QListWidgetItem | None = None
        for row in range(self._list.count()):
            item = self._list.item(row)
            cid = item.data(ROLE_CONVERSATION_ID)
            if item.data(ROLE_HIT_KIND) == "branch":
                is_active = cid == conversation_id and item.data(ROLE_BRANCH_ID) == branch_id
                if is_active:
                    current = item  # 展开时选中落在当前分支上
            else:
                is_active = cid is not None and cid == conversation_id
                if is_active and current is None:
                    current = item
            item.setData(ROLE_ACTIVE, is_active)
        if current is not None:
            self._list.setCurrentItem(current)
        else:
            self._list.setCurrentRow(-1)
        self._list.blockSignals(False)
        self._list.viewport().update()
        self._sync_active_indicator(
            animated=(
                previous_conversation_id is not None
                and conversation_id is not None
                and previous_conversation_id != conversation_id
            )
        )

    def _indicator_target(self) -> QRect | None:
        if self._searching or self._active_conversation_id is None:
            return None
        item = self._conversation_item(self._active_conversation_id)
        if item is None:
            return None
        row = self._list.visualItemRect(item)
        if row.isEmpty() or not row.intersects(self._list.viewport().rect()):
            return None
        return QRect(2, row.top() + 10, 3, max(3, row.height() - 20))

    def _sync_active_indicator(self, *, animated: bool) -> None:
        target = self._indicator_target()
        if target is None:
            self._indicator_animation.stop()
            self._active_indicator.hide()
            return
        current = self._active_indicator.geometry()
        can_animate = animated and self.isVisible() and self._active_indicator.isVisible()
        if can_animate and current != target:
            self._indicator_animation.stop()
            self._indicator_animation.setStartValue(current)
            self._indicator_animation.setEndValue(target)
            self._active_indicator.show()
            self._active_indicator.raise_()
            self._indicator_animation.start()
        else:
            if self._indicator_animation.state() == QAbstractAnimation.State.Running:
                self._indicator_animation.stop()
            self._active_indicator.setGeometry(target)
            self._active_indicator.show()
            self._active_indicator.raise_()

    def branches_loading(self, conversation_id: str) -> bool:
        return conversation_id in self._loading_branches

    def set_branches_loading(self, conversation_id: str, loading: bool) -> None:
        if loading:
            self._loading_branches.add(conversation_id)
        else:
            self._loading_branches.discard(conversation_id)
        self._branch_loading.setVisible(bool(self._loading_branches))

    def set_history_loading(self, loading: bool) -> None:
        """只锁定会话导航；窗口及设置入口仍可响应。"""
        self._list.setEnabled(not loading)
        self._new_btn.setEnabled(not loading)
        self._search_btn.setEnabled(not loading)

    def set_branches(self, conversation_id: str, branches: list[tuple[str, str, int]]) -> None:
        """灌入某会话的分支 (branch_id, label, message_count)。已展开则就地刷新。"""
        self._branches[conversation_id] = list(branches)
        if (
            self._searching
            or conversation_id not in self._expanded
            or conversation_id in self._defer_branch_insert
        ):
            return
        parent = self._conversation_item(conversation_id)
        if parent is None:
            return
        animate = not self._branch_items(parent)
        self._list.blockSignals(True)
        self._remove_branch_rows(parent)
        items = self._insert_branch_rows(parent, revealed=not animate)
        self._list.blockSignals(False)
        if animate:
            self._animate_branch_rows(conversation_id, items, expanding=True)

    def toggle_branches(self, conversation_id: str) -> None:
        """展开/收起会话的分支子行（双击的程序化入口）。"""
        if self._searching:
            return
        parent = self._conversation_item(conversation_id)
        if parent is None:
            return
        if conversation_id in self._expanded:
            self._expanded.discard(conversation_id)
            items = self._branch_items(parent)
            if items:
                self._animate_branch_rows(conversation_id, items, expanding=False)
        else:
            self._expanded.add(conversation_id)
            self._defer_branch_insert.add(conversation_id)
            self.branches_requested.emit(conversation_id)
            self._defer_branch_insert.discard(conversation_id)
            parent = self._conversation_item(conversation_id)
            if parent is not None:
                self._list.blockSignals(True)
                self._remove_branch_rows(parent)
                items = self._insert_branch_rows(parent, revealed=False)
                self._list.blockSignals(False)
                self._animate_branch_rows(conversation_id, items, expanding=True)

    def is_expanded(self, conversation_id: str) -> bool:
        return conversation_id in self._expanded

    def select_conversation(self, conversation_id: str) -> None:
        """程序化选中（如新建会话后）。不触发额外逻辑——选中即发送意图。"""
        item = self._conversation_item(conversation_id)
        if item is not None:
            self._list.setCurrentItem(item)

    # ---------- 内部 ----------

    def _conversation_item(self, conversation_id: str) -> QListWidgetItem | None:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if (
                item.data(ROLE_HIT_KIND) in ("conversation", "hit")
                and item.data(ROLE_CONVERSATION_ID) == conversation_id
            ):
                return item
        return None

    def _insert_branch_rows(
        self, parent: QListWidgetItem, *, revealed: bool = True
    ) -> list[QListWidgetItem]:
        conversation_id = parent.data(ROLE_CONVERSATION_ID)
        row = self._list.row(parent) + 1
        inserted: list[QListWidgetItem] = []
        for branch_id, label, count in self._branches.get(conversation_id, []):
            item = QListWidgetItem()
            item.setData(ROLE_CONVERSATION_ID, conversation_id)
            item.setData(ROLE_HIT_KIND, "branch")
            item.setData(ROLE_BRANCH_ID, branch_id)
            item.setData(ROLE_TITLE, label)
            item.setData(ROLE_SUBTITLE, str(count))
            is_active = (
                conversation_id == self._active_conversation_id
                and branch_id == self._active_branch_id
            )
            item.setData(ROLE_ACTIVE, is_active)
            item.setData(ROLE_BRANCH_REVEAL, 1.0 if revealed else 0.0)
            item.setToolTip(f"{label} · {count} 条消息")
            item.setSizeHint(QSize(-1, BRANCH_ROW_HEIGHT if revealed else 0))
            self._list.insertItem(row, item)
            inserted.append(item)
            if is_active:
                self._list.setCurrentItem(item)
            row += 1
        return inserted

    def _branch_items(self, parent: QListWidgetItem) -> list[QListWidgetItem]:
        row = self._list.row(parent) + 1
        items: list[QListWidgetItem] = []
        while row < self._list.count() and self._list.item(row).data(ROLE_HIT_KIND) == "branch":
            items.append(self._list.item(row))
            row += 1
        return items

    def _animate_branch_rows(
        self,
        conversation_id: str,
        items: list[QListWidgetItem],
        *,
        expanding: bool,
    ) -> None:
        previous = self._branch_animations.pop(conversation_id, None)
        if previous is not None:
            previous.stop()
        if not items:
            return
        animation = QVariantAnimation(self)
        animation.setDuration(190)
        easing = QEasingCurve.Type.OutCubic if expanding else QEasingCurve.Type.InCubic
        animation.setEasingCurve(easing)
        animation.setStartValue(0.0 if expanding else 1.0)
        animation.setEndValue(1.0 if expanding else 0.0)

        def _apply(value: object) -> None:
            progress = cast(float, value)
            for item in items:
                if self._list.row(item) < 0:
                    continue
                item.setData(ROLE_BRANCH_REVEAL, progress)
                item.setSizeHint(QSize(-1, round(BRANCH_ROW_HEIGHT * progress)))
            self._list.viewport().update()
            self._sync_active_indicator(animated=False)

        def _finished() -> None:
            self._branch_animations.pop(conversation_id, None)
            if not expanding:
                parent = self._conversation_item(conversation_id)
                if parent is not None:
                    self._list.blockSignals(True)
                    self._remove_branch_rows(parent)
                    self._list.blockSignals(False)
            self._sync_active_indicator(animated=False)

        animation.valueChanged.connect(_apply)
        animation.finished.connect(_finished)
        self._branch_animations[conversation_id] = animation
        animation.start()

    def _stop_branch_animations(self) -> None:
        for animation in self._branch_animations.values():
            animation.stop()
        self._branch_animations.clear()

    def _remove_branch_rows(self, parent: QListWidgetItem) -> None:
        row = self._list.row(parent) + 1
        while row < self._list.count() and self._list.item(row).data(ROLE_HIT_KIND) == "branch":
            removed = self._list.takeItem(row)
            if removed is not None and removed.data(ROLE_ACTIVE):
                self._list.setCurrentItem(parent)

    def _on_double_clicked(self, item: QListWidgetItem) -> None:
        if item.data(ROLE_HIT_KIND) == "conversation":
            self.toggle_branches(item.data(ROLE_CONVERSATION_ID))

    def _on_context_menu(self, pos: QPoint) -> None:
        """会话行的右键菜单。搜索态下不提供（搜索结果不是管理对象）。"""
        if self._searching:
            return
        item = self._list.itemAt(pos)
        if item is None:
            return
        conversation_id = item.data(ROLE_CONVERSATION_ID)
        if not conversation_id:
            return
        menu = QMenu(self)
        if item.data(ROLE_HIT_KIND) == "branch":
            branch_id = item.data(ROLE_BRANCH_ID)
            rename_action = menu.addAction("重命名分支")
            delete_action = menu.addAction("删除分支")
            chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
            if chosen is rename_action:
                self.branch_rename_requested.emit(conversation_id, branch_id)
            elif chosen is delete_action:
                self.branch_delete_requested.emit(conversation_id, branch_id)
            return
        if item.data(ROLE_HIT_KIND) != "conversation":
            return
        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除会话")
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is rename_action:
            self.rename_requested.emit(conversation_id)
        elif chosen is delete_action:
            self.delete_requested.emit(conversation_id)

    def _on_current_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        conversation_id = current.data(ROLE_CONVERSATION_ID)
        if not conversation_id:
            return
        if current.data(ROLE_HIT_KIND) == "branch":
            branch_id = current.data(ROLE_BRANCH_ID)
            is_active = (
                conversation_id == self._active_conversation_id
                and branch_id == self._active_branch_id
            )
            if not is_active:
                self.branch_switch_requested.emit(conversation_id, branch_id)
            return
        # 已经在显示的会话不重复打开（双击的第一击也会走到这里）
        if conversation_id != self._active_conversation_id:
            self.conversation_selected.emit(conversation_id)
