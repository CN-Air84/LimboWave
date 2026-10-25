"""主窗口：只负责展示状态与发出命令，不持有权威业务状态。

架构约束（见设计计划 §二.1）：会话树、路由、权限、上下文一律留在应用核心。
本窗口通过信号向外表达用户意图，不自己保存或改写业务数据。

布局（设计计划 §三.1）：左栏会话导航 + 中央消息流，用分隔条可调宽度。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QWidget,
)

from limbowave.bootstrap import APP_DISPLAY_NAME
from limbowave.domain.appearance import BUILTIN_BY_ID, DEFAULT_THEME_ID, AppearanceTheme
from limbowave.ui import theme
from limbowave.ui.backdrop import BackdropEngine
from limbowave.ui.blackout import Blackout
from limbowave.ui.chat_view import ChatView
from limbowave.ui.login_page import LoginPage
from limbowave.ui.popup_material import PopupMaterials
from limbowave.ui.session_toolbar import (
    TOOLBAR_RADIUS,
    SessionToolbar,
)
from limbowave.ui.sidebar import Sidebar

_MAX_TRANSITION_PIXELS = 1_400_000
_FRAME_INTERVAL = 1 / 60
_SETTINGS_ENTER_MS = 280
_SETTINGS_EXIT_MS = 210


def _page_snapshot(page: QWidget) -> QPixmap:
    """Render at a bounded resolution to avoid large high-DPI animation frames."""
    size = page.size()
    pixels = max(1, size.width() * size.height())
    ratio = min(page.devicePixelRatioF(), math.sqrt(_MAX_TRANSITION_PIXELS / pixels))
    frame = QPixmap(max(1, round(size.width() * ratio)), max(1, round(size.height() * ratio)))
    frame.fill(QColor(theme.BG_APP))
    # Raster dimensions round independently. Map the complete logical page into
    # those exact pixels instead of using the budget ratio as a geometry scale.
    painter = QPainter(frame)
    painter.scale(frame.width() / max(1, size.width()), frame.height() / max(1, size.height()))
    page.render(painter, QPoint())
    painter.end()
    frame.setDevicePixelRatio(ratio)
    return frame


class _PageTransition(QWidget):
    """Animate bounded page snapshots without moving or relaying out live controls."""

    def __init__(
        self,
        parent: QWidget,
        outgoing: QPixmap,
        incoming: QWidget | QPixmap,
        direction: int,
        *,
        scroll_down: bool = False,
        layered: bool = False,
    ) -> None:
        super().__init__(parent)
        self._outgoing = outgoing
        self._incoming_widget = incoming if isinstance(incoming, QWidget) else None
        self._incoming_pixmap = incoming if isinstance(incoming, QPixmap) else None
        self._direction = direction
        self._scroll_down = scroll_down
        self._layered = layered and not scroll_down
        self._surface = QColor(theme.BG_APP)
        if self._layered and self._incoming_widget is not None:
            self._incoming_pixmap = _page_snapshot(self._incoming_widget)
        self._progress = 0.0
        self._last_update = 0.0
        if self._layered:
            # Paint above the page without stealing its clicks. On input, the
            # transition is dismissed so nested tab motion stays visible.
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._position_incoming(0.0)

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, value))
        if self._layered:
            # Qt already coalesces updates. A second 1/60s gate drops alternate
            # 16ms animation ticks, making this short transition visibly judder.
            self.update()
            return
        now = time.monotonic()
        if value >= 1.0 or now - self._last_update >= _FRAME_INTERVAL:
            self._last_update = now
            self.update()

    progress = Property(float, _get_progress, _set_progress)

    def _position_incoming(self, progress: float) -> None:
        incoming = self._incoming_widget
        if incoming is None:
            return
        # Keep the complex live page stationary. Only lightweight snapshots move,
        # avoiding relayout and repaint work on every tick.
        incoming.move(0, 0)

    def normalize_incoming(self) -> None:
        if self._incoming_widget is not None:
            self._incoming_widget.move(0, 0)

    @staticmethod
    def _fade(progress: float) -> float:
        value = max(0.0, min(1.0, progress))
        return value * value * (3.0 - 2.0 * value)

    def _draw_frame(self, painter: QPainter, frame: QPixmap, offset: int = 0) -> None:
        # Snapshot DPR describes its bounded raster, not the destination size.
        # A point-only draw uses rounded raster/DPR dimensions and can leave gaps
        # or crop the page at fractional screen scales and non-default sizes.
        target = QRectF(self.rect()).translated(offset, 0)
        painter.drawPixmap(target, frame, QRectF(frame.rect()))

    def _paint_layered(self, painter: QPainter) -> None:
        incoming = self._incoming_pixmap
        if incoming is None:
            return
        progress = self._progress
        painter.fillRect(self.rect(), self._surface)
        # Stagger text visibility rather than displaying two full-strength pages
        # at once. Smoothstep gives both fades a soft start and finish.
        outgoing_opacity = 1.0 - self._fade(progress / 0.48)
        incoming_opacity = self._fade((progress - 0.24) / 0.76)
        outgoing_distance, incoming_distance = (10, 24) if self._direction > 0 else (24, 10)
        if outgoing_opacity > 0.0:
            painter.setOpacity(outgoing_opacity)
            self._draw_frame(
                painter, self._outgoing, round(-self._direction * outgoing_distance * progress)
            )
        if incoming_opacity > 0.0:
            painter.setOpacity(incoming_opacity)
            self._draw_frame(
                painter,
                incoming,
                round(self._direction * incoming_distance * (1.0 - progress) ** 3),
            )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            self._layered
            and self._incoming_widget is not None
            and event.type() in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.KeyPress,
                QEvent.Type.Wheel,
            )
            and isinstance(watched, QWidget)
            and (
                watched is self._incoming_widget
                or self._incoming_widget.isAncestorOf(watched)
            )
        ):
            # This executes before the target widget processes the input.
            # Uncover the live page so its own switch animation can play.
            parent = self.parentWidget()
            window = parent.window() if parent is not None else None
            if isinstance(window, MainWindow) and window._transition is self:
                window._stop_transition()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self._layered:
            self._paint_layered(painter)
            return
        incoming = self._incoming_pixmap
        if incoming is not None:
            painter.setOpacity(self._progress)
            if self._scroll_down:
                self._draw_frame(painter, incoming)
            else:
                distance = 28
                self._draw_frame(
                    painter, incoming, round(self._direction * distance * (1.0 - self._progress))
                )
        painter.setOpacity(1.0 - self._progress)
        if self._scroll_down:
            distance = round(self.width() * 0.42)
            self._draw_frame(painter, self._outgoing, round(distance * self._progress))
        else:
            distance = 28
            self._draw_frame(
                painter, self._outgoing, round(-self._direction * distance * self._progress)
            )


class MainWindow(QMainWindow):
    """主窗口：承载导航与聊天视图，把用户意图以信号形式外发。"""

    # 用户意图出口。上层控制器负责解释命令，窗口不关心它意味着什么。
    command_requested = Signal(str)
    stop_requested = Signal()
    diagnostics_requested = Signal()
    oobe_restart_requested = Signal()

    def __init__(
        self, parent: QWidget | None = None, *, enable_oobe_debug: bool = False
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1280, 800)
        self._diagnostics_action = QAction("诊断日志", self)
        self._diagnostics_action.setShortcut("Ctrl+Shift+L")
        self._diagnostics_action.triggered.connect(self.diagnostics_requested.emit)
        self.addAction(self._diagnostics_action)
        if enable_oobe_debug:
            self._oobe_restart_action = QAction("重启并进入首次引导", self)
            self._oobe_restart_action.setShortcut("Ctrl+Shift+O")
            self._oobe_restart_action.setAutoRepeat(False)
            self._oobe_restart_action.triggered.connect(self.oobe_restart_requested.emit)
            self.addAction(self._oobe_restart_action)

        self._sidebar = Sidebar()
        self._chat_view = ChatView()
        self._history_preview: ChatView | None = None
        self._chat_stack = QStackedWidget()
        self._chat_stack.addWidget(self._chat_view)
        self._toolbar = SessionToolbar()
        self._chat_view.message_submitted.connect(self.command_requested)
        self._chat_view.stop_requested.connect(self.stop_requested)

        # 高级设置叠在输入框右半部，既不挤压输入框，也不覆盖消息区。
        content_host = QWidget()
        # 必须限定到自身：无选择器的 background 会级联进高级栏下拉的弹层窗口
        # （弹层是 combo 的子对象），把构造时的配色烘死在弹层上、压过全局 QSS 的
        # QComboBox QAbstractItemView。_on_backdrop_changed 重写时同样要保持限定。
        content_host.setObjectName("contentHost")
        content_host.setStyleSheet(f"#contentHost {{ background: {theme.BG_APP}; }}")
        content_layout = QHBoxLayout(content_host)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._chat_stack, 1)
        self._chat_view.set_advanced_panel(self._toolbar)
        self._content_host = content_host
        # 高级栏收起后整栏隐藏；展开/收起统一由输入框右上角的开关原地切换。
        self._toolbar.use_external_toggle()
        self._chat_view.advanced_toggle_requested.connect(
            lambda: self._toolbar.set_collapsed(not self._toolbar.collapsed)
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("workspaceSplitter")
        splitter.setHandleWidth(1)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(content_host)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([292, 988])
        self._splitter = splitter
        self._toolbar.collapse_toggled.connect(self._reflow_toolbar)
        # 发送消息只改变输入框停靠位置，不自动打开高级面板。
        self._chat_view.docked_changed.connect(self._on_chat_docked)
        self._toolbar.set_collapsed(not self._chat_view.docked)
        # 左栏宽度锁在一个合理区间，避免被拖成极窄/极宽
        self._sidebar.setMinimumWidth(240)
        self._sidebar.setMaximumWidth(420)

        self._workspace = splitter
        self._backdrop_engine = BackdropEngine(splitter)
        self._backdrop_engine.changed.connect(self._on_backdrop_changed)
        self._appearance_theme = BUILTIN_BY_ID[DEFAULT_THEME_ID]
        self._popup_materials = PopupMaterials(
            self, splitter, self._backdrop_engine, self._appearance_theme
        )
        self._background_image = ""
        self._backdrop_material_state: tuple[AppearanceTheme, bool, ChatView | None] | None = None
        self._backdrop_resize = QTimer(self)
        self._backdrop_resize.setSingleShot(True)
        self._backdrop_resize.setInterval(150)
        self._backdrop_resize.timeout.connect(self._prepare_backdrop)
        self._root_stack = QStackedWidget()
        self._root_stack.addWidget(self._workspace)
        self._full_page: QWidget | None = None
        self._login_exit_page: LoginPage | None = None
        self._login_fade: Blackout | None = None
        self._login_release_page = True
        self._transition: _PageTransition | None = None
        self._transition_animation: QPropertyAnimation | None = None
        self.setCentralWidget(self._root_stack)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _reflow_toolbar(self, collapsed: bool) -> None:
        """切换右半部叠盖动画，并保持明确的键盘焦点归属。"""
        focused = QApplication.focusWidget()
        restore_focus = focused is not None and self._owns_advanced_widget(focused)
        self._chat_view.set_advanced_expanded(not collapsed)
        if not collapsed:
            self._toolbar._endpoint_value.setFocus(Qt.FocusReason.OtherFocusReason)
        elif restore_focus:
            self._chat_view._advanced_btn.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_chat_docked(self, docked: bool, reveal_advanced: bool) -> None:
        # Sending or loading messages never opens advanced settings automatically.
        if not docked:
            self._toolbar.set_collapsed(True)

    def _owns_advanced_widget(self, widget: QWidget) -> bool:
        # isAncestorOf does not cross Qt.Popup boundaries; walk parentWidget instead.
        owner: QWidget | None = widget
        while owner is not None:
            if owner in (self._toolbar, self._toolbar._endpoint_panel,
                         self._chat_view._advanced_btn):
                return True
            owner = owner.parentWidget()
        return False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Nested route panels / native combo popups handle their own Escape first.
        if (not self._toolbar.collapsed and self._chat_view.isVisible()
                and isinstance(watched, QWidget) and watched.window() is self
                and isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
                and QApplication.activePopupWidget() is None
                and self._toolbar._endpoint_panel is None):
            self._toolbar.set_collapsed(True)
            self._chat_view._advanced_btn.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return super().eventFilter(watched, event)

    def show_full_page(self, page: QWidget) -> None:
        """让页面占满整个主窗口，暂时替换聊天工作区。"""
        self._toolbar.set_collapsed(True)
        interrupted_frame = self._interrupt_transition()
        self._clear_login_exit()
        outgoing = self._root_stack.currentWidget()
        animate = self.isVisible() and outgoing is not page
        old_frame = interrupted_frame if animate else None
        if old_frame is None and animate and outgoing is not None:
            old_frame = _page_snapshot(outgoing)
        if self._full_page is not None and self._full_page is not page:
            self._root_stack.removeWidget(self._full_page)
            self._full_page.deleteLater()
        self._full_page = page
        if self._root_stack.indexOf(page) < 0:
            self._root_stack.addWidget(page)
        self._root_stack.setCurrentWidget(page)
        if old_frame is not None:
            self._start_transition(old_frame, page, direction=1)

    def show_workspace(
        self, *, scroll_down: bool = False, release_page: bool = True
    ) -> None:
        """返回工作区；登录必须完整播放 Logo 绘制/消失，不使用页面滑动。"""
        old_frame = self._interrupt_transition()
        page = self._full_page
        if isinstance(page, LoginPage):
            if self._login_exit_page is not page:
                self._login_exit_page = page
                self._login_release_page = release_page
                page.exit_ready.connect(self._finish_login_exit)
            page.request_exit()
            return
        if page is not None and scroll_down:
            prepare_snapshot = getattr(page, "prepare_exit_snapshot", None)
            if callable(prepare_snapshot):
                prepare_snapshot()
        if old_frame is None and page is not None and self.isVisible():
            old_frame = _page_snapshot(page)
        self._root_stack.setCurrentWidget(self._workspace)
        self._full_page = None
        if page is not None and release_page:
            self._root_stack.removeWidget(page)
            page.deleteLater()
        if old_frame is not None:
            self._start_transition(
                old_frame,
                self._workspace,
                direction=-1,
                scroll_down=scroll_down,
            )

    def _clear_login_exit(self) -> None:
        if self._login_fade is not None:
            self._login_fade.hide()
            self._login_fade.deleteLater()
            self._login_fade = None
        if self._login_exit_page is not None:
            self._login_exit_page.exit_ready.disconnect(self._finish_login_exit)
            self._login_exit_page = None

    def _finish_login_exit(self) -> None:
        page = self._login_exit_page
        if page is None or self._full_page is not page or self._login_fade is not None:
            return
        # Install an opaque veil before switching pages, then reveal the stationary workspace.
        veil = Blackout(self._root_stack, opacity=1.0)
        self._login_fade = veil
        veil.show()
        veil.raise_()
        self._root_stack.setCurrentWidget(self._workspace)
        self._full_page = None
        veil.finished.connect(self._complete_login_exit)
        veil.fade_to(0.0, Blackout.FADE_OUT_MS)

    def _complete_login_exit(self) -> None:
        page = self._login_exit_page
        release_page = self._login_release_page
        self._clear_login_exit()
        if page is not None and release_page:
            self._root_stack.removeWidget(page)
            page.deleteLater()

    def _start_transition(
        self,
        outgoing: QPixmap,
        incoming: QWidget,
        *,
        direction: int,
        scroll_down: bool = False,
    ) -> None:
        layered = not scroll_down and not isinstance(incoming, LoginPage)
        overlay = _PageTransition(
            self._root_stack,
            outgoing,
            incoming,
            direction,
            scroll_down=scroll_down,
            layered=layered,
        )
        overlay.setGeometry(self._root_stack.rect())
        overlay._position_incoming(0.0)
        overlay.show()
        overlay.raise_()
        animation = QPropertyAnimation(overlay, b"progress", self)
        duration = _SETTINGS_ENTER_MS if direction > 0 else _SETTINGS_EXIT_MS
        animation.setDuration(duration if layered else (680 if scroll_down else 200))
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        # Layered painting owns separate fade/movement curves on a linear clock.
        animation.setEasingCurve(
            QEasingCurve.Type.Linear if layered else QEasingCurve.Type.InOutCubic
        )
        self._transition = overlay
        self._transition_animation = animation
        animation.finished.connect(self._stop_transition)
        animation.start()

    def _interrupt_transition(self) -> QPixmap | None:
        """Continue navigation from the visible composite, never a hidden endpoint."""
        overlay = self._transition
        frame = _page_snapshot(overlay) if overlay is not None and overlay._layered else None
        self._stop_transition()
        return frame

    def _stop_transition(self) -> None:
        if self._transition is not None:
            self._transition.normalize_incoming()
        if self._transition_animation is not None:
            self._transition_animation.stop()
            self._transition_animation.deleteLater()
            self._transition_animation = None
        if self._transition is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self._transition)
            self._transition.hide()
            self._transition.deleteLater()
            self._transition = None

    @property
    def transition_active(self) -> bool:
        return self._transition is not None or self._login_exit_page is not None

    @property
    def showing_full_page(self) -> bool:
        return self._root_stack.currentWidget() is not self._workspace

    @property
    def chat(self) -> ChatView:
        """聊天视图（供上层接线）。注意这是访问器，不在实例字典里。"""
        return self._chat_view

    @property
    def history_preview(self) -> ChatView | None:
        """Draft-only history surface; the live chat keeps receiving its own events."""
        return self._history_preview

    def prepare_history_preview(self) -> ChatView:
        if self._history_preview is None:
            self._history_preview = ChatView(draft_only=True)
            self._chat_stack.addWidget(self._history_preview)
            self._on_backdrop_changed()
        return self._history_preview

    def show_history_preview(self) -> None:
        if self._history_preview is not None:
            self._chat_stack.setCurrentWidget(self._history_preview)

    def clear_history_preview(self) -> None:
        preview, self._history_preview = self._history_preview, None
        self._chat_stack.setCurrentWidget(self._chat_view)
        if preview is not None:
            self._chat_stack.removeWidget(preview)
            preview.deleteLater()

    @property
    def sidebar(self) -> Sidebar:
        """导航栏（供上层接线）。注意这是访问器，不在实例字典里。"""
        return self._sidebar

    @property
    def toolbar(self) -> SessionToolbar:
        """右侧会话工具栏（供上层接线）。访问器，不在实例字典里。"""
        return self._toolbar

    def set_appearance_theme(self, definition: AppearanceTheme, image_path: str = "") -> None:
        """Install a complete theme snapshot and request its shared background frames."""
        self._appearance_theme = definition
        self._background_image = image_path
        self._prepare_backdrop()

    def set_backdrop(self, image_path: str, radius: int) -> None:
        """Compatibility wrapper for callers that only provide one blur radius."""
        background = replace(self._appearance_theme.background, asset=image_path)
        materials = replace(
            self._appearance_theme.materials,
            content_blur_radius=radius,
            sidebar_blur_radius=radius,
        )
        self._appearance_theme = replace(
            self._appearance_theme, background=background, materials=materials
        )
        self._background_image = image_path
        if image_path:
            self._backdrop_engine.prepare(image_path, radius)
        else:
            self._backdrop_engine.clear()
        self._on_backdrop_changed()

    @staticmethod
    def _tint(color: str, opacity: float) -> str:
        tint = QColor(color)
        tint.setAlphaF(max(0.0, min(1.0, opacity)))
        return tint.name(QColor.NameFormat.HexArgb)

    def _prepare_backdrop(self) -> None:
        definition = self._appearance_theme
        if not self._background_image or not definition.background.asset:
            self._backdrop_engine.clear()
            return
        materials = definition.materials
        self._backdrop_engine.request(
            self._background_image,
            definition.background,
            definition.colors.background,
            (materials.content_blur_radius, materials.sidebar_blur_radius),
        )
        self._on_backdrop_changed()

    def _on_backdrop_changed(self) -> None:
        definition = self._appearance_theme
        materials = definition.materials
        engine = self._backdrop_engine
        state = (definition, engine.active, self._history_preview)
        if state == self._backdrop_material_state:
            # A new wallpaper frame changes pixels, not styles or widget layout.
            self._repaint_backdrop()
            self._popup_materials.changed.emit()
            return
        self._backdrop_material_state = state
        sidebar_active = engine.active and materials.sidebar_enabled
        content_active = engine.active and materials.content_enabled
        self._sidebar.setStyleSheet(
            "Sidebar { background: transparent; }"
            if sidebar_active
            else f"Sidebar {{ background: {theme.sidebar_surface()}; }}"
        )
        self._sidebar.set_backdrop(
            engine if sidebar_active else None,
            tint=self._tint(definition.colors.card, materials.sidebar_opacity),
            radius=materials.sidebar_blur_radius,
        )
        for view in (self._chat_view, self._history_preview):
            if view is None:
                continue
            view.setStyleSheet(
                "ChatView { background: transparent; }"
                if content_active
                else f"ChatView {{ background: {theme.BG_APP}; }}"
            )
            view.set_backdrop_engine(
                engine if content_active else None,
                tint=self._tint(definition.colors.background, materials.content_opacity),
                radius=materials.content_blur_radius,
            )
        self._toolbar.set_backdrop(
            engine if content_active else None,
            tint=self._tint(definition.colors.card, materials.content_opacity),
            radius=materials.content_blur_radius,
        )
        self._toolbar.refresh_control_styles()
        toolbar_bg = "transparent" if content_active else theme.BG_SURFACE
        self._toolbar.setStyleSheet(
            f"SessionToolbar {{ background: {toolbar_bg};"
            f" border: 1px solid {theme.container_border()};"
            f" border-radius: {TOOLBAR_RADIUS}px; }}"
        )
        self._content_host.setStyleSheet(
            "#contentHost { background: transparent; }"
            if content_active
            else f"#contentHost {{ background: {theme.BG_APP}; }}"
        )
        self._repaint_backdrop()
        self._popup_materials.set_theme(definition)

    def _repaint_backdrop(self) -> None:
        for pane in (self._sidebar, self._chat_view, self._toolbar, self._history_preview):
            if pane is not None:
                pane.update()

    _close_guard: Callable[[], bool] | None = None

    def set_close_guard(self, guard: Callable[[], bool]) -> None:
        self._close_guard = guard

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._close_guard is not None and not self._close_guard():
            event.ignore()
            return
        page = self._full_page
        resolver = getattr(page, "resolve_pending_changes", None)
        if callable(resolver) and not resolver():
            event.ignore()
            return
        self._backdrop_engine.close()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """缩放时更新页面过渡与背景；输入区由布局自行重排。"""
        self._stop_transition()  # Snapshots no longer match after a resize.
        super().resizeEvent(event)
        if self._background_image:
            # Keep stretching the last frame until resize settles. Even a completed
            # worker result queued for GUI delivery must not install an obsolete size.
            self._backdrop_engine.discard_pending()
            self._backdrop_resize.start()
