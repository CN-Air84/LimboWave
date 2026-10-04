"""实际模型条目、手动能力声明与自动探测页面。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QHideEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.model_probe import DiscoveryResult, ModelProbeResult
from limbowave.domain.models import ActualModel, ModelCapability
from limbowave.domain.providers import EndpointConfig

CAPABILITIES: tuple[tuple[ModelCapability, str], ...] = (
    ("supports_streaming", "流式"),
    ("supports_thinking", "思考"),
    ("supports_tools", "工具"),
)


@dataclass(frozen=True, slots=True)
class ModelProbeTask:
    endpoint: EndpointConfig
    model_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ModelCapabilityChange:
    endpoint: EndpointConfig
    model_id: str
    display_name: str
    capability: ModelCapability
    supported: bool


class _ModelIdLabel(QLabel):
    """只在绘制时省略长 ID，原文仍可通过 tooltip 与可访问性接口读取。"""

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideMiddle, self.contentsRect().width(),
        )
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignVCenter, text)


class _CapabilityCheckBox(QCheckBox):
    """半选只供回显未知状态；鼠标和键盘操作始终切换到明确的布尔声明。"""

    def nextCheckState(self) -> None:
        self.setCheckState(
            Qt.CheckState.Unchecked
            if self.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )


class ActualModelRow(QFrame):
    """一个模型一组真实控件；能力声明和检测选择互相独立。"""

    capability_changed = Signal(str, bool)
    probe_requested = Signal()

    def __init__(self, model_id: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("actualModelRow")
        self.model_id = model_id
        self.display_name = name
        self.saved = False
        self.probe_succeeded = False
        self.actual: ActualModel | None = None
        self._layout_position: QPoint | None = None
        self._move_animation = QPropertyAnimation(self, b"pos", self)
        self._move_animation.setDuration(260)
        self._move_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._values: dict[ModelCapability, tuple[bool | None, str, str, str]] = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 6, 12, 6)
        root.setSpacing(14)
        self.selected = QCheckBox()
        self.selected.setToolTip("选择此模型参与批量检测；不改变能力声明")
        self.selected.setAccessibleName(f"选择检测 {model_id}")
        root.addWidget(self.selected)
        self.id_label = _ModelIdLabel(model_id)
        self.id_label.setObjectName("actualModelName")
        self.id_label.setTextFormat(Qt.TextFormat.PlainText)
        self.id_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        root.addWidget(self.id_label, 1)

        self.checkboxes: dict[ModelCapability, _CapabilityCheckBox] = {}
        for key, title in CAPABILITIES:
            checkbox = _CapabilityCheckBox(title)
            checkbox.setAccessibleName(f"{model_id} {title}能力")
            checkbox.clicked.connect(partial(self._capability_clicked, key))
            self.checkboxes[key] = checkbox
            root.addWidget(checkbox)
            self.set_capability(key, None, "未测")

        self.detect = QPushButton("检测能力")
        self.detect.clicked.connect(self.probe_requested.emit)
        root.addWidget(self.detect)
        self.status_text = "尚未检测；可直接设置能力"
        self.set_name(name)

    def _capability_clicked(self, key: ModelCapability) -> None:
        self.capability_changed.emit(key, self.checkboxes[key].isChecked())

    def set_name(self, name: str) -> None:
        self.display_name = name
        self.id_label.setToolTip(
            self.model_id if name == self.model_id else f"{name}\n{self.model_id}"
        )
        self.set_status(self.status_text)

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.setToolTip(f"{self.id_label.toolTip()}\n{text}")
        self.setAccessibleDescription(text)
        self.detect.setToolTip(
            f"{text}\n实测三项能力并更新当前声明；检测完成后仍可手动修改"
        )

    def set_capability(
        self, key: ModelCapability, value: bool | None, text: str,
        tone: str = "muted", detail: str = "",
    ) -> None:
        self._values[key] = (value, text, tone, detail)
        checkbox = self.checkboxes[key]
        checkbox.setTristate(value is None)
        checkbox.setCheckState(
            Qt.CheckState.PartiallyChecked if value is None
            else Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        )
        checkbox.setToolTip("\n".join(part for part in (
            text, detail, "勾选表示支持，取消表示不支持；修改后自动保存",
        ) if part))
        checkbox.setAccessibleDescription(text)
        if checkbox.property("capabilityTone") != tone:
            checkbox.setProperty("capabilityTone", tone)
            checkbox.style().unpolish(checkbox)
            checkbox.style().polish(checkbox)
            checkbox.update()

    def restore_capabilities(self) -> None:
        for key, values in self._values.copy().items():
            self.set_capability(key, *values)

    def apply_actual(self, actual: ActualModel) -> None:
        self.actual = actual
        self.saved = True
        for key, _title in CAPABILITIES:
            value = getattr(actual, key)
            self.set_capability(
                key, value, "未确认" if value is None else "已启用" if value else "未启用",
                "muted" if value is None or not value else "success",
            )
        if actual.supports_thinking and actual.available_thinking_levels:
            text = "、".join(actual.available_thinking_levels)
            if actual.thinking_level_locked:
                text += "（固定）"
            self.set_capability("supports_thinking", True, text, "success")
        self.set_status("已保存；可手动修改或重新检测")
        self.detect.setText("重新检测")

    def animate_from_geometry(self, previous: QRect) -> None:
        target = self.pos()
        first_layout = self._layout_position is None
        self._layout_position = target
        animation = self._move_animation
        if first_layout or not self.isVisible() or previous.size() != self.size():
            animation.stop()
            return
        self.move(previous.topLeft())
        # 布局重复计算时不重启动画；连续勾选则从当前画面位置转向新目标。
        if animation.state() == QAbstractAnimation.State.Running and animation.endValue() == target:
            return
        animation.stop()
        if self.pos() == target:
            return
        if self.selected.isChecked():
            self.raise_()
        animation.setStartValue(self.pos())
        animation.setEndValue(target)
        animation.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._move_animation.stop()
        if self._layout_position is not None:
            self.move(self._layout_position)
        super().hideEvent(event)

    def set_busy(self, busy: bool) -> None:
        for checkbox in self.checkboxes.values():
            checkbox.setEnabled(not busy)
        self.detect.setEnabled(not busy)


class _ModelListHeader(QFrame):
    """列标题使用真实模型行的列宽，跟随窗口大小和主题字体对齐。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("actualModelsHeader")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 6, 12, 6)
        self._layout.setSpacing(14)
        self.id_label = QLabel("模型id")
        self.capabilities_label = QLabel("能力")
        for label in (self.id_label, self.capabilities_label):
            label.setProperty("hint", True)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
        self._layout.addWidget(self.id_label, 1)
        self._layout.addWidget(self.capabilities_label)

    def align_to_row(self, row: ActualModelRow) -> None:
        layout = row.layout()
        if layout is not None:
            layout.activate()
        first = row.checkboxes[CAPABILITIES[0][0]].geometry()
        last = row.checkboxes[CAPABILITIES[-1][0]].geometry()
        self._layout.setContentsMargins(
            row.id_label.x(), 6, row.width() - last.right() - 1, 6,
        )
        self._layout.setSpacing(first.left() - row.id_label.geometry().right() - 1)
        self.capabilities_label.setFixedWidth(last.right() - first.left() + 1)


