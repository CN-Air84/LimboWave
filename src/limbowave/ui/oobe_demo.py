"""首次使用引导。

``python -m limbowave.ui.oobe_demo`` 打开文案对照窗，检查是假的，左边一栏正式版没有。
正式启动在还没有可路由模型时嵌入同一套界面，检查和写入由 ``oobe_live`` 负责。

文案：``limbowave.ui.oobe_copy``
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.model_probe import DiscoveredModel, DiscoveryResult
from limbowave.domain.models import ActualModel
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.ui import theme
from limbowave.ui.animated_stack import AnimatedPageStack
from limbowave.ui.model_probe_page import ActualModelsPage, ModelCapabilityChange, ModelProbeTask
from limbowave.ui.oobe_copy import (
    ADDRESS_LABEL,
    BAD_URL,
    CONSOLE_HEADING,
    DISPLAY_NAME_LABEL,
    DISPLAY_NAME_PLACEHOLDER,
    KEY_LABEL,
    KEY_PLACEHOLDER,
    MORE_PROVIDERS,
    NEED_KEY,
    NEED_PROVIDER,
    NEED_URL,
    PROVIDERS,
    RAIL_FOOT,
    RAIL_GROUPS,
    RAIL_NOTE,
    RAIL_TITLE,
    REVEAL_HIDE,
    REVEAL_SHOW,
    SAMPLE_PROVIDER_ID,
    URL_PLACEHOLDER,
    WELCOME_NOTE,
    WELCOME_STEPS,
    WINDOW_TITLE,
    ProviderOption,
    ScreenCopy,
    mask_middle,
    present,
    provider_by_id,
)

CHECK_PREVIEW_MS = 800
_COLUMN_WIDTH = 480
_PRESET_WIDTH = 340
_PROVIDER_WIDTH = 720
_KEY_PAGE = 2
_EMPTY_PAGE = 3
# Logical navigation order, independent of the reused form/empty stack pages.
_SCREEN_STEP = {
    "intro": 0,
    "welcome": 1,
    "browse": 2,
    "provider": 3,
    "later": 3,
    "key": 4,
    "key_custom": 4,
    "checking": 5,
    "models": 5,
    "success": 6,
    "error_key": 6,
    "error_network": 6,
    "error_timeout": 6,
    "done": 7,
}


def valid_http_url(value: str) -> bool:
    text = value.strip()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            return bool(text[len(prefix) :].strip("/"))
    return False


class _ProviderRow(QFrame):
    """一户服务商。选中后左侧露出强调色。"""

    def __init__(self, option: ProviderOption, on_pick: Callable[[str], None]) -> None:
        super().__init__()
        self.option = option
        self._on_pick = on_pick
        self.setObjectName("oobeProviderRow")
        self.setProperty("selected", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName(f"{option.name}，{option.detail}")
        self.setFixedHeight(58)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 14, 8)
        layout.setSpacing(1)
        name = QLabel(option.name)
        name.setObjectName("oobeProviderName")
        detail = QLabel(option.detail)
        detail.setObjectName("oobeProviderDetail")
        for label in (name, detail):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(name)
        layout.addWidget(detail)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_pick(self.option.id)
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._on_pick(self.option.id)
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self.property("selected") != "true":
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACCENT))
        painter.drawRoundedRect(0, 12, 3, self.height() - 24, 1, 1)


class _Stack(QStackedWidget):
    """高度只跟当前页走，避免欢迎页被服务商列表撑高。"""

    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        if current is None:
            return super().sizeHint()
        hint = current.sizeHint()
        return QSize(max(hint.width(), _COLUMN_WIDTH), hint.height())

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        if current is None:
            return super().minimumSizeHint()
        hint = current.minimumSizeHint()
        return QSize(hint.width(), hint.height())

    def hasHeightForWidth(self) -> bool:
        current = self.currentWidget()
        return bool(current is not None and current.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:
        current = self.currentWidget()
        if current is not None and current.hasHeightForWidth():
            return current.heightForWidth(width)
        return self.sizeHint().height()


class _OobePages(AnimatedPageStack):
    """Preserve the content's sizing through the animation's layout-free host."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("oobePages")
        self._stack.setObjectName("oobePagesStack")

    def animate_refresh(self, index: int) -> None:
        super().animate_refresh(index)
        if self._outgoing is not None:
            self._outgoing.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self) -> QSize:
        return self._stack.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self._stack.minimumSizeHint()

    def hasHeightForWidth(self) -> bool:
        return self._stack.hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        return self._stack.heightForWidth(width)


