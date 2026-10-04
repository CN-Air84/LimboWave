"""Complete appearance editor backed by immutable theme drafts."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.appearance_theme_service import AppearanceThemeService
from limbowave.application.services.preferences_service import FONT_SCALES, PreferencesService
from limbowave.domain.appearance import (
    BACKGROUND_FIT_MODES,
    BACKGROUND_POSITIONS,
    BUILTIN_BY_ID,
    CONTROL_CATEGORIES,
    DEFAULT_THEME_ID,
    AppearanceTheme,
    BackgroundSettings,
    MaterialSettings,
    TextGlowSettings,
    replace_controls,
)
from limbowave.ui.animated_stack import AnimatedPageStack, AnimatedTabBar
from limbowave.ui.font_registry import family_for_file

_COLOR_FIELDS = (
    ("background", "窗口底色"),
    ("card", "卡片底色"),
    ("component", "组件底色"),
    ("accent", "强调色"),
    ("text", "正文色"),
    ("info", "信息色"),
    ("warning", "警告色"),
    ("error", "错误色"),
    ("tab_indicator", "页签指示色"),
)
_CONTROL_LABELS = {
    "buttons": "按钮",
    "text_inputs": "文本输入",
    "selections": "选择控件",
    "item_views": "列表与表格",
    "scrollbars": "滚动条",
}
_FIT_LABELS = {"cover": "填充裁剪", "contain": "完整适应", "stretch": "拉伸", "tile": "平铺"}
_POSITION_LABELS = {
    "top-left": "左上",
    "top": "上中",
    "top-right": "右上",
    "left": "左中",
    "center": "居中",
    "right": "右中",
    "bottom-left": "左下",
    "bottom": "下中",
    "bottom-right": "右下",
}


class _ColorButton(QPushButton):
    color_changed = Signal(str)

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.clicked.connect(self._choose)
        self.set_color(color, emit=False)

    @property
    def color(self) -> str:
        return self._color

    def set_color(self, color: str, *, emit: bool = True) -> None:
        self._color = QColor(color).name(QColor.NameFormat.HexRgb).upper()
        foreground = "#FFFFFF" if QColor(self._color).lightness() < 145 else "#111318"
        self.setText(self._color)
        self.setStyleSheet(
            f"QPushButton {{ background: {self._color}; color: {foreground}; "
            "border: 1px solid #7F7F7F; padding: 6px 12px; }"
        )
        if emit:
            self.color_changed.emit(self._color)

    def _choose(self) -> None:
        selected = QColorDialog.getColor(QColor(self._color), self, "选择颜色")
        if selected.isValid():
            self.set_color(selected.name(QColor.NameFormat.HexRgb))


class AppearanceEditor(QWidget):
    """Theme draft editor with explicit save semantics and immediate preview."""

    appearance_changed = Signal(object)

    # 应用一次预览要重设全局样式表、重渲染会话，会卡住事件循环一小段。先让控件自己的
    # 点击反馈（复选框勾选过渡约 150–190ms）播完再应用；连续调数值时也顺带合并成一次。
    PREVIEW_DELAY_MS = 200

    def __init__(
        self,
        preferences: PreferencesService,
        theme_service: AppearanceThemeService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._preferences = preferences
        current = preferences.load()
        self.theme_service = theme_service or AppearanceThemeService(
            preferences.path.parent / "themes",
            legacy_theme=current.theme,
            legacy_background_image=current.background_image,
            legacy_blur_radius=current.blur_radius,
        )
        self._baseline = self.theme_service.active_theme
        self._draft = self._baseline
        self._loading = False
        self._font_file = current.font_file
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(self.PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._emit_appearance)
        self._build()
        self.reload()

    @property
    def draft(self) -> AppearanceTheme:
        return self._draft

    @property
    def dirty(self) -> bool:
        return self._draft != self._baseline

    def _add_page(self, title: str, hint: str) -> QFormLayout:
        page = QWidget()
        page.setObjectName("appearancePage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(18, 14, 18, 18)
        page_layout.setSpacing(10)

        heading = QLabel(title)
        heading.setObjectName("appearancePageTitle")
        page_layout.addWidget(heading)
        description = QLabel(hint)
        description.setObjectName("appearancePageHint")
        description.setWordWrap(True)
        page_layout.addWidget(description)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(11)
        page_layout.addLayout(form)
        page_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("appearancePageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self._subtabs.addTab(title)
        self._stack.add_page(scroll)
        return form

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        tab_host = QWidget()
        tab_host.setObjectName("appearanceSubtabHost")
        tab_layout = QHBoxLayout(tab_host)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)
        tab_layout.addStretch(1)
        self._subtabs = AnimatedTabBar()
        self._subtabs.setObjectName("appearanceSubtabs")
        self._subtabs.setShape(AnimatedTabBar.Shape.RoundedNorth)
        self._subtabs.setExpanding(False)
        self._subtabs.setElideMode(Qt.TextElideMode.ElideNone)
        self._subtabs.setDocumentMode(True)
        self._subtabs.setDrawBase(False)
        self._subtabs.setUsesScrollButtons(True)
        tab_layout.addWidget(self._subtabs, 5)
        tab_layout.addStretch(1)
        root.addWidget(tab_host)

        self._stack = AnimatedPageStack(orientation=Qt.Orientation.Horizontal)
        self._stack.setObjectName("appearanceSubtabStack")
        root.addWidget(self._stack, 1)

        form = self._add_page(
            "主题",
            "内置主题只读；修改会形成实时草稿，可另存为自定义主题。",
        )
        self.theme_combo = QComboBox()
        self.theme_combo.currentIndexChanged.connect(self._select_theme)
        form.addRow("当前主题", self.theme_combo)
        self.theme_context_label = QLabel()
        self.theme_context_label.setWordWrap(True)
        form.addRow("草稿状态", self.theme_context_label)
        actions = QHBoxLayout()
        self.theme_new_button = QPushButton("新建…")
        self.theme_save_button = QPushButton("保存")
        self.theme_save_as_button = QPushButton("另存为…")
        self.theme_rename_button = QPushButton("重命名…")
        self.theme_delete_button = QPushButton("删除")
        self.theme_reset_button = QPushButton("放弃修改")
        for button in (
            self.theme_new_button,
            self.theme_save_button,
            self.theme_save_as_button,
            self.theme_rename_button,
            self.theme_delete_button,
            self.theme_reset_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        self.theme_new_button.clicked.connect(self._new_theme)
        self.theme_save_button.clicked.connect(self._save)
        self.theme_save_as_button.clicked.connect(self._save_as)
        self.theme_rename_button.clicked.connect(self._rename)
        self.theme_delete_button.clicked.connect(self._delete)
        self.theme_reset_button.clicked.connect(self._reset_draft)
        form.addRow("主题操作", actions)

        form = self._add_page(
            "颜色",
            "主题保存九个语义颜色；边框、悬停态和次要文字由引擎统一派生。",
        )
        self.color_buttons: dict[str, _ColorButton] = {}
        for key, label in _COLOR_FIELDS:
            button = _ColorButton("#000000")
            button.color_changed.connect(lambda value, name=key: self._set_color(name, value))
            self.color_buttons[key] = button
            setattr(self, f"{key}_color", button)
            form.addRow(label, button)

        form = self._add_page(
            "背景",
            "图片会复制到主题资源目录；缩放、位置、透明度与遮罩均随主题保存。",
        )
        resource_row = QHBoxLayout()
        self.background_resource_label = QLineEdit()
        self.background_resource_label.setReadOnly(True)
        import_button = QPushButton("导入…")
        remove_button = QPushButton("移除")
        cleanup_button = QPushButton("清理未引用资源…")
        import_button.clicked.connect(self._import_background)
        remove_button.clicked.connect(self._remove_background)
        cleanup_button.clicked.connect(self._cleanup_backgrounds)
        resource_row.addWidget(self.background_resource_label, 1)
        resource_row.addWidget(import_button)
        resource_row.addWidget(remove_button)
        resource_row.addWidget(cleanup_button)
        form.addRow("托管资源", resource_row)
        self.background_fit_combo = QComboBox()
        for value in BACKGROUND_FIT_MODES:
            self.background_fit_combo.addItem(_FIT_LABELS[value], value)
        self.background_fit_combo.currentIndexChanged.connect(self._background_changed)
        form.addRow("缩放方式", self.background_fit_combo)
        self.background_position_combo = QComboBox()
        for value in BACKGROUND_POSITIONS:
            self.background_position_combo.addItem(_POSITION_LABELS[value], value)
        self.background_position_combo.currentIndexChanged.connect(self._background_changed)
        form.addRow("九宫格位置", self.background_position_combo)
        self.background_image_opacity = self._percent_spin(self._background_changed)
        form.addRow("图片透明度", self.background_image_opacity)
        self.background_mask_color = _ColorButton("#000000")
        self.background_mask_color.color_changed.connect(self._background_changed)
        form.addRow("遮罩颜色", self.background_mask_color)
        self.background_mask_opacity = self._percent_spin(self._background_changed)
        form.addRow("遮罩透明度", self.background_mask_opacity)

        form = self._add_page(
            "材质",
            "内容区与左侧栏共享同一背景坐标，但拥有独立的不透明度和模糊半径。",
        )
        self.content_enabled = QCheckBox("启用内容区磨砂")
        self.content_enabled.toggled.connect(self._materials_changed)
        form.addRow("内容区", self.content_enabled)
        self.content_opacity = self._percent_spin(self._materials_changed)
        form.addRow("内容底板不透明度", self.content_opacity)
        self.content_blur_radius = self._radius_spin(self._materials_changed)
        form.addRow("内容模糊半径", self.content_blur_radius)
        self.cards_enabled = QCheckBox("卡片使用内容区共享材质")
        self.cards_enabled.toggled.connect(self._materials_changed)
        form.addRow("卡片", self.cards_enabled)
        self.sidebar_enabled = QCheckBox("启用左侧栏独立磨砂")
        self.sidebar_enabled.toggled.connect(self._materials_changed)
        form.addRow("左侧栏", self.sidebar_enabled)
        self.sidebar_opacity = self._percent_spin(self._materials_changed)
        form.addRow("左栏底板不透明度", self.sidebar_opacity)
        self.sidebar_blur_radius = self._radius_spin(self._materials_changed)
        form.addRow("左栏模糊半径", self.sidebar_blur_radius)
        self.tab_indicator_opacity = self._percent_spin(self._materials_changed)
        form.addRow("页签指示不透明度", self.tab_indicator_opacity)

        form = self._add_page(
            "控件",
            "总开关只覆盖运行状态，不会清空按钮、输入框等分类的原有选择。",
        )
        self.controls_master_enabled = QCheckBox("一键启用全部控件效果")
        self.controls_master_enabled.toggled.connect(self._materials_changed)
        form.addRow("总开关", self.controls_master_enabled)
        self.control_checks: dict[str, QCheckBox] = {}
        for category in CONTROL_CATEGORIES:
            check = QCheckBox("启用")
            check.toggled.connect(self._materials_changed)
            self.control_checks[category] = check
            form.addRow(_CONTROL_LABELS[category], check)

        form = self._add_page(
            "光晕与交互",
            "字形光晕按字号插值；悬停停用只改变目标控件底板，不降低文字不透明度。",
        )
        self.text_glow_enabled = QCheckBox("暗色文字启用白色字形光晕")
        self.text_glow_enabled.toggled.connect(self._glow_changed)
        form.addRow("文字光晕", self.text_glow_enabled)
        self.glow_minimum_intensity = self._percent_spin(self._glow_changed)
        self.glow_maximum_intensity = self._percent_spin(self._glow_changed)
        self.glow_minimum_radius = self._radius_spin(self._glow_changed)
        self.glow_maximum_radius = self._radius_spin(self._glow_changed)
        form.addRow("最小字号强度", self.glow_minimum_intensity)
        form.addRow("最大字号强度", self.glow_maximum_intensity)
        form.addRow("最小字号半径", self.glow_minimum_radius)
        form.addRow("最大字号半径", self.glow_maximum_radius)
        self.hover_suspend_enabled = QCheckBox("悬停时临时恢复为实体表面")
        self.hover_suspend_enabled.toggled.connect(self._materials_changed)
        form.addRow("悬停停用", self.hover_suspend_enabled)
        self.hover_enter_speed = QSpinBox()
        self.hover_restore_speed = QSpinBox()
        for spin in (self.hover_enter_speed, self.hover_restore_speed):
            spin.setRange(0, 60_000)
            spin.setSuffix(" ms")
            spin.valueChanged.connect(self._materials_changed)
        form.addRow("进入时长", self.hover_enter_speed)
        form.addRow("恢复时长", self.hover_restore_speed)

        form = self._add_page(
            "字体",
            "字体与字号是机器级偏好；导入字体会复制到 LimboWave 数据目录。",
        )
        self._font_scale = QComboBox()
        for scale in FONT_SCALES:
            self._font_scale.addItem(f"{int(scale * 100)}%", scale)
        self._font_scale.currentIndexChanged.connect(self._font_preferences_changed)
        form.addRow("字号", self._font_scale)
        self._family = QComboBox()
        self._family.addItem("系统默认", "")
        for family in QFontDatabase.families():
            self._family.addItem(family, family)
        self._family.currentIndexChanged.connect(self._system_font_changed)
        form.addRow("界面字体", self._family)
        font_button = QPushButton("导入 TTF/OTF/TTC…")
        font_button.clicked.connect(self._import_font)
        form.addRow("自定义字体", font_button)

        self._subtabs.refresh_width_constraints(max(0, self.width() - 8))
        self._subtabs.currentChanged.connect(self._switch_subtab)
        self._subtabs.setCurrentIndex(0)
        self._subtabs.sync_indicator()
        self._sync_input_heights()

    def _switch_subtab(self, index: int) -> None:
        self._stack.set_index(index)

    def _sync_input_heights(self) -> None:
        controls: list[QWidget] = [
            *self.findChildren(QComboBox),
            *self.findChildren(QSpinBox),
            *self.findChildren(QDoubleSpinBox),
            self.background_resource_label,
        ]
        unique = list(dict.fromkeys(controls))
        for control in unique:
            control.setMinimumHeight(0)
        target = max((control.sizeHint().height() for control in unique), default=0)
        for control in unique:
            control.setMinimumHeight(target)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
            self, "_subtabs"
        ):
            QTimer.singleShot(0, self._sync_input_heights)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._subtabs.refresh_width_constraints(max(0, self.width() - 8))

    def _percent_spin(self, slot: object) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setSuffix(" %")
        spin.valueChanged.connect(slot)
        return spin

    def _radius_spin(self, slot: object) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 64.0)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setSuffix(" px")
        spin.valueChanged.connect(slot)
        return spin

    def reload(self) -> None:
        self._baseline = self.theme_service.active_theme
        self._draft = self._baseline
        self._reload_theme_list()
        self._load_draft()
        self._load_font_preferences()

    def _reload_theme_list(self) -> None:
        self._loading = True
        self.theme_combo.clear()
        for definition in self.theme_service.themes():
            suffix = "  🔒" if definition.readonly else ""
            self.theme_combo.addItem(definition.name + suffix, definition.id)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self._baseline.id))
        self._loading = False

    def _load_draft(self) -> None:
        self._loading = True
        colors = self._draft.colors
        for key, _label in _COLOR_FIELDS:
            self.color_buttons[key].set_color(getattr(colors, key), emit=False)
        background = self._draft.background
        self.background_resource_label.setText(background.asset or "未设置")
        self.background_fit_combo.setCurrentIndex(
            self.background_fit_combo.findData(background.fit_mode)
        )
        self.background_position_combo.setCurrentIndex(
            self.background_position_combo.findData(background.position)
        )
        self.background_image_opacity.setValue(round(background.image_opacity * 100))
        self.background_mask_color.set_color(background.mask_color, emit=False)
        self.background_mask_opacity.setValue(round(background.mask_opacity * 100))
        materials = self._draft.materials
        self.content_enabled.setChecked(materials.content_enabled)
        self.content_opacity.setValue(round(materials.content_opacity * 100))
        self.content_blur_radius.setValue(materials.content_blur_radius)
        self.cards_enabled.setChecked(materials.cards_enabled)
        self.controls_master_enabled.setChecked(materials.controls_master_enabled)
        for category, check in self.control_checks.items():
            check.setChecked(bool(getattr(materials.controls, category)))
        self.sidebar_enabled.setChecked(materials.sidebar_enabled)
        self.sidebar_opacity.setValue(round(materials.sidebar_opacity * 100))
        self.sidebar_blur_radius.setValue(materials.sidebar_blur_radius)
        self.tab_indicator_opacity.setValue(round(materials.tab_indicator_opacity * 100))
        self.hover_suspend_enabled.setChecked(materials.hover_suspend_enabled)
        self.hover_enter_speed.setValue(materials.hover_enter_ms)
        self.hover_restore_speed.setValue(materials.hover_restore_ms)
        glow = self._draft.text_glow
        self.text_glow_enabled.setChecked(glow.enabled)
        self.glow_minimum_intensity.setValue(round(glow.minimum_intensity * 100))
        self.glow_maximum_intensity.setValue(round(glow.maximum_intensity * 100))
        self.glow_minimum_radius.setValue(glow.minimum_radius)
        self.glow_maximum_radius.setValue(glow.maximum_radius)
        self._loading = False
        self._update_state()

    def _load_font_preferences(self) -> None:
        current = self._preferences.load()
        self._loading = True
        self._font_file = current.font_file
        for family in family_for_file(current.font_file):
            if self._family.findData(family) < 0:
                self._family.addItem(family, family)
        self._family.setCurrentIndex(max(0, self._family.findData(current.font_family)))
        index = self._font_scale.findData(current.font_scale)
        self._font_scale.setCurrentIndex(index if index >= 0 else 1)
        self._loading = False

    def _project_legacy_theme(self, definition: AppearanceTheme) -> None:
        current = self._preferences.load()
        mode = "light" if QColor(definition.colors.background).lightness() >= 128 else "dark"
        if current.theme != mode:
            self._preferences.save(replace(current, theme=mode))

    def _update_state(self) -> None:
        kind = "内置只读预设" if self._baseline.readonly else "自定义主题"
        dirty = "有未保存更改" if self.dirty else "已保存"
        self.theme_context_label.setText(f"{kind} · {dirty}")
        self.theme_save_button.setEnabled(not self._baseline.readonly and self.dirty)
        self.theme_save_as_button.setEnabled(True)
        self.theme_rename_button.setEnabled(not self._baseline.readonly)
        self.theme_delete_button.setEnabled(not self._baseline.readonly)
        self.theme_reset_button.setEnabled(self.dirty)

    def _preview(self, draft: AppearanceTheme) -> None:
        if self._loading:
            return
        self._draft = draft
        self._update_state()
        self._preview_timer.start()

    def _emit_appearance(self) -> None:
        """立即外发当前草稿；同时作废尚未触发的延迟预览，避免旧草稿随后覆盖。"""
        self._preview_timer.stop()
        self.appearance_changed.emit(self._draft)

    def _set_color(self, key: str, value: str) -> None:
        self._preview(replace(self._draft, colors=replace(self._draft.colors, **{key: value})))

    def _background_changed(self, _value: object = None) -> None:
        if self._loading:
            return
        background = BackgroundSettings(
            asset=self._draft.background.asset,
            fit_mode=str(self.background_fit_combo.currentData()),
            position=str(self.background_position_combo.currentData()),
            image_opacity=self.background_image_opacity.value() / 100,
            mask_color=self.background_mask_color.color,
            mask_opacity=self.background_mask_opacity.value() / 100,
        )
        self._preview(replace(self._draft, background=background))

    def _materials_changed(self, _value: object = None) -> None:
        if self._loading:
            return
        materials = MaterialSettings(
            content_enabled=self.content_enabled.isChecked(),
            content_opacity=self.content_opacity.value() / 100,
            content_blur_radius=self.content_blur_radius.value(),
            cards_enabled=self.cards_enabled.isChecked(),
            controls_master_enabled=self.controls_master_enabled.isChecked(),
            controls=self._draft.materials.controls,
            sidebar_enabled=self.sidebar_enabled.isChecked(),
            sidebar_opacity=self.sidebar_opacity.value() / 100,
            sidebar_blur_radius=self.sidebar_blur_radius.value(),
            tab_indicator_opacity=self.tab_indicator_opacity.value() / 100,
            hover_suspend_enabled=self.hover_suspend_enabled.isChecked(),
            hover_enter_ms=self.hover_enter_speed.value(),
            hover_restore_ms=self.hover_restore_speed.value(),
        )
        for category, check in self.control_checks.items():
            materials = replace_controls(materials, **{category: check.isChecked()})
        self._preview(replace(self._draft, materials=materials))

    def _glow_changed(self, _value: object = None) -> None:
        if self._loading:
            return
        minimum_intensity = self.glow_minimum_intensity.value() / 100
        maximum_intensity = max(minimum_intensity, self.glow_maximum_intensity.value() / 100)
        minimum_radius = self.glow_minimum_radius.value()
        maximum_radius = max(minimum_radius, self.glow_maximum_radius.value())
        glow = TextGlowSettings(
            self.text_glow_enabled.isChecked(),
            minimum_intensity,
            maximum_intensity,
            minimum_radius,
            maximum_radius,
        )
        self._preview(replace(self._draft, text_glow=glow))

    def _select_theme(self, index: int) -> None:
        if self._loading or index < 0:
            return
        theme_id = str(self.theme_combo.itemData(index))
        if theme_id == self._baseline.id:
            return
        if self.dirty and not self._resolve_dirty():
            self._loading = True
            self.theme_combo.setCurrentIndex(self.theme_combo.findData(self._baseline.id))
            self._loading = False
            return
        self._baseline = self.theme_service.activate(theme_id)
        self._project_legacy_theme(self._baseline)
        self._draft = self._baseline
        self._load_draft()
        self._emit_appearance()

    def _resolve_dirty(self) -> bool:
        choice = QMessageBox.question(
            self,
            "未保存的主题",
            "当前主题有未保存修改。是否保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            if self._baseline.readonly:
                return self._save_as()
            return self._save()
        self._draft = self._baseline
        self._load_draft()
        self._emit_appearance()
        return True

    def _new_theme(self) -> None:
        if self.dirty and not self._resolve_dirty():
            return
        name, accepted = QInputDialog.getText(self, "新建主题", "主题名称")
        if not accepted:
            return
        try:
            self._baseline = self.theme_service.create(
                name, BUILTIN_BY_ID[DEFAULT_THEME_ID], activate=True
            )
            self._project_legacy_theme(self._baseline)
            self._draft = self._baseline
            self._reload_theme_list()
            self._load_draft()
            self._emit_appearance()
        except Exception as exc:
            QMessageBox.warning(self, "新建主题失败", str(exc))

    def _reset_draft(self) -> None:
        self._draft = self._baseline
        self._load_draft()
        self._emit_appearance()

    def _save(self) -> bool:
        try:
            self._baseline = self.theme_service.save(self._draft)
            self._project_legacy_theme(self._baseline)
            self._draft = self._baseline
            self._update_state()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "保存主题失败", str(exc))
            return False

    def _save_as(self) -> bool:
        name, accepted = QInputDialog.getText(self, "另存主题", "主题名称")
        if not accepted:
            return False
        try:
            self._baseline = self.theme_service.create(name, self._draft, activate=True)
            self._project_legacy_theme(self._baseline)
            self._draft = self._baseline
            self._reload_theme_list()
            self._load_draft()
            self._emit_appearance()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "另存主题失败", str(exc))
            return False

    def _rename(self) -> None:
        name, accepted = QInputDialog.getText(
            self, "重命名主题", "主题名称", text=self._baseline.name
        )
        if not accepted:
            return
        try:
            self._baseline = self.theme_service.rename(self._baseline.id, name)
            self._draft = replace(self._draft, name=self._baseline.name)
            self._reload_theme_list()
            self._update_state()
        except Exception as exc:
            QMessageBox.warning(self, "重命名失败", str(exc))

    def _delete(self) -> None:
        if (
            QMessageBox.question(self, "删除主题", f"确定删除“{self._baseline.name}”？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self._baseline = self.theme_service.delete(self._baseline.id)
            self._project_legacy_theme(self._baseline)
            self._draft = self._baseline
            self._reload_theme_list()
            self._load_draft()
            self._emit_appearance()
        except Exception as exc:
            QMessageBox.warning(self, "删除主题失败", str(exc))

    def _import_background(self) -> None:
        file, _ = QFileDialog.getOpenFileName(
            self, "导入背景", "", "图片 (*.png *.jpg *.jpeg *.bmp)"
        )
        if not file:
            return
        try:
            asset = self.theme_service.import_background(Path(file))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "背景不可用", str(exc))
            return
        self.background_resource_label.setText(asset)
        self._preview(replace(self._draft, background=replace(self._draft.background, asset=asset)))

    def _remove_background(self) -> None:
        self.background_resource_label.setText("未设置")
        self._preview(replace(self._draft, background=replace(self._draft.background, asset="")))

    def _cleanup_backgrounds(self) -> None:
        orphans = self.theme_service.orphan_assets()
        if not orphans:
            QMessageBox.information(self, "主题资源", "没有未引用的背景图片。")
            return
        total = sum(path.stat().st_size for path in orphans)
        answer = QMessageBox.question(
            self,
            "清理主题资源",
            f"删除 {len(orphans)} 个未引用背景（{total / 1024 / 1024:.1f} MiB）？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            deleted = self.theme_service.cleanup_orphan_assets()
            QMessageBox.information(self, "主题资源", f"已删除 {len(deleted)} 个文件。")

    def _system_font_changed(self, _index: int = -1) -> None:
        if self._loading:
            return
        self._font_file = ""
        self._font_preferences_changed()

    def _font_preferences_changed(self, _index: int = -1) -> None:
        if self._loading:
            return
        current = self._preferences.load()
        self._preferences.save(
            replace(
                current,
                font_scale=float(self._font_scale.currentData()),
                font_family=str(self._family.currentData() or ""),
                font_file=self._font_file,
                theme=(
                    "light" if QColor(self._draft.colors.background).lightness() >= 128 else "dark"
                ),
            )
        )
        self._emit_appearance()

    def _import_font(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "导入字体", "", "字体 (*.ttf *.otf *.ttc)")
        if not file:
            return
        source = Path(file)
        try:
            data = source.read_bytes()
            if len(data) > 16 * 1024 * 1024 or not (families := family_for_file(file)):
                raise ValueError("字体无效或超过 16 MiB")
            directory = self._preferences.path.parent / "fonts"
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / (sha256(data).hexdigest() + source.suffix.lower())
            if not destination.exists():
                pending = destination.with_suffix(destination.suffix + ".tmp")
                pending.write_bytes(data)
                pending.replace(destination)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "字体不可用", str(exc))
            return
        self._font_file = str(destination)
        family = families[0]
        if self._family.findData(family) < 0:
            self._family.addItem(family, family)
        self._loading = True
        self._family.setCurrentIndex(self._family.findData(family))
        self._loading = False
        self._font_preferences_changed()

    def resolve_pending_changes(self) -> bool:
        return not self.dirty or self._resolve_dirty()