class _ModelRowsLayout(QVBoxLayout):
    """布局负责终点，真实模型行从当前位置平滑移过去，不用截图代替控件。"""

    def __init__(self, parent: QWidget, header: _ModelListHeader) -> None:
        super().__init__(parent)
        self._header = header

    def setGeometry(self, rect: QRect) -> None:
        previous: dict[ActualModelRow, QRect] = {}
        for index in range(self.count()):
            item = self.itemAt(index)
            row = item.widget() if item is not None else None
            if isinstance(row, ActualModelRow):
                previous[row] = row.geometry()
                # 先恢复布局终点，避免 Qt 的几何缓存把动画中间帧当成新终点。
                if row._layout_position is not None:
                    row.move(row._layout_position)
        super().setGeometry(rect)
        if previous:
            self._header.align_to_row(next(iter(previous)))
        for row, geometry in previous.items():
            row.animate_from_geometry(geometry)


class ActualModelsPage(QWidget):
    """自建模型条目列表；勾选只选择，能力编辑与显式检测分别发出意图。"""

    discovery_requested = Signal(object)  # EndpointConfig
    probe_requested = Signal(object)  # ModelProbeTask
    manual_save_requested = Signal(object)  # ModelProbeTask
    capability_save_requested = Signal(object)  # ModelCapabilityChange

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._endpoint: EndpointConfig | None = None
        self._updating = False
        self._probing: set[str] = set()
        self._saving: set[str] = set()
        self._manual_ids: list[str] = []
        self._rows: dict[str, ActualModelRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        title = QLabel("实际模型")
        title.setObjectName("actualModelsTitle")
        root.addWidget(title)
        self._endpoint_label = QLabel("保存站点后将在这里加载远端模型清单。")
        self._endpoint_label.setTextFormat(Qt.TextFormat.PlainText)
        self._endpoint_label.setWordWrap(True)
        self._endpoint_label.setProperty("hint", True)
        root.addWidget(self._endpoint_label)

        actions = QHBoxLayout()
        self._status = QLabel("等待站点配置")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setProperty("hint", True)
        actions.addWidget(self._status, 1)
        self._select_all = QPushButton("全部添加")
        self._select_all.setToolTip("将全部模型添加到模型列表并勾选；不会自动检测或覆盖已有能力")
        self._select_all.clicked.connect(self._add_all_models)
        self._select_all.setEnabled(False)
        actions.addWidget(self._select_all)
        self._refresh = QPushButton("重新拉取")
        self._refresh.clicked.connect(self._request_discovery)
        self._refresh.setEnabled(False)
        actions.addWidget(self._refresh)
        root.addLayout(actions)

        manual_row = QHBoxLayout()
        manual_row.setContentsMargins(0, 4, 0, 4)
        manual_row.setSpacing(8)
        self._manual_id = QLineEdit()
        self._manual_id.setPlaceholderText("列表不可用或缺少模型？输入远端模型 ID")
        self._manual_id.setClearButtonEnabled(True)
        self._manual_id.returnPressed.connect(self._add_manual_model)
        manual_row.addWidget(self._manual_id, 1)
        self._manual_add = QPushButton("添加到模型列表")
        self._manual_add.setToolTip("仅添加模型，不自动检测；已有模型不会重复添加或覆盖能力")
        self._manual_add.setEnabled(False)
        self._manual_id.textChanged.connect(self._update_manual_buttons)
        self._manual_add.clicked.connect(self._add_manual_model)
        manual_row.addWidget(self._manual_add)
        self._run = QPushButton("批量检测")
        self._run.setProperty("accent", True)
        self._run.setToolTip("检测选中的待测模型并更新能力声明；已通过的模型可单独重新检测")
        self._run.clicked.connect(self._probe_checked_models)
        self._run.setEnabled(False)
        manual_row.addWidget(self._run)
        root.addLayout(manual_row)

        self._models = QScrollArea()
        self._models.setObjectName("actualModelsScroll")
        self._models.setWidgetResizable(True)
        self._models.setFrameShape(QFrame.Shape.NoFrame)
        self._models.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("actualModelsContent")
        self._model_header = _ModelListHeader(content)
        self._model_layout = _ModelRowsLayout(content, self._model_header)
        self._model_layout.setContentsMargins(0, 0, 8, 0)
        self._model_layout.setSpacing(6)
        self._model_layout.addWidget(self._model_header)
        self._empty = QLabel("暂无模型；可拉取清单或手动输入模型 ID。")
        self._empty.setWordWrap(True)
        self._empty.setProperty("hint", True)
        self._model_layout.addWidget(self._empty)
        self._model_layout.addStretch(1)
        self._models.setWidget(content)
        root.addWidget(self._models, 1)

        note = QLabel(
            "行首复选框仅选择待检测模型；流式、思考、工具可直接勾选，修改后自动保存。"
            "半选表示尚未确认。主动检测会更新能力声明，检测完成后仍可手动调整。"
        )
        note.setWordWrap(True)
        note.setProperty("hint", True)
        root.addWidget(note)

    @property
    def endpoint_id(self) -> str | None:
        return self._endpoint.id if self._endpoint else None

    def activate_endpoint(
        self, endpoint: EndpointConfig | None, *, discover: bool = True,
        actual_models: tuple[ActualModel, ...] = (),
    ) -> None:
        self._endpoint = endpoint
        self._endpoint_label.setText(
            f"{endpoint.name} · {endpoint.base_url} · {endpoint.api.value}"
            if endpoint is not None else "保存站点后将在这里加载远端模型清单。"
        )
        self._refresh.setEnabled(endpoint is not None)
        self._manual_id.clear()
        self._update_manual_buttons("")
        self._manual_ids.clear()
        self._probing.clear()
        self._saving.clear()
        for row in self._rows.values():
            self._model_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows.clear()
        self.apply_saved_models(actual_models)
        self._update_actions()
        self._status.setText("正在拉取模型清单…" if discover else "等待模型清单")
        if discover and endpoint is not None:
            self.discovery_requested.emit(endpoint)

    def _new_row(
        self, model_id: str, name: str, *, selected: bool = False,
    ) -> ActualModelRow:
        row = ActualModelRow(model_id, name, self._models.widget())
        row.selected.setChecked(selected)
        row.selected.toggled.connect(self._on_selection_changed)
        row.probe_requested.connect(partial(self._probe_row, model_id))
        row.capability_changed.connect(partial(self._save_capability, model_id))
        self._rows[model_id] = row
        self._model_layout.insertWidget(self._model_layout.count() - 1, row)
        return row

    def apply_saved_models(self, models: tuple[ActualModel, ...]) -> None:
        for actual in models:
            if actual.endpoint_id != self.endpoint_id:
                continue
            # 本地已保存的模型默认勾选；刷新已有行时保留用户当前的选择。
            row = self._item(actual.model_id) or self._new_row(
                actual.model_id, actual.display_name, selected=True,
            )
            row.saved = True
            if (
                actual.model_id not in self._probing and actual.model_id not in self._saving
                and row.actual != actual
            ):
                row.apply_actual(actual)
        self._update_actions()

    def apply_discovery(self, endpoint_id: str, result: DiscoveryResult) -> None:
        if self.endpoint_id != endpoint_id:
            return
        existing = self._rows.copy()
        ordered: dict[str, ActualModelRow] = {}
        for model in result.models:
            if model.id in ordered:
                continue
            row = existing.pop(model.id, None) or self._new_row(model.id, model.name)
            if model.id not in self._manual_ids:
                row.set_name(model.name)
            ordered[model.id] = row
        for model_id, row in existing.items():
            if (
                not result.models or row.saved or model_id in self._manual_ids
                or model_id in self._probing or model_id in self._saving
            ):
                ordered[model_id] = row
            else:
                self._model_layout.removeWidget(row)
                row.hide()
                row.deleteLater()
        self._rows = ordered
        self._update_actions()
        self._status.setText(
            result.detail + ("；可手动输入模型 ID 添加到模型列表" if not result.models else "")
        )

    def _save_capability(
        self, model_id: str, capability: ModelCapability, supported: bool,
    ) -> None:
        row = self._item(model_id)
        if (
            row is None or self._endpoint is None
            or model_id in self._probing or model_id in self._saving
        ):
            return
        self._saving.add(model_id)
        row.set_busy(True)
        row.set_status("正在保存能力…")
        self._update_actions()
        self.capability_save_requested.emit(ModelCapabilityChange(
            self._endpoint, model_id, row.display_name, capability, supported,
        ))

    def apply_capability_save(self, change: ModelCapabilityChange, actual: ActualModel) -> None:
        if self._endpoint != change.endpoint:
            return
        self._saving.discard(change.model_id)
        row = self._item(change.model_id)
        if row is None:
            return
        row.apply_actual(actual)
        row.probe_succeeded = False
        row.set_capability(
            change.capability, change.supported,
            "手动启用" if change.supported else "手动禁用",
            "success" if change.supported else "muted",
        )
        row.set_status("能力已保存（手动设置，非检测结果）")
        row.set_busy(change.model_id in self._probing)
        self._status.setText(f"{change.model_id}：能力已保存")
        self._update_actions()

    def show_capability_save_error(self, change: ModelCapabilityChange, detail: str) -> None:
        if self._endpoint != change.endpoint:
            return
        self._saving.discard(change.model_id)
        row = self._item(change.model_id)
        if row is not None:
            row.restore_capabilities()
            row.set_busy(change.model_id in self._probing)
            row.set_status(f"保存失败，已恢复原值：{detail}")
        self._status.setText(f"能力保存失败：{detail}")
        self._update_actions()

    def _update_manual_buttons(self, text: str) -> None:
        enabled = self._endpoint is not None and bool(text.strip())
        self._manual_add.setEnabled(enabled)

    def _manual_item(self) -> ActualModelRow | None:
        model_id = self._manual_id.text().strip()
        if not model_id:
            self._status.setText("请输入远端模型 ID")
            return None
        if any(char in model_id for char in ("\r", "\n")):
            self._status.setText("模型 ID 不能包含换行符")
            return None
        if model_id not in self._manual_ids:
            self._manual_ids.append(model_id)
        row = self._item(model_id) or self._new_row(model_id, model_id)
        row.selected.setChecked(True)
        self._update_actions()
        return row

    def _add_manual_model(self) -> None:
        if self._endpoint is None or (row := self._manual_item()) is None:
            return
        self._save_model(row)

    def _save_model(self, row: ActualModelRow) -> None:
        if self._endpoint is None:
            return
        if row.model_id in self._probing or row.model_id in self._saving:
            self._status.setText("该模型正在检测或保存，请等待完成")
            return
        if row.saved:
            self._status.setText("该模型已在模型列表中；可直接修改能力或检测")
            if self._manual_id.text().strip() == row.model_id:
                self._manual_id.clear()
            return
        self.manual_save_requested.emit(
            ModelProbeTask(self._endpoint, row.model_id, row.display_name)
        )

    def apply_manual_save(self, endpoint_id: str, model_id: str) -> None:
        if self.endpoint_id != endpoint_id or (row := self._item(model_id)) is None:
            return
        row.saved = True
        row.set_status("已保存（未验证）；可直接设置能力")
        if self._manual_id.text().strip() == model_id:
            self._manual_id.clear()
        self._status.setText(f"{model_id}：已添加到模型列表，能力未重新验证")
        self._update_actions()

    def mark_probe_started(self, model_id: str) -> None:
        if model_id in self._probing:
            return
        row = self._item(model_id) or self._new_row(model_id, model_id)
        self._probing.add(model_id)
        row.set_busy(True)
        row.set_status("检测中…当前能力声明暂不变")
        self._update_actions()

    def apply_probe_result(
        self, endpoint_id: str, result: ModelProbeResult, actual: ActualModel | None = None,
    ) -> None:
        if self.endpoint_id != endpoint_id or (row := self._item(result.model_id)) is None:
            return
        self._probing.discard(result.model_id)
        if actual is not None:
            row.apply_actual(actual)
        row.set_busy(False)
        row.probe_succeeded = result.stream.alive
        row.saved = row.saved or result.stream.alive
        row.set_capability(
            "supports_streaming", result.stream.alive,
            "通过" if result.stream.alive else "失败",
            "success" if result.stream.alive else "danger", result.stream.detail,
        )
        if result.thinking.supported:
            text = "、".join(result.thinking.levels) if result.thinking.levels else (
                result.thinking.level or "支持（预算模式）"
            )
            if result.thinking_level_locked:
                text += "（固定）"
            row.set_capability("supports_thinking", True, text, "success", result.thinking.detail)
        else:
            row.set_capability(
                "supports_thinking", None if result.thinking.inconclusive else False,
                "未确认" if result.thinking.inconclusive else "不支持",
                "warning" if result.thinking.inconclusive else "danger", result.thinking.detail,
            )
        row.set_capability(
            "supports_tools", True if result.tools.supported else (
                None if result.tools.inconclusive else False
            ), "支持" if result.tools.supported else (
                "未确认" if result.tools.inconclusive else "不支持"
            ), "success" if result.tools.supported else (
                "warning" if result.tools.inconclusive else "danger"
            ), result.tools.detail,
        )
        if self._manual_id.text().strip() == result.model_id:
            self._manual_id.clear()
        row.detect.setText("重新检测")
        row.set_status("检测完成并已保存；可直接调整能力" if result.alive else "检测失败")
        self._status.setText(f"{result.model_id}：{row.status_text}")
        self._update_actions()

    def show_probe_error(self, endpoint_id: str, model_id: str, detail: str) -> None:
        if self.endpoint_id != endpoint_id:
            return
        self._probing.discard(model_id)
        row = self._item(model_id)
        if row is not None:
            row.probe_succeeded = False
            row.set_busy(model_id in self._saving)
            row.set_status(f"检测失败：{detail}；已有能力声明未修改")
            row.detect.setText("重新检测")
        self._update_actions()
        self._status.setText(detail)

    def _request_discovery(self) -> None:
        if self._endpoint is not None:
            self._status.setText("正在拉取模型清单…")
            self.discovery_requested.emit(self._endpoint)

    def _on_selection_changed(self) -> None:
        if not self._updating:
            self._update_actions()

    def _add_all_models(self) -> None:
        if self._endpoint is None:
            return
        rows = list(self._rows.values())
        self._updating = True
        try:
            for row in rows:
                row.selected.setChecked(True)
            self._status.setText(f"已选择 {len(rows)} 个模型，按「批量检测」执行检测")
            for row in rows:
                if self._can_add_model(row):
                    self._save_model(row)
        finally:
            self._updating = False
        self._update_actions()

    def _can_add_model(self, row: ActualModelRow) -> bool:
        return (
            not row.saved and row.model_id not in self._probing
            and row.model_id not in self._saving
        )

    def _probe_checked_models(self) -> None:
        self._updating = True
        try:
            started = sum(self._start_probe(row) for row in self._pending_probe_items())
        finally:
            self._updating = False
        self._update_actions()
        if started:
            self._status.setText(f"正在检测 {started} 个模型…")

    def _pending_probe_items(self) -> list[ActualModelRow]:
        return [
            row for row in self._rows.values()
            if row.selected.isChecked() and row.model_id not in self._probing
            and row.model_id not in self._saving and not row.probe_succeeded
        ]

    def _sort_rows(self) -> None:
        # 只调整显示位置，保留目录顺序作为两组内部的稳定顺序。
        rows = sorted(self._rows.values(), key=lambda row: not row.selected.isChecked())
        for index, row in enumerate(rows, start=2):
            item = self._model_layout.itemAt(index)
            if item is None or item.widget() is not row:
                self._model_layout.insertWidget(index, row)

    def _update_actions(self) -> None:
        if self._updating:
            return
        self._sort_rows()
        self._empty.setVisible(not self._rows)
        self._select_all.setEnabled(
            self._endpoint is not None
            and any(
                not row.selected.isChecked() or self._can_add_model(row)
                for row in self._rows.values()
            )
        )
        pending = len(self._pending_probe_items()) if self._endpoint is not None else 0
        self._run.setEnabled(pending > 0)
        self._run.setText(f"批量检测（{pending}）" if pending else "批量检测")

    def _probe_row(self, model_id: str) -> None:
        if (row := self._item(model_id)) is not None:
            self._start_probe(row)
            self._update_actions()

    def _start_probe(self, row: ActualModelRow) -> bool:
        if (
            self._endpoint is None or row.model_id in self._probing
            or row.model_id in self._saving
        ):
            return False
        self.mark_probe_started(row.model_id)
        self._status.setText(f"{row.model_id}：正在检测…")
        self.probe_requested.emit(ModelProbeTask(self._endpoint, row.model_id, row.display_name))
        return True

    def _items(self) -> list[ActualModelRow]:
        return list(self._rows.values())

    def _item(self, model_id: str) -> ActualModelRow | None:
        return self._rows.get(model_id)