class _Stage(QWidget):
    """产品表面：淡光晕，和登录页同一套底。"""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("oobeStage")
        self.setAutoFillBackground(False)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.BG_APP))
        accent = QColor(theme.ACCENT)
        accent.setAlpha(26 if theme.current_palette().name == "dark" else 18)
        radius = max(self.width(), self.height()) * 0.72
        glow = QRadialGradient(self.width() * 0.82, self.height() * 0.0, radius)
        glow.setColorAt(0, accent)
        glow.setColorAt(1, QColor(theme.BG_APP))
        painter.fillRect(self.rect(), glow)


class OobeDemo(QWidget):
    """首次使用的几屏。``preview`` 时检查会假装成功，并带文案对照栏。"""

    check_requested = Signal(int, str, str)
    model_selected = Signal(str)
    screen_changed = Signal(str)
    finished = Signal()
    skipped = Signal()

    def __init__(self, *, preview: bool = True) -> None:
        super().__init__()
        self.setObjectName("oobeDemo")
        self._preview = preview
        self._screen_id = "intro"
        self._selected: str | None = None
        self._display_names: dict[str, str] = {}
        self._rows: dict[str, _ProviderRow] = {}
        self._rail_buttons: dict[str, QPushButton] = {}
        self._check_generation = 0
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(CHECK_PREVIEW_MS)
        self._check_timer.timeout.connect(self._finish_check)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        if preview:
            self.setWindowTitle(WINDOW_TITLE)
            self.setMinimumSize(960, 640)
            root.addWidget(self._build_rail())
        root.addWidget(self._build_stage(), 1)
        self.setStyleSheet(_stylesheet())
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.show_screen("intro")

    def provider(self) -> ProviderOption:
        return self._provider()

    def display_name(self) -> str:
        return self._display_name.text().strip()

    def _detach(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    @property
    def screen_id(self) -> str:
        return self._screen_id

    def show_screen(self, screen_id: str, *, auto_continue: bool = False) -> None:
        """切到某一屏。``auto_continue`` 只在用户真的点了检查时为真，对照文案时不要自动往下走。"""
        changed = screen_id != self._screen_id
        if changed:
            direction = -1 if _SCREEN_STEP[screen_id] < _SCREEN_STEP[self._screen_id] else 1
            # Titles, status copy and actions change outside the inner form stack.
            # Capture the entire column before any of those widgets are updated.
            self._pages.capture_refresh(direction)
        self._check_timer.stop()
        if screen_id != "checking":
            self._check_generation += 1
        self._prepare_provider(screen_id)
        if screen_id == "models" and self._preview:
            self._prepare_preview_models()
        self._screen_id = screen_id
        self._mark_rail(screen_id)
        copy = self._copy()
        self._apply_copy(copy)
        self._show_page(screen_id)
        self._sync_key_form(screen_id)
        self._refresh_actions()
        self._pages.updateGeometry()
        if changed:
            self._content_layout.activate()
            self._column_layout.activate()
            self._pages.animate_refresh(0)
        self.screen_changed.emit(screen_id)
        if auto_continue and screen_id == "checking":
            self._check_timer.start()

    def _build_rail(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("oobeRail")
        rail.setFixedWidth(220)
        rail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(14, 22, 14, 16)
        layout.setSpacing(2)

        title = QLabel(RAIL_TITLE)
        title.setObjectName("oobeRailTitle")
        note = QLabel(RAIL_NOTE)
        note.setObjectName("oobeRailNote")
        note.setWordWrap(True)
        note.setMinimumWidth(0)
        layout.addWidget(title)
        layout.addSpacing(6)
        layout.addWidget(note)
        layout.addSpacing(14)

        self._rail_group = QButtonGroup(self)
        self._rail_group.setExclusive(True)
        index = 0
        for group_index, (heading, screen_ids) in enumerate(RAIL_GROUPS):
            label = QLabel(heading)
            label.setObjectName("oobeRailGroup")
            label.setContentsMargins(8, 0 if group_index == 0 else 12, 8, 4)
            layout.addWidget(label)
            for screen_id in screen_ids:
                button = QPushButton(present(screen_id, provider_by_id(SAMPLE_PROVIDER_ID)).rail)
                button.setObjectName("oobeRailButton")
                button.setProperty("screenId", screen_id)
                button.setCheckable(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                self._rail_group.addButton(button, index)
                self._rail_buttons[screen_id] = button
                layout.addWidget(button)
                index += 1
        self._rail_group.idClicked.connect(self._on_rail_index)

        layout.addStretch(1)
        foot = QLabel(RAIL_FOOT)
        foot.setObjectName("oobeRailNote")
        foot.setWordWrap(True)
        foot.setMinimumWidth(0)
        layout.addWidget(foot)
        return rail

    def _build_stage(self) -> QWidget:
        stage = _Stage()
        outer = QHBoxLayout(stage)
        outer.setContentsMargins(64, 52, 48, 32)
        outer.setSpacing(0)

        column = QWidget()
        self._column = column
        column.setObjectName("oobeColumn")
        column.setMinimumWidth(_COLUMN_WIDTH)
        column.setMaximumWidth(_COLUMN_WIDTH)
        column.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._column_layout = QVBoxLayout(column)
        self._column_layout.setContentsMargins(0, 0, 0, 0)
        self._column_layout.setSpacing(0)

        self._mark = QFrame()
        self._mark.setObjectName("oobeMark")
        self._mark.setFixedSize(36, 3)
        self._mark.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._column_layout.addWidget(self._mark, alignment=Qt.AlignmentFlag.AlignLeft)
        self._column_layout.addSpacing(18)

        self._eyebrow = _wrapping_label("oobeEyebrow")
        self._title = _wrapping_label("oobeTitle")
        self._body = _wrapping_label("oobeBody")
        self._column_layout.addWidget(self._eyebrow)
        self._column_layout.addSpacing(8)
        self._column_layout.addWidget(self._title)
        self._column_layout.addSpacing(12)
        self._column_layout.addWidget(self._body)
        self._secret = _wrapping_label("oobeSecret")
        self._secret.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._secret.hide()
        self._column_layout.addSpacing(10)
        self._column_layout.addWidget(self._secret)
        self._column_layout.addSpacing(18)

        self._stack = _Stack()
        self._stack.setObjectName("oobeStack")
        self._stack.addWidget(self._build_welcome())
        self._stack.addWidget(self._build_providers())
        self._stack.addWidget(self._build_key_form())
        self._stack.addWidget(QWidget())
        self._stack.addWidget(self._build_intro())
        self._stack.addWidget(self._build_models())
        self._stack_index = self._column_layout.count()
        self._column_layout.addWidget(self._stack)

        self._hint_wrap = QWidget()
        self._hint_wrap.setObjectName("oobeColumn")
        hint_layout = QVBoxLayout(self._hint_wrap)
        hint_layout.setContentsMargins(0, 14, 0, 0)
        hint_layout.setSpacing(0)
        self._hint = _wrapping_label("oobeHint")
        hint_layout.addWidget(self._hint)
        self._column_layout.addWidget(self._hint_wrap)
        self._column_layout.addSpacing(16)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self._secondary = QPushButton()
        self._secondary.setObjectName("oobeSecondary")
        self._secondary.setProperty("flat", True)
        self._secondary.setCursor(Qt.CursorShape.PointingHandCursor)
        self._secondary.clicked.connect(self._on_secondary)
        self._primary = QPushButton()
        self._primary.setObjectName("oobePrimary")
        self._primary.setProperty("accent", True)
        self._primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self._primary.clicked.connect(self._on_primary)
        footer.addStretch(1)
        footer.addWidget(self._secondary)
        footer.addWidget(self._primary)
        self._column_layout.addLayout(footer)
        self._tail_index = self._column_layout.count()
        self._column_layout.addStretch(1)

        # A single animated page holds all screen content. The rail and stage
        # background stay still; snapshots also cover status-only screen changes.
        content = QWidget()
        content.setObjectName("oobeColumn")
        self._content_layout = QHBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        self._content_layout.addWidget(column)
        self._content_layout.addStretch(1)
        self._pages = _OobePages()
        self._pages.add_page(content)
        outer.addWidget(self._pages, 1)
        return stage

    def _build_intro(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        self._intro_layout = QVBoxLayout(page)
        self._intro_layout.setContentsMargins(0, 0, 0, 0)
        self._intro_layout.setSpacing(18)
        return page

    def _fill_intro(self, body: str) -> None:
        while self._intro_layout.count():
            item = self._intro_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for paragraph in body.split("\n\n"):
            text = paragraph.strip()
            if not text:
                continue
            label = _wrapping_label("oobeIntroParagraph")
            label.setText(text)
            self._intro_layout.addWidget(label)

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for index, step in enumerate(WELCOME_STEPS, start=1):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 14)
            row.setSpacing(14)
            number = QLabel(str(index))
            number.setObjectName("oobeStepIndex")
            number.setFixedWidth(18)
            label = QLabel(step)
            label.setObjectName("oobeStepLabel")
            row.addWidget(number, alignment=Qt.AlignmentFlag.AlignTop)
            row.addWidget(label, 1)
            layout.addLayout(row)
        note = _wrapping_label("oobeWelcomeNote")
        note.setText(WELCOME_NOTE)
        layout.addSpacing(8)
        layout.addWidget(note)
        return page

    def _build_providers(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        left = QWidget()
        left.setObjectName("oobeColumn")
        left.setFixedWidth(_PRESET_WIDTH)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(self._provider_scroll(), 1)
        left_layout.addWidget(self._build_more_combo())
        layout.addWidget(left)

        side = QWidget()
        side.setObjectName("oobeColumn")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(8)
        custom = provider_by_id("custom")
        custom_row = _ProviderRow(custom, self._select_provider)
        self._rows[custom.id] = custom_row
        side_layout.addWidget(custom_row)
        side_layout.addStretch(1)
        layout.addWidget(side, 1)
        return page

    def _provider_scroll(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("oobeScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        host = QWidget()
        host.setObjectName("oobeColumn")
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 4, 0)
        host_layout.setSpacing(8)
        for option in PROVIDERS:
            if option.custom or option.more:
                continue
            row = _ProviderRow(option, self._select_provider)
            self._rows[option.id] = row
            host_layout.addWidget(row)
        host_layout.addStretch(1)
        scroll.setWidget(host)
        scroll.viewport().setAutoFillBackground(False)
        return scroll

    def _build_more_combo(self) -> QComboBox:
        combo = QComboBox()
        self._more = combo
        combo.setObjectName("oobeMoreCombo")
        combo.setAccessibleName(MORE_PROVIDERS)
        combo.setPlaceholderText(MORE_PROVIDERS)
        combo.setFixedHeight(58)
        combo.setCursor(Qt.CursorShape.PointingHandCursor)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(8)
        for option in PROVIDERS:
            if option.more:
                combo.addItem(option.name, option.id)
        combo.setCurrentIndex(-1)
        combo.currentIndexChanged.connect(self._on_more_provider)
        return combo

    def _on_more_provider(self, index: int) -> None:
        if index < 0:
            return
        provider_id = self._more.itemData(index)
        if isinstance(provider_id, str) and provider_id != self._selected:
            self._select_provider(provider_id)

    def _sync_more_combo(self, provider_id: str) -> None:
        selected = provider_by_id(provider_id).more
        self._more.blockSignals(True)
        self._more.setCurrentIndex(self._more.findData(provider_id) if selected else -1)
        self._more.blockSignals(False)
        self._more.setProperty("selected", "true" if selected else "false")
        self._more.style().unpolish(self._more)
        self._more.style().polish(self._more)

    def _build_key_form(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._display_name = QLineEdit()
        self._display_name.setObjectName("oobeDisplayName")
        self._display_name.setAccessibleName(DISPLAY_NAME_LABEL)
        self._display_name.returnPressed.connect(self._on_primary)
        _add_field(layout, DISPLAY_NAME_LABEL, self._display_name)
        layout.addSpacing(20)

        self._address = QLabel()
        self._address.setObjectName("oobeAddressValue")
        self._address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._address.setWordWrap(True)
        self._url = QLineEdit()
        self._url.setObjectName("oobeUrl")
        self._url.setPlaceholderText(URL_PLACEHOLDER)
        self._url.setAccessibleName(ADDRESS_LABEL)
        self._url.textChanged.connect(self._refresh_actions)
        self._url.returnPressed.connect(self._on_primary)
        address_slot = QWidget()
        address_slot.setObjectName("oobeColumn")
        address_layout = QVBoxLayout(address_slot)
        address_layout.setContentsMargins(0, 0, 0, 0)
        address_layout.setSpacing(0)
        address_layout.addWidget(self._address)
        address_layout.addWidget(self._url)
        _add_field(layout, ADDRESS_LABEL, address_slot)

        self._key = QLineEdit()
        self._key.setObjectName("oobeKey")
        self._key.setPlaceholderText(KEY_PLACEHOLDER)
        self._key.setAccessibleName(KEY_LABEL)
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.textChanged.connect(self._refresh_actions)
        self._key.returnPressed.connect(self._on_primary)
        self._reveal = QPushButton(REVEAL_SHOW)
        self._reveal.setObjectName("oobeReveal")
        self._reveal.setProperty("flat", True)
        self._reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reveal.setFixedWidth(72)
        self._reveal.clicked.connect(self._toggle_reveal)
        key_row = QWidget()
        key_row.setObjectName("oobeColumn")
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(8)
        key_layout.addWidget(self._key, 1)
        key_layout.addWidget(self._reveal)
        layout.addSpacing(20)
        _add_field(layout, KEY_LABEL, key_row)

        self._console = _wrapping_label("oobeConsole")
        layout.addSpacing(22)
        _add_field(layout, CONSOLE_HEADING, self._console)
        layout.addStretch(1)
        return page

    def _build_models(self) -> QWidget:
        page = QWidget()
        page.setObjectName("oobeColumn")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.actual_models = ActualModelsPage(page)
        layout.addWidget(self.actual_models, 1)
        self._default_model = QComboBox()
        self._default_model.setAccessibleName("默认聊天模型")
        self._default_model.setPlaceholderText("添加模型后，在这里选择默认聊天模型")
        self._default_model.currentIndexChanged.connect(self._refresh_actions)
        _add_field(layout, "默认聊天模型", self._default_model)
        if self._preview:
            self.actual_models.discovery_requested.connect(lambda _e: self._preview_discovery())
            self.actual_models.manual_save_requested.connect(self._preview_save)
            self.actual_models.probe_requested.connect(self._preview_save)
            self.actual_models.capability_save_requested.connect(self._preview_capability)
        return page

    def update_default_models(self, models: tuple[ActualModel, ...]) -> None:
        selected = self._default_model.currentData()
        self._default_model.blockSignals(True)
        self._default_model.clear()
        for model in models:
            if model.endpoint_id == self.actual_models.endpoint_id:
                self._default_model.addItem(
                    f"{model.display_name} · {model.model_id}", model.model_id
                )
        self._default_model.setCurrentIndex(self._default_model.findData(selected))
        self._default_model.blockSignals(False)
        self._refresh_actions()

    def _preview_discovery(self) -> None:
        endpoint_id = self.actual_models.endpoint_id
        if endpoint_id is None:
            return
        self.actual_models.apply_discovery(
            endpoint_id,
            DiscoveryResult(
                (
                    DiscoveredModel("demo-chat", "演示聊天模型"),
                    DiscoveredModel("demo-reasoner", "演示思考模型"),
                ),
                "演示模型清单（未连接服务商）",
            ),
        )

    def _preview_save(self, task: ModelProbeTask) -> None:
        self.actual_models._probing.discard(task.model_id)
        actual = ActualModel(
            endpoint_id=task.endpoint.id, model_id=task.model_id, name=task.display_name
        )
        self.actual_models.apply_saved_models((actual,))
        self.actual_models.apply_manual_save(task.endpoint.id, task.model_id)
        self.actual_models._status.setText("演示：仅添加到当前预览，未检测、未保存")
        self.update_default_models(
            tuple(r.actual for r in self.actual_models._rows.values() if r.actual)
        )

    def _preview_capability(self, change: ModelCapabilityChange) -> None:
        row = self.actual_models._rows[change.model_id]
        actual = row.actual or ActualModel(
            endpoint_id=change.endpoint.id,
            model_id=change.model_id,
            name=change.display_name,
        )
        actual = actual.model_copy(update={change.capability: change.supported})
        self.actual_models.apply_capability_save(change, actual)
        self.update_default_models(
            tuple(r.actual for r in self.actual_models._rows.values() if r.actual)
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
            and isinstance(watched, QWidget)
            and (watched is self or self.isAncestorOf(watched))
        ):
            self._on_escape()
            return True
        return super().eventFilter(watched, event)

    def _prepare_provider(self, screen_id: str) -> None:
        if screen_id == "key_custom":
            self._select_provider("custom")
            return
        needs_named = screen_id in {
            "key",
            "checking",
            "models",
            "success",
            "done",
            "error_key",
            "error_network",
            "error_timeout",
        }
        named_missing = needs_named and self._selected is None
        key_needs_sample = screen_id == "key" and self._selected in (None, "custom")
        if named_missing or key_needs_sample:
            self._select_provider(SAMPLE_PROVIDER_ID)

    def _select_provider(self, provider_id: str) -> None:
        if provider_id != self._selected:
            if self._selected is not None:
                self._display_names[self._selected] = self._display_name.text()
            self._selected = provider_id
            provider = self._provider()
            default_name = "" if provider.custom else provider.name
            self._display_name.setText(self._display_names.get(provider_id, default_name))
            self._display_name.setPlaceholderText(
                DISPLAY_NAME_PLACEHOLDER if provider.custom else provider.name
            )
        for row_id, row in self._rows.items():
            row.set_selected(row_id == provider_id)
        self._sync_more_combo(provider_id)
        if self._screen_id == "provider":
            self._refresh_actions()

    def _provider(self) -> ProviderOption:
        return provider_by_id(self._selected or SAMPLE_PROVIDER_ID)

    def _current_url(self) -> str:
        provider = self._provider()
        if provider.custom:
            return self._url.text().strip().rstrip("/")
        return provider.base_url

    def _copy(self) -> ScreenCopy:
        return present(
            self._screen_id,
            self._provider(),
            url=self._current_url(),
            display_name=self.display_name(),
        )

    def _apply_copy(self, copy: ScreenCopy) -> None:
        self._eyebrow.setText(copy.eyebrow)
        self._eyebrow.setVisible(bool(copy.eyebrow.strip()))
        self._title.setText(copy.title)
        self._body.setText("" if self._screen_id == "intro" else copy.body)
        self._body.setVisible(self._screen_id != "intro")
        masked = mask_middle(self._key.text()) if self._screen_id == "checking" else ""
        self._secret.setText(masked)
        self._secret.setVisible(bool(masked))
        self._primary.setText(copy.primary)
        self._secondary.setText(copy.secondary)
        self._secondary.setVisible(bool(copy.secondary))
        tone = "warning" if self._screen_id.startswith("error_") else "accent"
        self._mark.setProperty("tone", tone)
        self._mark.style().unpolish(self._mark)
        self._mark.style().polish(self._mark)

    def _show_page(self, screen_id: str) -> None:
        if screen_id == "intro":
            index = 4
            self._fill_intro(self._copy().body)
        elif screen_id == "welcome":
            index = 0
        elif screen_id == "provider":
            index = 1
        elif screen_id == "models":
            index = 5
        elif screen_id in ("key", "key_custom"):
            index = _KEY_PAGE
        else:
            index = _EMPTY_PAGE
        self._stack.setCurrentIndex(index)
        fill = screen_id in ("provider", "models")
        vertical = QSizePolicy.Policy.Expanding if fill else QSizePolicy.Policy.Maximum
        self._stack.setSizePolicy(QSizePolicy.Policy.Preferred, vertical)
        self._column_layout.setStretch(self._stack_index, 1 if fill else 0)
        self._column_layout.setStretch(self._tail_index, 0 if fill else 1)
        self._stack.updateGeometry()
        self._column_layout.invalidate()
        width = _PROVIDER_WIDTH if fill else _COLUMN_WIDTH
        self._column.setMinimumWidth(width)
        self._column.setMaximumWidth(width)
        self._pages.setMinimumWidth(width)

    def _sync_key_form(self, screen_id: str) -> None:
        if screen_id not in ("key", "key_custom"):
            return
        provider = self._provider()
        custom = provider.custom
        self._url.setVisible(custom)
        self._address.setVisible(not custom)
        self._address.setText(provider.base_url)
        self._console.setText(provider.console_hint)
        self._console.updateGeometry()

    def _refresh_actions(self, *_args: object) -> None:
        if not hasattr(self, "_primary"):
            return
        copy = self._copy()
        hint = self._hint_for(copy)
        self._hint.setText(hint)
        self._hint_wrap.setVisible(bool(hint))
        self._primary.setEnabled(self._can_continue())

    def _hint_for(self, copy: ScreenCopy) -> str:
        if self._screen_id == "models" and self._default_model.currentIndex() < 0:
            return "添加至少一个模型，再选择默认聊天模型。"
        if self._screen_id == "provider" and self._selected is None:
            return NEED_PROVIDER
        if self._screen_id == "key_custom" and not valid_http_url(self._url.text()):
            return BAD_URL if self._url.text().strip() else NEED_URL
        if self._screen_id in ("key", "key_custom") and not self._key.text().strip():
            return NEED_KEY
        return copy.hint

    def _can_continue(self) -> bool:
        if self._screen_id == "models":
            return (
                self._default_model.currentIndex() >= 0
                and not self.actual_models._probing
                and not self.actual_models._saving
            )
        if self._screen_id == "checking":
            return False
        if self._screen_id == "provider":
            return self._selected is not None
        if self._screen_id == "key_custom":
            return valid_http_url(self._url.text())
        return True

    def _mark_rail(self, screen_id: str) -> None:
        button = self._rail_buttons.get(screen_id)
        if button is None:
            return
        self._rail_group.blockSignals(True)
        button.setChecked(True)
        self._rail_group.blockSignals(False)

    def _on_rail_index(self, index: int) -> None:
        button = self._rail_group.button(index)
        if button is None:
            return
        screen_id = str(button.property("screenId"))
        if screen_id != self._screen_id:
            self.show_screen(screen_id)

    def _on_primary(self) -> None:
        if not self._primary.isEnabled():
            return
        screen = self._screen_id
        if screen == "intro":
            self.show_screen("welcome")
        elif screen == "welcome":
            self.show_screen("provider")
        elif screen == "provider":
            target = "key_custom" if self._provider().custom else "key"
            self.show_screen(target)
            self._key.setFocus()
        elif screen in ("key", "key_custom"):
            self._begin_check()
        elif screen == "models":
            if self._preview:
                self.show_screen("success")
            else:
                self.model_selected.emit(str(self._default_model.currentData()))
        elif screen == "success":
            if self._preview:
                self.show_screen("done")
            else:
                self._detach()
                self.finished.emit()
        elif screen == "error_key":
            target = "key_custom" if self._provider().custom else "key"
            self.show_screen(target)
            self._key.setFocus()
        elif screen in ("error_network", "error_timeout"):
            self._begin_check()
        elif screen == "browse":
            self.show_screen("provider")
        elif screen == "done":
            self.show_screen("intro")
        elif screen == "later":
            self.show_screen("welcome")

    def _on_secondary(self) -> None:
        screen = self._screen_id
        if screen in ("intro", "welcome"):
            self.show_screen("browse")
        elif screen == "browse":
            if self._preview:
                self.show_screen("later")
            else:
                self._detach()
                self.skipped.emit()
        elif screen == "checking":
            self.show_screen(self._key_screen())
        elif screen == "success":
            self._begin_check()
        elif screen == "models":
            self.show_screen(self._key_screen())
        elif screen in ("key", "key_custom", "error_key", "error_network", "error_timeout"):
            self.show_screen("provider")
        else:
            self.show_screen("welcome")

    def _on_escape(self) -> None:
        """Esc 沿设置退回一步，不触发「再检查一次」这类次按钮。"""
        if self._screen_id == "intro":
            return
        if self._screen_id == "welcome":
            self.show_screen("intro")
            return
        if self._screen_id in ("checking", "models", "success", "error_key"):
            self.show_screen(self._key_screen())
            return
        if self._screen_id in ("key", "key_custom", "error_network", "error_timeout"):
            self.show_screen("provider")
            return
        self.show_screen("welcome")

    def _begin_check(self) -> None:
        self.show_screen("checking", auto_continue=self._preview)
        if not self._preview:
            self.check_requested.emit(
                self._check_generation, self._current_url(), self._key.text().strip()
            )

    def _key_screen(self) -> str:
        return "key_custom" if self._provider().custom else "key"

    def _finish_check(self) -> None:
        if self._screen_id == "checking":
            self.show_screen("models")

    def _prepare_preview_models(self) -> None:
        endpoint = EndpointConfig(
            id=self._provider().id,
            name=self.display_name() or self._provider().name,
            base_url=self._current_url() or "https://preview.example/v1",
            api=(
                ProviderProtocol.ANTHROPIC_MESSAGES
                if self._provider().id == "claude"
                else ProviderProtocol.OPENAI_COMPLETIONS
            ),
        )
        if self.actual_models._endpoint != endpoint:
            self.actual_models.activate_endpoint(endpoint, discover=False)
            self.update_default_models(())
        self._preview_discovery()

    def _toggle_reveal(self) -> None:
        hidden = self._key.echoMode() == QLineEdit.EchoMode.Password
        mode = QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password
        self._key.setEchoMode(mode)
        self._reveal.setText(REVEAL_HIDE if hidden else REVEAL_SHOW)


def _add_field(layout: QVBoxLayout, caption: str, field: QWidget) -> None:
    label = QLabel(caption)
    label.setObjectName("oobeCaption")
    layout.addWidget(label)
    layout.addSpacing(6)
    layout.addWidget(field)


class _WrapLabel(QLabel):
    """中文没有空格，默认最小宽度会把整句撑成一行。这里按栏宽折行。"""

    def __init__(self, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setMaximumWidth(_COLUMN_WIDTH)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:
        return True

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        height = self.heightForWidth(_COLUMN_WIDTH)
        return QSize(_COLUMN_WIDTH, max(height, 1))

    def heightForWidth(self, width: int) -> int:
        return super().heightForWidth(width)


def _wrapping_label(object_name: str) -> QLabel:
    return _WrapLabel(object_name)


def _stylesheet() -> str:
    family = theme.FONT_FAMILY
    return f"""
    QWidget#oobeColumn, QWidget#oobeStack,
    QWidget#oobePages, QStackedWidget#oobePagesStack, QLabel {{
        background: transparent;
        font-family: {family};
    }}
    QFrame#oobeRail {{
        background: {theme.BG_SURFACE};
        border: none;
        border-right: 1px solid {theme.BORDER};
    }}
    QLabel#oobeRailTitle {{
        color: {theme.TEXT_PRIMARY};
        font-family: {family};
        font-size: 15px;
        font-weight: 600;
        padding-left: 8px;
    }}
    QLabel#oobeRailNote {{
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        font-size: {theme.FS_TINY}px;
        padding: 0 8px;
    }}
    QLabel#oobeRailGroup {{
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        font-size: {theme.FS_TINY}px;
        font-weight: 600;
    }}
    QPushButton#oobeRailButton {{
        background: transparent;
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        border: 1px solid transparent;
        border-radius: {theme.RADIUS_SM}px;
        text-align: left;
        font-weight: 400;
        padding: 6px 8px;
    }}
    QPushButton#oobeRailButton:hover {{
        background: {theme.BG_SURFACE_HOVER};
        color: {theme.TEXT_PRIMARY};
    }}
    QPushButton#oobeRailButton:checked {{
        background: {theme.BG_ELEVATED};
        color: {theme.TEXT_PRIMARY};
        font-weight: 600;
    }}
    QFrame#oobeMark {{
        background: {theme.ACCENT};
        border: none;
        border-radius: 1px;
    }}
    QFrame#oobeMark[tone="warning"] {{
        background: {theme.WARNING};
    }}
    QLabel#oobeEyebrow {{
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        font-size: {theme.FS_SMALL}px;
    }}
    QLabel#oobeTitle {{
        color: {theme.TEXT_PRIMARY};
        font-family: {family};
        font-size: 28px;
        font-weight: 600;
    }}
    QLabel#oobeBody, QLabel#oobeConsole {{
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        font-size: 15px;
    }}
    QLabel#oobeIntroParagraph {{
        color: {theme.TEXT_PRIMARY};
        font-family: {family};
        font-size: 16px;
    }}
    QLabel#oobeHint, QLabel#oobeWelcomeNote, QLabel#oobeCaption {{
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        font-size: {theme.FS_SMALL}px;
    }}
    QLabel#oobeStepIndex {{
        color: {theme.ACCENT};
        font-family: {family};
        font-size: 16px;
        font-weight: 600;
    }}
    QLabel#oobeStepLabel {{
        color: {theme.TEXT_PRIMARY};
        font-family: {family};
        font-size: 16px;
    }}
    QLabel#oobeAddressValue, QLabel#oobeSecret {{
        color: {theme.TEXT_PRIMARY};
        font-family: {theme.FONT_MONO};
        font-size: {theme.FS_SMALL}px;
    }}
    QComboBox#oobeMoreCombo {{
        font-family: {family};
        font-size: 14px;
    }}
    QComboBox#oobeMoreCombo[selected="true"] {{
        background: {theme.qss_alpha(theme.ACCENT, 0.14)};
        border-color: {theme.ACCENT};
    }}
    QFrame#oobeProviderRow {{
        background: transparent;
        border: 1px solid {theme.BORDER};
        border-radius: {theme.RADIUS_MD}px;
    }}
    QFrame#oobeProviderRow:hover {{
        background: {theme.BG_SURFACE_HOVER};
    }}
    QFrame#oobeProviderRow[selected="true"] {{
        background: {theme.qss_alpha(theme.ACCENT, 0.14)};
        border-color: {theme.ACCENT};
    }}
    QLabel#oobeProviderName {{
        color: {theme.TEXT_PRIMARY};
        font-family: {family};
        font-size: 14px;
        font-weight: 600;
    }}
    QLabel#oobeProviderDetail {{
        color: {theme.TEXT_SECONDARY};
        font-family: {family};
        font-size: {theme.FS_SMALL}px;
    }}
    QScrollArea#oobeScroll {{
        background: transparent;
        border: none;
    }}
    QPushButton#oobePrimary, QPushButton#oobeSecondary, QPushButton#oobeReveal {{
        font-family: {family};
    }}
    QPushButton#oobeReveal {{
        padding: 7px 0;
    }}
    """


def main() -> int:
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv)
    theme.set_palette("dark")
    app.setStyleSheet(theme.app_stylesheet())
    window = OobeDemo()
    window.resize(1080, 740)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
