"""全窗口设置页面：纵向分类栏 + 带过渡动画的内容区域。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from limbowave.domain.models import ActualModel
from limbowave.domain.providers import EndpointConfig
from limbowave.ui import theme
from limbowave.ui.animated_stack import AnimatedPageStack, AnimatedTabBar

if TYPE_CHECKING:
    from limbowave.application.services.model_probe import (
        DiscoveryResult,
        ModelProbeProgress,
        ModelProbeResult,
    )
    from limbowave.ui.about_page import AboutPage
    from limbowave.ui.gentle_presence import PresenceSettingsPage
    from limbowave.ui.logical_models_tab import LogicalModelsTab
    from limbowave.ui.model_probe_page import (
        ActualModelsPage,
        ModelCapabilityChange,
        ModelProbeTask,
    )
    from limbowave.ui.reminder_notifications import ReminderNotificationSettingsPage
    from limbowave.ui.session_memory_panel import MemoryPanel
    from limbowave.ui.settings_dialog import _AppearanceTab, _EndpointsTab

def _hint_style() -> str:
    # Lazy pages may be created after a theme/font change.
    return f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px;"


_AnimatedPageStack = AnimatedPageStack


class _VerticalTabRail(QFrame):
    """纵向选项卡栏；选中指示器独立于按钮平滑移动。"""

    tab_selected = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsRail")
        self.setFixedWidth(190)
        self._buttons: list[QPushButton] = []
        self._group_labels: list[QLabel] = []
        self._current = 0
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self._on_clicked)
        self._indicator = QFrame(self)
        self._indicator.setObjectName("settingsIndicator")
        self._indicator.setFixedWidth(4)
        self._indicator.raise_()
        self._indicator_animation = QPropertyAnimation(self._indicator, b"geometry", self)
        self._indicator_animation.setDuration(230)
        self._indicator_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 18, 12, 18)
        self._layout.setSpacing(4)
        self._layout.addStretch(1)

    @property
    def current_index(self) -> int:
        return self._current

    @property
    def group_titles(self) -> tuple[str, ...]:
        return tuple(label.text() for label in self._group_labels)

    def add_group(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("settingsGroupLabel")
        label.setFixedHeight(24)
        self._layout.insertWidget(self._layout.count() - 1, label)
        self._group_labels.append(label)

    def add_tab(self, text: str) -> int:
        index = len(self._buttons)
        button = QPushButton(text)
        button.setCheckable(True)
        button.setProperty("settingsTab", True)
        # 分类按钮自己画悬浮/选中底色。悬停停用会把底色写进按钮自身样式表，
        # 自身样式表压过页面样式表，:checked 底色会在悬停期间被一并抹掉。
        button.setProperty("themeEffectDisabled", True)
        button.setFixedHeight(42)
        self._layout.insertWidget(self._layout.count() - 1, button)
        self._buttons.append(button)
        self._group.addButton(button, index)
        if index == 0:
            button.setChecked(True)
        return index

    def index_of(self, text: str) -> int:
        for index, button in enumerate(self._buttons):
            if button.text() == text:
                return index
        return -1

    def set_current(self, index: int, *, animated: bool = True) -> None:
        if not 0 <= index < len(self._buttons):
            return
        self._current = index
        self._buttons[index].setChecked(True)
        target = self._indicator_rect(index)
        if animated and self.isVisible() and self._indicator.geometry().isValid():
            self._indicator_animation.stop()
            self._indicator_animation.setStartValue(self._indicator.geometry())
            self._indicator_animation.setEndValue(target)
            self._indicator_animation.start()
        else:
            self._indicator.setGeometry(target)
        self._indicator.raise_()

    def _on_clicked(self, index: int) -> None:
        if index == self._current:
            return
        self.set_current(index)
        self.tab_selected.emit(index)

    def _indicator_rect(self, index: int) -> QRect:
        button = self._buttons[index]
        point = button.mapTo(self, QPoint(0, 0))
        height = min(26, button.height() - 10)
        return QRect(4, point.y() + (button.height() - height) // 2, 4, height)

    def _place_indicator(self) -> None:
        if self._buttons:
            self.set_current(self._current, animated=False)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._place_indicator()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if self._indicator_animation.state() != QAbstractAnimation.State.Running:
            self._place_indicator()


class _DataSecurityTab(QScrollArea):
    """数据与安全页：系统保护恢复、备份、恢复与重置入口。"""

    backup_requested = Signal()
    restore_requested = Signal()
    reset_requested = Signal()

    def __init__(self, vault: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        if vault is not None:
            from limbowave.ui.settings_dialog import _SecurityTab

            security = _SecurityTab(vault, content)
            security_layout = security.layout()
            assert security_layout is not None
            security_layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(security, 0, Qt.AlignmentFlag.AlignTop)
            layout.addSpacing(20)
        heading = QLabel("备份与恢复")
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)
        note = QLabel(
            "备份使用独立密码（与主密码无关），生成 .lwb 加密包；\n"
            "恢复前会自动校验并保留当前资料库，完成后自动重启，失败自动回退。\n"
            "重启后使用备份密码解锁，无需手动操作文件夹。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(_hint_style())
        layout.addWidget(note)
        row = QHBoxLayout()
        backup = QPushButton("生成加密备份…")
        backup.setProperty("accent", True)
        backup.clicked.connect(self.backup_requested.emit)
        row.addWidget(backup)
        restore = QPushButton("从备份恢复…")
        restore.clicked.connect(self.restore_requested.emit)
        row.addWidget(restore)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(20)
        reset_heading = QLabel("重置数据")
        reset_heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(reset_heading)
        warning = QLabel("重置会直接删除所选范围的数据，无法撤销；请按需先自行备份。")
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {theme.DANGER_TEXT};")
        layout.addWidget(warning)
        reset = QPushButton("重置数据…")
        reset.setObjectName("resetDataButton")
        reset.clicked.connect(self.reset_requested.emit)
        layout.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)


class _RequestLogTab(QWidget):
    """请求日志页：有会话时内嵌查看器，否则说明原因。"""

    def __init__(
        self,
        request_logs: Any,
        conversation_id: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if conversation_id is None or request_logs is None:
            hint = QLabel("当前没有打开的会话：先聊一轮，或选择一个历史会话。")
            hint.setWordWrap(True)
            hint.setStyleSheet(_hint_style())
            layout.addWidget(hint)
            layout.addStretch(1)
            return
        from limbowave.ui.request_log_dialog import RequestLogDialog

        viewer = RequestLogDialog(request_logs, conversation_id, self)
        viewer.setWindowFlags(Qt.WindowType.Widget)
        layout.addWidget(viewer, 1)


class _EndpointWorkspace(QWidget):
    """共享站点列表与右侧二级选项卡并列，配置和实际模型共用当前站点。"""

    def __init__(
        self,
        configuration: _EndpointsTab,
        actual_models: ActualModelsPage,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self._splitter)
        self._splitter.addWidget(configuration.sidebar)
        content = QWidget()
        self._splitter.addWidget(content)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([230, 520])
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        host = QWidget()
        host.setObjectName("endpointSubtabHost")
        host_layout = QHBoxLayout(host)
        host_layout.setContentsMargins(4, 0, 4, 0)
        host_layout.setSpacing(0)
        host_layout.addStretch(1)
        self._subtabs = AnimatedTabBar(indicator_name="endpointSubtabIndicator")
        self._subtabs.setObjectName("endpointSubtabs")
        self._subtabs.setShape(AnimatedTabBar.Shape.RoundedNorth)
        self._subtabs.setExpanding(False)
        self._subtabs.setDocumentMode(True)
        self._subtabs.setDrawBase(False)
        self._subtabs.setUsesScrollButtons(True)
        host_layout.addWidget(self._subtabs)
        host_layout.addStretch(1)
        root.addWidget(host)

        self._stack = AnimatedPageStack(orientation=Qt.Orientation.Horizontal)
        self._stack.setObjectName("endpointSubtabStack")
        root.addWidget(self._stack, 1)
        for title, content in (("端点配置", configuration), ("实际模型", actual_models)):
            page = QWidget()
            page.setObjectName("endpointSubtabPage")
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(18, 14, 18, 18)
            page_layout.addWidget(content, 1)
            self._subtabs.addTab(title)
            self._stack.add_page(page)
        self._animate_switch = True
        self._subtabs.currentChanged.connect(self._switch_subtab)
        self._subtabs.setCurrentIndex(0)
        self._subtabs.sync_indicator()

    def _switch_subtab(self, index: int) -> None:
        self._stack.set_index(index, animated=self._animate_switch)

    def select_subtab(self, title: str, *, animated: bool = True) -> None:
        index = next(
            (i for i in range(self._subtabs.count()) if self._subtabs.tabText(i) == title), -1
        )
        if index < 0:
            return
        self._animate_switch = animated
        try:
            self._subtabs.setCurrentIndex(index)
        finally:
            self._animate_switch = True


class SettingsPage(QWidget):
    """占满主窗口的设置页。业务动作仍通过信号交给应用层。"""

    tab_created = Signal(str, object)
    configuration_changed = Signal()
    close_requested = Signal()
    diagnostics_requested = Signal()
    backup_requested = Signal()
    restore_requested = Signal()
    reset_requested = Signal()
    appearance_changed = Signal(object)
    voice_changed = Signal(object)
    presence_changed = Signal(object)
    reminder_notifications_changed = Signal(object)
    reminder_sound_preview_requested = Signal()
    model_discovery_requested = Signal(object)  # EndpointConfig
    # ModelProbeTask：实际模型页勾选 / 手动添加，或逻辑模型页「重新检测能力」
    model_probe_requested = Signal(object)
    manual_model_save_requested = Signal(object)  # ModelProbeTask
    model_capability_save_requested = Signal(object)  # ModelCapabilityChange

    def __init__(
        self,
        parent: QWidget | None,
        settings: Any,
        credentials: Any,
        *,
        preferences: Any = None,
        voice_service: Any = None,
        appearance_themes: Any = None,
        vault: Any = None,
        request_logs: Any = None,
        conversation_id: str | None = None,
        memories: Any = None,
        mcp_service: Any = None,
        diagnostics_available: bool = False,
        lan_panel: QWidget | Callable[[], QWidget] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._discovery_cache: dict[str, tuple[EndpointConfig, DiscoveryResult]] = {}
        self._settings = settings
        self._credentials = credentials
        self._voice_service = voice_service
        self._preferences = preferences
        self._appearance_themes = appearance_themes
        self._vault = vault
        self._memories = memories
        self._mcp_service = mcp_service
        self._mcp_tab: Any = None
        self._lan_panel = lan_panel
        self._pending_pages: dict[int, Callable[[], QWidget]] = {}
        self._stale_pages: set[int] = set()
        self._models_tab: LogicalModelsTab | None = None
        self._endpoints_tab: _EndpointsTab | None = None
        self._actual_models: ActualModelsPage | None = None
        self._endpoint_workspace: _EndpointWorkspace | None = None
        self._appearance_tab: _AppearanceTab | None = None
        self._reminder_notifications_tab: ReminderNotificationSettingsPage | None = None
        self._presence_tab: PresenceSettingsPage | None = None
        self._memory_tab: MemoryPanel | None = None
        self._about_tab: AboutPage | None = None
        self._probe_tasks: dict[tuple[str, str], ModelProbeTask] = {}
        self._probe_progress: dict[tuple[str, str], ModelProbeProgress] = {}
        self._request_logs = request_logs
        self._conversation_id = conversation_id
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("settingsHeader")
        header.setFixedHeight(68)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 10, 20, 10)
        title = QLabel("设置")
        title.setObjectName("settingsTitle")
        header_layout.addWidget(title)
        subtitle = QLabel("模型、站点、安全与应用偏好")
        subtitle.setObjectName("settingsSubtitle")
        header_layout.addWidget(subtitle)
        header_layout.addStretch(1)
        self._diagnostics_button = QPushButton("诊断日志")
        self._diagnostics_button.setObjectName("openDiagnostics")
        self._diagnostics_button.setProperty("flat", True)
        self._diagnostics_button.setToolTip("Ctrl+Shift+L（登录阶段也可使用）")
        self._diagnostics_button.setEnabled(diagnostics_available)
        self._diagnostics_button.clicked.connect(self.diagnostics_requested.emit)
        header_layout.addWidget(self._diagnostics_button)
        self._close_button = QPushButton("返回聊天")
        self._close_button.setProperty("flat", True)
        self._close_button.clicked.connect(self._request_close)
        header_layout.addWidget(self._close_button)
        root.addWidget(header)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self._rail = _VerticalTabRail()
        body_layout.addWidget(self._rail)
        content = QFrame()
        content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 22, 28, 24)
        self._pages = _AnimatedPageStack()
        content_layout.addWidget(self._pages, 1)
        body_layout.addWidget(content, 1)
        root.addWidget(body, 1)

        self._rail.add_group("模型与连接")
        self._add_lazy_page("逻辑模型", self._build_models)
        self._add_lazy_page("站点端点", self._build_endpoints)
        self._add_lazy_page("联网搜索", self._build_network)
        if mcp_service is not None:
            self._add_lazy_page("MCP 服务器", self._build_mcp)

        self._appearance_index = -1
        if preferences is not None or voice_service is not None:
            self._rail.add_group("使用体验")
        if preferences is not None:
            self._appearance_index = self._add_lazy_page("外观", self._build_appearance)
            self._add_lazy_page("提醒通知", self._build_reminders)
            self._add_lazy_page("低压在场", self._build_presence)
        if voice_service is not None:
            self._add_lazy_page("语音", self._build_voice)

        self._rail.add_group("数据与隐私")
        if memories is not None:
            self._add_lazy_page("记忆", self._build_memory)
        self._add_lazy_page("数据与安全", self._build_data_security)
        if lan_panel is not None:
            self._add_lazy_page("局域网访问", self._build_lan)
        self._request_log_index = self._add_page("请求日志", _RequestLogTab(None, None, self))

        self._rail.add_group("其他")
        self._add_lazy_page("关于", self._build_about)
        self._rail.tab_selected.connect(self._on_tab_selected)
        # Install inherited QSS before building controls, not after polishing a
        # full tree. Hidden categories stay as lightweight placeholders.
        self.restyle()
        self._ensure_page(0)

    def _add_lazy_page(self, title: str, factory: Callable[[], QWidget]) -> int:
        index = self._add_page(title, QWidget())
        self._pending_pages[index] = factory
        return index

    def _ensure_page(self, index: int) -> QWidget | None:
        factory = self._pending_pages.get(index)
        if factory is not None:
            page = factory()
            old = self._pages.replace_page(index, page)
            if old is not None:
                old.deleteLater()
            del self._pending_pages[index]
            self._stale_pages.discard(index)  # constructors read current data
            self.tab_created.emit(self._rail._buttons[index].text(), page)
        return self._pages._stack.widget(index) if index >= 0 else None

    def _build_models(self) -> QWidget:
        from limbowave.ui.logical_models_tab import LogicalModelsTab

        self._models_tab = LogicalModelsTab(self._settings, self._pages._stack)
        self._models_tab.probe_requested.connect(self._request_model_probe)
        return self._models_tab

    def _build_endpoints(self) -> QWidget:
        from limbowave.ui.model_probe_page import ActualModelsPage
        from limbowave.ui.settings_dialog import _EndpointsTab

        endpoints = _EndpointsTab(self._settings, self._credentials)
        actual = ActualModelsPage()
        workspace = _EndpointWorkspace(endpoints, actual, self._pages._stack)
        self._endpoints_tab = endpoints
        self._actual_models = actual
        self._endpoint_workspace = workspace
        endpoints.configuration_changed.connect(self._configuration_changed)
        endpoints.discovery_requested.connect(self.model_discovery_requested.emit)
        endpoints.endpoint_saved.connect(self._on_endpoint_saved)
        endpoints.endpoint_selecting.connect(workspace._stack.capture_refresh)
        endpoints.endpoint_selected.connect(self._on_endpoint_selected)
        actual.discovery_requested.connect(self.model_discovery_requested.emit)
        actual.probe_requested.connect(self._request_model_probe)
        actual.manual_save_requested.connect(self.manual_model_save_requested.emit)
        actual.capability_save_requested.connect(self.model_capability_save_requested.emit)
        return workspace

    def _configuration_changed(self) -> None:
        if self._models_tab is not None:
            self._models_tab.reload()
        self.configuration_changed.emit()

    def _build_appearance(self) -> QWidget:
        from limbowave.ui.settings_dialog import _AppearanceTab

        self._appearance_tab = _AppearanceTab(
            self._preferences, self._pages._stack, theme_service=self._appearance_themes,
        )
        self._appearance_tab.appearance_changed.connect(self.appearance_changed.emit)
        return self._appearance_tab

    def _scroll_page(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea(self._pages._stack)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _build_reminders(self) -> QWidget:
        from limbowave.ui.reminder_notifications import ReminderNotificationSettingsPage

        self._reminder_notifications_tab = ReminderNotificationSettingsPage(self._preferences)
        self._reminder_notifications_tab.changed.connect(self.reminder_notifications_changed.emit)
        self._reminder_notifications_tab.preview_requested.connect(
            self.reminder_sound_preview_requested.emit,
        )
        return self._scroll_page(self._reminder_notifications_tab)

    def _build_mcp(self) -> QWidget:
        from limbowave.ui.mcp_settings import McpSettingsPage

        self._mcp_tab = McpSettingsPage(self._mcp_service)
        return self._scroll_page(self._mcp_tab)

    def _build_network(self) -> QWidget:
        from limbowave.application.services.network_settings_service import NetworkSettingsService
        from limbowave.ui.network_settings_page import NetworkSettingsPage

        return self._scroll_page(NetworkSettingsPage(
            NetworkSettingsService(self._settings, self._credentials),
        ))

    def _build_voice(self) -> QWidget:
        from limbowave.ui.voice_settings import VoiceSettingsPage

        page = VoiceSettingsPage(self._voice_service)
        page.changed.connect(self.voice_changed.emit)
        return self._scroll_page(page)

    def _build_presence(self) -> QWidget:
        from limbowave.ui.gentle_presence import PresenceSettingsPage

        self._presence_tab = PresenceSettingsPage(self._preferences)
        self._presence_tab.changed.connect(self.presence_changed.emit)
        return self._scroll_page(self._presence_tab)

    def _build_memory(self) -> QWidget:
        from limbowave.ui.session_memory_panel import MemoryPanel

        self._memory_tab = MemoryPanel(self._memories, self._pages._stack)
        return self._memory_tab

    def _build_data_security(self) -> QWidget:
        page = _DataSecurityTab(self._vault, self._pages._stack)
        page.backup_requested.connect(self.backup_requested.emit)
        page.restore_requested.connect(self.restore_requested.emit)
        page.reset_requested.connect(self.reset_requested.emit)
        return page

    def _build_lan(self) -> QWidget:
        panel = self._lan_panel() if callable(self._lan_panel) else self._lan_panel
        assert panel is not None
        return self._scroll_page(panel)

    def _build_about(self) -> QWidget:
        from limbowave.ui.about_page import AboutPage

        self._about_tab = AboutPage(self._pages._stack)
        return self._about_tab

    # Keep the public accessors used by model callbacks and integrations. Access
    # creates a category, but does not navigate to it or replace an existing tab.
    @property
    def models_tab(self) -> LogicalModelsTab:
        self._ensure_page(self._rail.index_of("逻辑模型"))
        assert self._models_tab is not None
        return self._models_tab

    @property
    def endpoints_tab(self) -> _EndpointsTab:
        self._ensure_page(self._rail.index_of("站点端点"))
        assert self._endpoints_tab is not None
        return self._endpoints_tab

    @property
    def actual_models(self) -> ActualModelsPage:
        self._ensure_page(self._rail.index_of("站点端点"))
        assert self._actual_models is not None
        return self._actual_models

    @property
    def endpoint_workspace(self) -> _EndpointWorkspace:
        return cast(_EndpointWorkspace, self._ensure_page(self._rail.index_of("站点端点")))

    @property
    def appearance_tab(self) -> _AppearanceTab | None:
        self._ensure_page(self._appearance_index)
        return self._appearance_tab

    @property
    def reminder_notifications_tab(self) -> ReminderNotificationSettingsPage | None:
        self._ensure_page(self._rail.index_of("提醒通知"))
        return self._reminder_notifications_tab

    @property
    def presence_tab(self) -> PresenceSettingsPage | None:
        self._ensure_page(self._rail.index_of("低压在场"))
        return self._presence_tab

    @property
    def memory_tab(self) -> MemoryPanel | None:
        self._ensure_page(self._rail.index_of("记忆"))
        return self._memory_tab

    @property
    def about_tab(self) -> AboutPage:
        self._ensure_page(self._rail.index_of("关于"))
        assert self._about_tab is not None
        return self._about_tab

    def _on_tab_selected(self, index: int) -> None:
        current = self._pages.current_index
        if (
            current == self._appearance_index
            and index != current
            and self._appearance_tab is not None
            and not self._appearance_tab.resolve_pending_changes()
        ):
            self._rail.set_current(current, animated=False)
            return
        self._ensure_page(index)
        self._refresh_page(index)
        if index == self._request_log_index:
            self._rebuild_request_log()
        self._pages.set_index(index)

    def prepare_open(self, conversation_id: str | None, initial_tab: str | None = None) -> None:
        """Refresh the destination only; hidden categories catch up on selection."""
        self._conversation_id = conversation_id
        self._stale_pages.update(
            self._rail.index_of(title) for title in ("逻辑模型", "站点端点", "记忆")
            if self._rail.index_of(title) >= 0
        )
        if initial_tab:
            self.select_tab(initial_tab)
        else:
            index = self._pages.current_index
            self._refresh_page(index)
            if index == self._request_log_index:
                self._rebuild_request_log()

    def _refresh_page(self, index: int) -> None:
        if index not in self._stale_pages:
            return
        self._stale_pages.discard(index)
        if index == self._rail.index_of("逻辑模型"):
            self.models_tab.reload()
        elif index == self._rail.index_of("站点端点"):
            self.endpoints_tab.reload()
            self.actual_models.apply_saved_models(tuple(self._settings.load().actual_models))
        elif index == self._rail.index_of("记忆") and self._memory_tab is not None:
            self._memory_tab.reload()

    def _rebuild_request_log(self) -> None:
        from limbowave.ui.request_log_dialog import RequestLogDialog

        current = self._pages._stack.widget(self._request_log_index)
        viewer = current.findChild(RequestLogDialog) if current is not None else None
        if viewer is not None and self._conversation_id is not None:
            viewer.set_conversation(self._conversation_id)
            viewer.show()
            return
        replacement = _RequestLogTab(self._request_logs, self._conversation_id, self)
        old = self._pages.replace_page(self._request_log_index, replacement)
        if old is not None:
            old.deleteLater()

    def resolve_pending_changes(self) -> bool:
        if self._mcp_tab is not None and not self._mcp_tab.resolve_pending_changes():
            self.select_tab("MCP 服务器")
            return False
        return self._appearance_tab is None or self._appearance_tab.resolve_pending_changes()

    def _request_close(self) -> None:
        if not self.resolve_pending_changes():
            return
        self.close_requested.emit()

    def _on_endpoint_selected(self, endpoint: EndpointConfig | None) -> None:
        if endpoint is None:
            self.actual_models.activate_endpoint(None, discover=False)
            self.endpoint_workspace.select_subtab("端点配置", animated=False)
            return
        if self.actual_models._endpoint == endpoint:
            return
        cached = self._discovery_cache.get(endpoint.id)
        self.actual_models.activate_endpoint(
            endpoint,
            discover=True,
            actual_models=tuple(self._settings.load().actual_models),
        )
        if cached is not None and cached[0] == endpoint:
            self.actual_models.apply_discovery(endpoint.id, cached[1])
        self._restore_active_probes()
        self.endpoint_workspace._stack.animate_refresh(
            self.endpoint_workspace._subtabs.currentIndex()
        )

    def _on_endpoint_saved(self, endpoint: EndpointConfig) -> None:
        """保存站点后切到端点页的实际模型子选项卡，并复用已拉取清单。"""
        self._on_endpoint_selected(endpoint)
        self.select_tab("站点端点", animated=True)
        self.endpoint_workspace.select_subtab("实际模型", animated=True)

    def apply_model_discovery(self, endpoint: EndpointConfig, result: DiscoveryResult) -> None:
        self._discovery_cache[endpoint.id] = (endpoint, result)
        if self.actual_models._endpoint == endpoint:
            self.actual_models.apply_discovery(endpoint.id, result)

    def _request_model_probe(self, task: ModelProbeTask) -> None:
        key = (task.endpoint.id, task.model_id)
        if key in self._probe_tasks:
            return
        self._probe_tasks[key] = task
        if self.actual_models.endpoint_id == task.endpoint.id:
            self.actual_models.mark_probe_started(task.model_id)
        self.model_probe_requested.emit(task)

    def _restore_active_probes(self) -> None:
        # 切走再回来，以及从逻辑模型页发起的检测，也要锁住同一模型的能力编辑。
        for task in self._probe_tasks.values():
            if self.actual_models.endpoint_id == task.endpoint.id:
                self.actual_models.mark_probe_started(task.model_id)
                progress = self._probe_progress.get((task.endpoint.id, task.model_id))
                if progress is not None:
                    self.actual_models.apply_probe_progress(
                        task.endpoint.id,
                        task.model_id,
                        progress,
                    )

    def apply_model_probe_progress(
        self,
        endpoint_id: str,
        model_id: str,
        progress: ModelProbeProgress,
    ) -> None:
        key = (endpoint_id, model_id)
        if key not in self._probe_tasks:
            return
        self._probe_progress[key] = progress
        self.actual_models.apply_probe_progress(endpoint_id, model_id, progress)

    def apply_model_probe(self, endpoint_id: str, result: ModelProbeResult) -> None:
        self._probe_tasks.pop((endpoint_id, result.model_id), None)
        self._probe_progress.pop((endpoint_id, result.model_id), None)
        self.models_tab.apply_probe_result(endpoint_id, result)
        actual = self._settings.load().actual_model(endpoint_id, result.model_id)
        self.actual_models.apply_probe_result(endpoint_id, result, actual)

    def apply_manual_model_save(self, endpoint_id: str, model_id: str) -> None:
        self.models_tab.reload()
        self.actual_models.apply_saved_models(tuple(self._settings.load().actual_models))
        self.actual_models.apply_manual_save(endpoint_id, model_id)

    def apply_model_capability_save(
        self,
        change: ModelCapabilityChange,
        actual: ActualModel,
    ) -> None:
        self.models_tab.reload()
        self.actual_models.apply_capability_save(change, actual)

    def show_model_probe_error(self, endpoint_id: str, model_id: str, detail: str) -> None:
        self._probe_tasks.pop((endpoint_id, model_id), None)
        self._probe_progress.pop((endpoint_id, model_id), None)
        self.models_tab.show_probe_error(endpoint_id, model_id, detail)
        self.actual_models.show_probe_error(endpoint_id, model_id, detail)

    def _add_page(self, title: str, page: QWidget) -> int:
        self._rail.add_tab(title)
        return self._pages.add_page(page)

    def select_tab(self, title: str, *, animated: bool = False) -> None:
        if title in ("数据", "安全"):
            title = "数据与安全"
        if title == "实际模型":
            self.select_tab("站点端点", animated=animated)
            self.endpoint_workspace.select_subtab("实际模型", animated=animated)
            return
        index = self._rail.index_of(title)
        if index < 0:
            return
        self._ensure_page(index)
        self._refresh_page(index)
        self._rail.set_current(index, animated=animated)
        if index == self._request_log_index:
            self._rebuild_request_log()
        self._pages.set_index(index, animated=animated)

    def restyle(self) -> None:
        """按当前主题令牌重建本页样式。

        指示器浓度随材质设置（不透明度滑杆）变化，``theme.refresh_inline_styles``
        只换调色板令牌追不上，所以主题/材质变化后由上层调用这里整体重建。
        """
        if self._about_tab is not None:
            self._about_tab.restyle()
        css = f"""
            #settingsPage {{ background: {theme.BG_APP}; }}
            #settingsHeader {{ background: {theme.BG_SURFACE};
                border-bottom: 1px solid {theme.BORDER}; }}
            #settingsTitle {{ background: transparent; color: {theme.TEXT_PRIMARY};
                font-size: {theme.FS_TITLE}px; font-weight: 600; border: none; }}
            #settingsSubtitle {{ background: transparent; color: {theme.TEXT_SECONDARY};
                font-size: {theme.FS_SMALL}px; border: none; padding-left: 8px; }}
            #settingsRail {{ background: {theme.BG_SURFACE};
                border-right: 1px solid {theme.BORDER}; }}
            #settingsContent {{ background: {theme.BG_APP}; border: none; }}
            #settingsIndicator {{ background: {theme.tab_indicator_surface()}; border: none;
                border-radius: 2px; }}
            QLabel#settingsGroupLabel {{ background: transparent; border: none;
                color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; font-weight: 600;
                padding: 6px 10px 0 10px; }}
            QPushButton[settingsTab="true"] {{ background: transparent; border: none;
                border-radius: {theme.RADIUS_MD}px; color: {theme.TEXT_SECONDARY};
                text-align: left; padding: 0 14px 0 24px; font-weight: 500; }}
            QPushButton[settingsTab="true"]:hover {{ background: {theme.tab_hover_surface()};
                color: {theme.TEXT_PRIMARY}; }}
            QPushButton[settingsTab="true"]:checked,
            QPushButton[settingsTab="true"]:checked:hover {{
                background: {theme.tab_selected_surface()}; color: {theme.TEXT_PRIMARY}; }}
            QPushButton[settingsTab="true"]:focus {{ border: none; }}
            """
        # setStyleSheet 即使内容相同也会重新 polish 整棵子树，外观预览每一步都会走到这里。
        if css != self.styleSheet():
            self.setStyleSheet(css)


# 兼容旧导入名：实现已经从悬浮窗升级为全窗口页面。
SettingsPanel = SettingsPage
