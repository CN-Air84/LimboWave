"""逻辑模型页：用户手动建立的分类，按 ID 自动匹配或手动配对实际模型。

设计要点（设计计划 §四.1 / §四.2）：

- 逻辑模型**只由用户建立**。ID 可以手输，也可以引用实际模型的 ID（输入时补全，
  或点「引用实际模型」列出候选）。已被现有逻辑模型绑定的实际模型 ID 不在候选里。
  从站点导入实际模型不会在这里多出任何条目。
- 保存新模型时按归一规则（忽略大小写 / 首尾空白 / 末尾 ``-free``）自动匹配实际模型，
  每个站点至多绑一个；输入 ID 时就预告会绑定哪些。
- 手动配对从实际模型目录里挑。已绑定到别的逻辑模型的实际模型不可选——
  **一个实际模型只能绑定一个逻辑模型**。
- 能力声明属于实际模型：这里展示、可改默认思考强度、可重新检测，结果写回实际模型目录。

架构约束同设置对话框：不直接碰仓库，每个动作经 ``SettingsService`` 落盘；
校验失败弹错误框，配置不变；路由预览是纯展示，不触发网络请求。
"""

from __future__ import annotations

from difflib import SequenceMatcher

from PySide6.QtCore import QEvent, QObject, QStringListModel, Qt, Signal
from PySide6.QtGui import QMouseEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.model_probe import ModelProbeResult
from limbowave.application.services.routing_service import RoutingService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.model_matching import MatchPlan, match_rank, plan_for_logical
from limbowave.domain.model_normalize import normalize_model_id
from limbowave.domain.models import LogicalModel, ModelBinding
from limbowave.domain.routing import RoutingError
from limbowave.ui.animated_stack import AnimatedPageStack, AnimatedTabBar
from limbowave.ui.marquee_list import MarqueeListWidget
from limbowave.ui.model_probe_page import ModelProbeTask

_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_NEW_MODEL_GUIDE = (
    "逻辑模型由你手动建立：输入 ID，或点「引用实际模型」借用某个实际模型的 ID。"
    "保存时会按 ID（忽略大小写与末尾 -free）自动绑定各站点匹配的实际模型。"
)


def _show_error(parent: QWidget, exc: Exception) -> None:
    from limbowave.ui.floating import ask_alert

    ask_alert(parent, "无法保存", str(exc))


def _capabilities_text(binding: ModelBinding) -> str:
    caps: list[str] = []
    if binding.supports_thinking is False:
        caps.append("不支持思考")
    elif binding.available_thinking_levels:
        fixed = "（固定）" if binding.thinking_level_locked else ""
        caps.append(f"思考={'/'.join(binding.available_thinking_levels)}{fixed}")
    elif binding.default_thinking_level:
        caps.append(f"思考={binding.default_thinking_level}")
    if binding.supports_tools:
        caps.append("工具")
    return f"　[{' '.join(caps)}]" if caps else ""


class _TokenLimitCombo(QComboBox):
    """预设显示短标签，配置仍保存精确 token 数；留空或 0 表示默认。"""

    def __init__(self, presets: tuple[tuple[str, int], ...], maximum: int) -> None:
        super().__init__()
        self._maximum = maximum
        self.setEditable(True)
        edit = self.lineEdit()
        assert edit is not None
        edit.installEventFilter(self)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItem("（默认）", 0)
        for label, value in presets:
            self.addItem(label, value)
            self.setItemData(self.count() - 1, f"{value} tokens", Qt.ItemDataRole.ToolTipRole)
        self.setMinimumHeight(38)
        self.setMaximumWidth(260)
        self.setToolTip(
            "双击输入框或点击箭头可选择预设；可输入整数，留空或 0 使用默认值。\n"
            + "；".join(f"{label} = {value}" for label, value in presets)
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.lineEdit()
            and event.type() == QEvent.Type.MouseButtonDblClick
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.showPopup()
            return True
        return super().eventFilter(watched, event)

    def value(self) -> int:
        text = self.currentText().strip()
        if not text:
            return 0
        index = self.findText(text, Qt.MatchFlag.MatchFixedString)
        if index >= 0:
            return int(self.itemData(index))
        if not text.isascii() or not text.isdecimal() or len(text) > 10:
            raise ValueError("Token 上限请填写非负整数，或选择预设。")
        value = int(text)
        if value > self._maximum:
            raise ValueError(f"Token 上限不能超过 {self._maximum}。")
        return value

    def setValue(self, value: int) -> None:
        index = self.findData(value)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setCurrentIndex(-1)
            self.setEditText(str(value))


class LogicalModelsTab(QWidget):
    """逻辑模型：列表 + 表单 + 实际模型绑定（自动匹配 / 手动配对）+ 路由预览。"""

    probe_requested = Signal(object)  # ModelProbeTask：重新检测选中绑定的实际模型

    def __init__(self, settings: SettingsService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._config = AppConfiguration()
        self._probing: set[tuple[str, str]] = set()
        self._auto_name: str | None = None  # 根据 ID 自动生成的显示名（用户手填的不覆盖）
        self._build()
        self.reload()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._list = MarqueeListWidget()
        # 可多选：一次删掉多个不要的逻辑模型
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.currentItemChanged.connect(self._on_select)
        left_layout.addWidget(self._list, 1)
        new_btn = QPushButton("新建逻辑模型")
        new_btn.clicked.connect(self._on_new)
        left_layout.addWidget(new_btn)
        splitter.addWidget(left)

        right = QWidget()
        workspace_layout = QVBoxLayout(right)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(10)
        host = QWidget()
        host.setObjectName("modelSubtabHost")
        host_layout = QHBoxLayout(host)
        host_layout.setContentsMargins(4, 0, 4, 0)
        host_layout.addStretch(1)
        self._subtabs = AnimatedTabBar(indicator_name="modelSubtabIndicator")
        self._subtabs.setObjectName("modelSubtabs")
        self._subtabs.setExpanding(False)
        self._subtabs.setDocumentMode(True)
        self._subtabs.setDrawBase(False)
        host_layout.addWidget(self._subtabs)
        host_layout.addStretch(1)
        workspace_layout.addWidget(host)
        self._stack = AnimatedPageStack(orientation=Qt.Orientation.Horizontal)
        self._stack.setObjectName("modelSubtabStack")
        workspace_layout.addWidget(self._stack, 1)
        self._configuration_page = QWidget()
        self._binding_page = QWidget()
        for title, page in (
            ("逻辑模型设置", self._configuration_page), ("模型绑定", self._binding_page)
        ):
            page.setObjectName("modelSubtabPage")
            self._subtabs.addTab(title)
            self._stack.add_page(page)
        self._subtabs.currentChanged.connect(self._stack.set_index)
        right_layout = QVBoxLayout(self._configuration_page)
        right_layout.setContentsMargins(18, 14, 18, 18)
        right_layout.setSpacing(10)
        binding_layout = QVBoxLayout(self._binding_page)
        binding_layout.setContentsMargins(18, 14, 18, 18)
        binding_layout.setSpacing(10)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self._id = QLineEdit()
        self._id.setPlaceholderText("手动输入，或引用实际模型的 ID")
        self._id.textChanged.connect(self._on_id_changed)
        self._id_candidates = QStringListModel(self)
        self._completer = QCompleter(self._id_candidates, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(12)
        self._id.setCompleter(self._completer)
        self._reference_btn = QPushButton("引用实际模型…")
        self._reference_btn.setProperty("flat", True)
        self._reference_btn.setToolTip(
            "从已导入的实际模型里挑一个 ID 作为逻辑模型 ID（已被其他逻辑模型绑定的不列出）"
        )
        self._reference_btn.clicked.connect(self._on_reference)
        id_row = QHBoxLayout()
        id_row.setSpacing(8)
        id_row.addWidget(self._id, 1)
        id_row.addWidget(self._reference_btn)
        self._name = QLineEdit()
        self._name.setPlaceholderText("留空 = 与 ID 相同")
        self._context_window = _TokenLimitCombo(
            (("256K", 256_000), ("262K", 262_144), ("1M", 1_000_000)), 10_000_000
        )
        self._max_tokens = _TokenLimitCombo(
            (("128000", 128_000), ("65536", 65_536), ("16384", 16_384)), 1_000_000
        )
        self._images = QCheckBox("支持图片输入")
        form.addRow("ID", id_row)
        form.addRow("显示名", self._name)
        form.addRow("上下文窗口", self._context_window)
        form.addRow("输出上限", self._max_tokens)
        form.addRow("", self._images)
        right_layout.addLayout(form)

        # 自动匹配：新建时预告，保存 / 手动触发后报告结果
        self._match_hint = QLabel("")
        self._match_hint.setProperty("hint", True)
        self._match_hint.setWordWrap(True)
        right_layout.addWidget(self._match_hint)

        ops = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        ops.addWidget(save_btn)
        default_model_btn = QPushButton("设为默认模型")
        default_model_btn.clicked.connect(self._on_default_model)
        ops.addWidget(default_model_btn)
        delete_btn = QPushButton("删除")
        delete_btn.setToolTip("删除选中的逻辑模型（可多选）；实际模型与检测结果保留")
        delete_btn.clicked.connect(self._on_delete)
        ops.addWidget(delete_btn)
        ops.addStretch(1)
        right_layout.addLayout(ops)
        right_layout.addStretch(1)
        # 绑定管理
        binding_layout.addWidget(QLabel("绑定的实际模型（★ 为默认站点，每个站点至多一个）："))
        self._bindings = QListWidget()
        self._bindings.setObjectName("modelBindingsList")
        self._bindings.setMinimumHeight(120)
        self._bindings.setMaximumHeight(160)
        self._bindings.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._bindings.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._bindings.setToolTip("点击一行选择实际模型，再设置默认站点、解除绑定或检测能力")
        self._bindings.currentItemChanged.connect(self._on_binding_selected)
        binding_layout.addWidget(self._bindings)
        pair_row = QHBoxLayout()
        self._pair_site_combo = QComboBox()
        self._pair_site_combo.setMaxVisibleItems(16)
        self._pair_site_combo.setAccessibleName("配对站点")
        self._pair_site_combo.setToolTip("先选择站点，再选择该站点下的实际模型 ID")
        self._pair_site_combo.currentIndexChanged.connect(self._fill_pair_models)
        pair_row.addWidget(self._pair_site_combo, 1)
        self._pair_combo = QComboBox()
        self._pair_combo.setAccessibleName("配对实际模型 ID")
        self._pair_combo.setMaxVisibleItems(16)
        self._pair_combo.setToolTip("已绑定到其他逻辑模型的实际模型不可选：一个实际模型只能绑定一个逻辑模型")
        pair_row.addWidget(self._pair_combo, 1)
        self._pair_btn = QPushButton("手动配对")
        self._pair_btn.clicked.connect(self._on_pair)
        pair_row.addWidget(self._pair_btn)
        binding_layout.addLayout(pair_row)
        bind_ops = QHBoxLayout()
        self._default_binding_btn = QPushButton("设为默认站点")
        self._default_binding_btn.clicked.connect(self._on_default_binding)
        bind_ops.addWidget(self._default_binding_btn)
        self._unbind_btn = QPushButton("解除绑定")
        self._unbind_btn.clicked.connect(self._on_unbind)
        bind_ops.addWidget(self._unbind_btn)
        self._auto_match_btn = QPushButton("按 ID 自动匹配")
        self._auto_match_btn.setToolTip("为还没有手动绑定的站点，绑定 ID 归一后相同的实际模型")
        self._auto_match_btn.clicked.connect(self._on_auto_match)
        bind_ops.addWidget(self._auto_match_btn)
        bind_ops.addStretch(1)
        binding_layout.addLayout(bind_ops)

        # 实际模型（站点-模型）级能力声明（§二.4 模型级覆盖）：
        # 思考强度与工具支持是「某站点上的某模型」的属性，写回实际模型目录
        cap_row = QHBoxLayout()
        self._bind_thinking = QComboBox()
        self._bind_thinking.addItem("（内核默认）", None)
        for level in _THINKING_LEVELS:
            self._bind_thinking.addItem(level, level)
        self._bind_thinking.setToolTip("路由到该实际模型时应用的默认思考强度")
        self._bind_thinking.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        cap_row.addWidget(self._bind_thinking)
        bind_apply = QPushButton("保存默认思考强度")
        bind_apply.setProperty("flat", True)
        bind_apply.clicked.connect(self._on_apply_binding_caps)
        cap_row.addWidget(bind_apply)
        self._probe_btn = QPushButton("重新检测能力")
        self._probe_btn.setProperty("flat", True)
        self._probe_btn.setToolTip("按流式响应 → 思考等级 → 工具调用重新实测选中的实际模型")
        self._probe_btn.clicked.connect(self._on_probe)
        cap_row.addWidget(self._probe_btn)
        self._probe_status = QLabel("能力来自实际模型的检测结果")
        self._probe_status.setProperty("hint", True)
        self._probe_status.setWordWrap(True)  # 窄栏下换行，不把整行撑宽
        cap_row.addWidget(self._probe_status, 1)
        binding_layout.addLayout(cap_row)

        # 路由预览（纯展示，不触发网络）
        self._preview = QLabel("")
        self._preview.setProperty("hint", True)
        self._preview.setWordWrap(True)
        binding_layout.addWidget(self._preview)

        binding_layout.addStretch(1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 520])
        root.addWidget(splitter)

    # ---------- 数据 ----------

    def reload(self) -> None:
        """重新加载配置；保留当前选中的模型和表单里尚未保存的编辑。"""
        self._config = self._settings.load()
        selected = self._selected_id()
        self._list.blockSignals(True)
        self._list.clear()
        for model in self._config.models:
            mark = "★ " if model.id == self._config.default_model_id else ""
            unbound = "　· 未绑定" if not model.bindings else ""
            item = QListWidgetItem(model.name)
            item.setData(Qt.ItemDataRole.UserRole, model.id)
            item.setToolTip(f"{mark}{model.name}{unbound}")
            self._list.addItem(item)
            if model.id == selected:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        # 引用候选 = 未被任何逻辑模型绑定的实际模型 ID（一个实际模型只能绑一个逻辑模型，
        # 已绑定的 ID 再引用只会造出重复/抢不到绑定的逻辑模型）
        bound_ids = {b.model_id for m in self._config.models for b in m.bindings}
        self._id_candidates.setStringList(
            sorted(
                {a.model_id for a in self._config.actual_models if a.model_id not in bound_ids},
                key=str.lower,
            )
        )
        if selected is not None and self._current_model() is None:
            self._on_new()  # 选中的模型已不在配置里
            return
        self._refresh_detail()
        if selected is None:
            self._refresh_match_preview()  # 新建中：目录可能刚导入了新的实际模型

    def _selected_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _current_model(self) -> LogicalModel | None:
        model_id = self._selected_id()
        return next((m for m in self._config.models if m.id == model_id), None)

    def _endpoint_name(self, endpoint_id: str) -> str:
        return next((e.name for e in self._config.endpoints if e.id == endpoint_id), endpoint_id)

    def _select_model(self, model_id: str) -> None:
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == model_id:
                self._list.clearSelection()
                self._list.setCurrentItem(item)
                return

    def _on_select(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        model = self._current_model()
        if model is None:
            return
        direction = (
            -1
            if previous is not None
            and self._list.row(current) < self._list.row(previous)
            else 1
        )
        self._stack.capture_refresh(direction)
        self._id.blockSignals(True)
        self._id.setText(model.id)
        self._id.blockSignals(False)
        self._id.setReadOnly(True)  # id 是会话与路由的引用锚点，不允许改（改 id = 新建）
        self._reference_btn.setEnabled(False)
        self._name.setText(model.name)
        self._context_window.setValue(model.context_window or 0)
        self._max_tokens.setValue(model.max_tokens or 0)
        self._images.setChecked(model.supports_images)
        self._auto_name = None
        self._match_hint.clear()
        self._refresh_detail()
        self._stack.animate_refresh(self._subtabs.currentIndex())

    def _on_new(self) -> None:
        self._subtabs.setCurrentIndex(0)
        self._list.setCurrentRow(-1)
        self._list.clearSelection()
        self._id.setReadOnly(False)
        self._reference_btn.setEnabled(True)
        self._id.clear()
        self._name.clear()
        self._auto_name = None
        self._context_window.setValue(0)
        self._max_tokens.setValue(0)
        self._images.setChecked(False)
        self._probe_status.setText("能力来自实际模型的检测结果")
        self._refresh_detail()
        self._refresh_match_preview()
        self._id.setFocus()

    def _refresh_detail(self) -> None:
        """绑定列表、配对候选、路由预览随当前模型刷新（不动表单字段）。"""
        model = self._current_model()
        self._show_bindings(model)
        self._fill_pair_combo(model)
        self._refresh_preview(model)
        for widget in (
            self._default_binding_btn,
            self._unbind_btn,
            self._auto_match_btn,
            self._probe_btn,
        ):
            widget.setEnabled(model is not None)

    def _show_bindings(self, model: LogicalModel | None) -> None:
        selected = self._selected_binding_endpoint()
        self._bindings.blockSignals(True)
        self._bindings.clear()
        if model is not None:
            for index, binding in enumerate(model.bindings):
                star = "★ " if index == model.default_binding else ""
                auto = "（自动匹配）" if binding.auto_matched else ""
                item = QListWidgetItem(
                    f"{star}{self._endpoint_name(binding.endpoint_id)} · {binding.model_id}"
                    f"{auto}{_capabilities_text(binding)}"
                )
                item.setToolTip(item.text())
                item.setData(Qt.ItemDataRole.UserRole, binding.endpoint_id)
                self._bindings.addItem(item)
                if binding.endpoint_id == selected:
                    self._bindings.setCurrentItem(item)
        self._bindings.blockSignals(False)
        if self._bindings.currentItem() is not None:
            self._on_binding_selected(self._bindings.currentItem(), None)

    def _selected_binding(self) -> ModelBinding | None:
        model = self._current_model()
        endpoint_id = self._selected_binding_endpoint()
        if model is None or endpoint_id is None:
            return None
        return next((b for b in model.bindings if b.endpoint_id == endpoint_id), None)

    def _selected_binding_endpoint(self) -> str | None:
        item = self._bindings.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _fill_pair_combo(self, model: LogicalModel | None) -> None:
        """手动配对候选：目录里不属于本模型的实际模型；属于别的逻辑模型的置灰。"""
        previous_site = self._pair_site_combo.currentData()
        self._pair_site_combo.blockSignals(True)
        self._pair_site_combo.clear()
        for endpoint in self._config.endpoints:
            self._pair_site_combo.addItem(endpoint.name, endpoint.id)
        index = self._pair_site_combo.findData(previous_site)
        self._pair_site_combo.setCurrentIndex(index if index >= 0 else 0)
        self._pair_site_combo.setEnabled(model is not None and bool(self._config.endpoints))
        self._pair_site_combo.blockSignals(False)
        self._fill_pair_models()

    def _fill_pair_models(self, _index: int = -1) -> None:
        """第二级仅列出当前站点的候选，按与逻辑 ID 的匹配程度排序。"""
        previous_key = self._pair_combo.currentData()
        self._pair_combo.clear()
        model = self._current_model()
        endpoint_id = self._pair_site_combo.currentData()
        self._pair_combo.setEnabled(model is not None and endpoint_id is not None)
        self._pair_btn.setEnabled(False)
        if model is None:
            self._pair_combo.addItem("先保存逻辑模型，再手动配对实际模型", None)
            self._disable_combo_item(0)
            return
        owners = {b.key: m.id for m in self._config.models for b in m.bindings}

        def sort_key(model_id: str) -> tuple[int, float, str, str]:
            rank = match_rank(model.id, model_id)
            similarity = SequenceMatcher(
                None, normalize_model_id(model.id), normalize_model_id(model_id), autojunk=False
            ).ratio()
            return (rank if rank is not None else 3, -similarity, model_id.lower(), model_id)

        candidates = sorted(
            (a for a in self._config.actual_models
             if a.endpoint_id == endpoint_id and owners.get(a.key) != model.id),
            key=lambda a: sort_key(a.model_id),
        )
        first_enabled = -1
        selected = -1
        for actual in candidates:
            owner = owners.get(actual.key)
            text = actual.model_id + (f"（已绑定：{owner}）" if owner else "")
            self._pair_combo.addItem(text, actual.key)
            index = self._pair_combo.count() - 1
            if owner:
                self._disable_combo_item(index)
            else:
                if first_enabled < 0:
                    first_enabled = index
                if actual.key == previous_key:
                    selected = index
        if self._pair_combo.count() == 0:
            self._pair_combo.addItem("本站点没有可配对的实际模型：请先导入或检查现有绑定", None)
            self._disable_combo_item(0)
        self._pair_combo.setCurrentIndex(selected if selected >= 0 else max(first_enabled, 0))
        self._pair_btn.setEnabled(first_enabled >= 0)

    def _disable_combo_item(self, index: int) -> None:
        model = self._pair_combo.model()
        if isinstance(model, QStandardItemModel):
            item = model.item(index)
            if item is not None:
                item.setEnabled(False)

    def _pair_choice(self) -> tuple[str, str] | None:
        index = self._pair_combo.currentIndex()
        model = self._pair_combo.model()
        if index < 0 or not isinstance(model, QStandardItemModel):
            return None
        item = model.item(index)
        key = self._pair_combo.currentData()
        if item is None or not item.isEnabled() or not key:
            return None
        return (str(key[0]), str(key[1]))

    def _refresh_preview(self, model: LogicalModel | None) -> None:
        """路由预览：确定性路由的结果 + 原因。纯展示，不发请求。"""
        if model is None:
            self._preview.clear()
            return
        try:
            decision = RoutingService(self._config).route(model.id)
        except RoutingError as exc:
            self._preview.setText(f"路由预览：{exc}")
            return
        self._preview.setText(
            f"路由预览：{decision.model.id} → {decision.endpoint.name}"
            f"（{decision.endpoint.base_url}），远端模型 {decision.model_id}。"
            f"原因：{decision.reason}"
        )

    # ---------- 新建：引用实际模型 ID + 自动匹配预告 ----------

    def _on_reference(self) -> None:
        if not self._config.actual_models:
            self._match_hint.setText("还没有实际模型可引用：先在「站点端点 → 实际模型」导入。")
            return
        if not self._id_candidates.stringList():
            self._match_hint.setText(
                "没有可引用的实际模型：已导入的 ID 都被现有逻辑模型绑定了；"
                "也可以直接手输一个新 ID。"
            )
            return
        self._id.setFocus()
        self._completer.setCompletionPrefix(self._id.text().strip())
        self._completer.complete()

    def _on_id_changed(self, _text: str) -> None:
        if self._current_model() is None:
            self._fill_name_from_id()
            self._refresh_match_preview()

    def _fill_name_from_id(self) -> None:
        """将 ID 的连字符替换为空格并转为标题格式，保留用户手填的显示名。"""
        current = self._name.text()
        if current.strip() and current != self._auto_name:
            return
        name = self._id.text().strip().replace("-", " ").title()
        self._auto_name = name or None
        self._name.setText(name)

    def _refresh_match_preview(self) -> None:
        logical_id = self._id.text().strip()
        if not logical_id:
            self._match_hint.setText(_NEW_MODEL_GUIDE)
        elif any(m.id == logical_id for m in self._config.models):
            self._match_hint.setText(f"逻辑模型 ID {logical_id!r} 已存在。")
        else:
            plan = plan_for_logical(self._config, logical_id)
            self._match_hint.setText(self._describe_plan(plan, done=False))

    def _describe_plan(self, plan: MatchPlan, *, done: bool) -> str:
        def _label(endpoint_id: str, model_id: str) -> str:
            return f"{self._endpoint_name(endpoint_id)} · {model_id}"

        parts: list[str] = []
        if plan.bind:
            items = "、".join(_label(a.endpoint_id, a.model_id) for a in plan.bind)
            verb = "已自动绑定" if done else "保存后将自动绑定"
            parts.append(f"{verb} {len(plan.bind)} 个实际模型：{items}")
        elif done:
            parts.append("没有新的可自动绑定的实际模型")
        else:
            parts.append("目前没有 ID 匹配的实际模型；保存后可以手动配对")
        if plan.taken:
            items = "、".join(
                f"{_label(a.endpoint_id, a.model_id)}（属于 {owner}）" for a, owner in plan.taken
            )
            parts.append(f"ID 匹配但已绑定到其他逻辑模型、不会改动：{items}")
        if plan.ambiguous:
            items = "；".join(
                f"{self._endpoint_name(endpoint_id)}：{'、'.join(ids)}"
                for endpoint_id, ids in plan.ambiguous
            )
            parts.append(f"同一站点有多个同样接近的候选，请手动配对：{items}")
        return "。".join(parts) + "。"

    # ---------- 动作 ----------

    def _on_save(self) -> None:
        model_id = self._id.text().strip()
        name = self._name.text().strip() or model_id
        supports_images = self._images.isChecked()
        existing = self._current_model()
        plan: MatchPlan | None = None
        try:
            context_window = self._context_window.value() or None
            max_tokens = self._max_tokens.value() or None
            if existing is None:
                if not model_id:
                    raise ValueError("请填写逻辑模型 ID（手动输入，或引用实际模型的 ID）。")
                _config, plan = self._settings.create_model(
                    LogicalModel(
                        id=model_id,
                        name=name,
                        context_window=context_window,
                        max_tokens=max_tokens,
                        supports_images=supports_images,
                    )
                )
            else:
                # 绑定以最新配置为准：探测、配对可能刚刚改过它们
                fresh = next(
                    (m for m in self._settings.load().models if m.id == existing.id), existing
                )
                self._settings.upsert_model(
                    LogicalModel(
                        id=fresh.id,
                        name=name,
                        bindings=fresh.bindings,
                        default_binding=fresh.default_binding,
                        context_window=context_window,
                        max_tokens=max_tokens,
                        supports_images=supports_images,
                    )
                )
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()
        self._select_model(model_id)
        if plan is not None:
            self._match_hint.setText(self._describe_plan(plan, done=True))
        self._subtabs.setCurrentIndex(1)

    def _on_default_model(self) -> None:
        model = self._current_model()
        if model is None:
            return
        try:
            self._settings.set_default_model(model.id)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()

    def _on_delete(self) -> None:
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in self._list.selectedItems()]
        if not ids and self._current_model() is not None:
            ids = [self._selected_id()]
        if not ids:
            return
        if len(ids) == 1:
            self._delete_models(ids)
            return
        from limbowave.ui.floating import ask_confirm

        ask_confirm(
            self,
            "删除逻辑模型",
            f"删除选中的 {len(ids)} 个逻辑模型？它们的绑定会解除；"
            "实际模型与能力检测结果仍保留在实际模型目录里。",
            lambda ok: self._delete_models(ids) if ok else None,
            confirm_text="删除",
            danger=True,
        )

    def _delete_models(self, ids: list[str]) -> None:
        errors: list[str] = []
        for model_id in ids:
            try:
                self._settings.delete_model(model_id)
            except ValueError as exc:
                errors.append(str(exc))
        self.reload()
        self._on_new()
        if errors:
            _show_error(self, ValueError("；".join(errors)))

    def _on_pair(self) -> None:
        model = self._current_model()
        choice = self._pair_choice()
        if model is None or choice is None:
            return
        try:
            self._settings.pair_actual_model(model.id, *choice)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()
        endpoint_id, remote_id = choice
        self._match_hint.setText(f"已手动配对：{self._endpoint_name(endpoint_id)} · {remote_id}。")

    def _on_auto_match(self) -> None:
        model = self._current_model()
        if model is None:
            return
        try:
            _config, plan = self._settings.auto_match(model.id)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()
        self._match_hint.setText(self._describe_plan(plan, done=True))

    def _on_unbind(self) -> None:
        model = self._current_model()
        endpoint_id = self._selected_binding_endpoint()
        if model is None or endpoint_id is None:
            return
        try:
            self._settings.unbind_endpoint(model.id, endpoint_id)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()

    def _on_default_binding(self) -> None:
        model = self._current_model()
        row = self._bindings.currentRow()
        if model is None or row < 0:
            return
        try:
            self._settings.set_default_binding(model.id, row)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()

    def _on_binding_selected(
        self, current: QListWidgetItem | None, _prev: QListWidgetItem | None
    ) -> None:
        """选中绑定 → 把实际模型的能力声明带进能力控件（§二.4）。"""
        binding = self._selected_binding()
        if current is None or binding is None:
            return
        index = self._bind_thinking.findData(binding.default_thinking_level)
        self._bind_thinking.setCurrentIndex(index if index >= 0 else 0)
        if binding.key in self._probing:
            return
        self._probe_status.setText(
            "支持工具调用" if binding.supports_tools else "尚未证实工具调用"
        )

    def _on_apply_binding_caps(self) -> None:
        """保存默认思考强度（写回实际模型）；其余能力只由检测结果更新。"""
        binding = self._selected_binding()
        if binding is None:
            _show_error(self, ValueError("请先在绑定列表里选一条绑定"))
            return
        try:
            self._settings.set_default_thinking_level(
                binding.endpoint_id, binding.model_id, self._bind_thinking.currentData()
            )
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()

    def _on_probe(self) -> None:
        binding = self._selected_binding()
        if binding is None:
            self._probe_status.setText("请先选择一条绑定")
            return
        endpoint = next((e for e in self._config.endpoints if e.id == binding.endpoint_id), None)
        if endpoint is None:
            return
        actual = self._config.actual_model(binding.endpoint_id, binding.model_id)
        self._probing.add(binding.key)
        self._probe_status.setText(f"{binding.model_id}：正在检测…")
        self.probe_requested.emit(
            ModelProbeTask(
                endpoint,
                binding.model_id,
                actual.display_name if actual is not None else binding.model_id,
            )
        )

    def apply_probe_result(self, endpoint_id: str, result: ModelProbeResult) -> None:
        """检测结果已写进实际模型目录：刷新展示；本页发起的检测顺带报告结果。"""
        self.reload()
        if (endpoint_id, result.model_id) not in self._probing:
            return
        self._probing.discard((endpoint_id, result.model_id))
        tools = "支持工具调用" if result.supports_tools else "未证实工具调用"
        self._probe_status.setText(f"{result.model_id}：检测完成，{tools}")

    def show_probe_error(self, endpoint_id: str, model_id: str, detail: str) -> None:
        if (endpoint_id, model_id) not in self._probing:
            return
        self._probing.discard((endpoint_id, model_id))
        self._probe_status.setText(f"{model_id}：检测失败（{detail}）")
