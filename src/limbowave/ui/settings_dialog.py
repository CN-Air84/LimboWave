"""站点与逻辑模型管理对话框（Task 2.1 / 2.2 / 2.3 的界面）。

设计计划 §四.1：界面按「逻辑模型 → 站点」组织。本对话框用两个页签呈现
两类实体，但模型页签里直接管理「这个模型绑定哪些站点」——绑定关系是
模型页的主角，不是站点页的从属。

架构约束：
- 对话框不直接碰仓库；每个用户动作经 ``SettingsService`` 落盘，
  密钥经 ``CredentialService``（密码框不回显，保存或切换端点后清空）。
- 校验失败（``ValueError``）弹错误框，配置保持不变——服务层保证不写半截配置。
- 路由预览是纯展示：调用 :class:`RoutingService`，不触发任何网络请求。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.appearance_theme_service import AppearanceThemeService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.preferences_service import PreferencesService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.param_rules import validate_rules
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.slug import unique_slug
from limbowave.infrastructure.crypto.recovery import describe_protection
from limbowave.infrastructure.crypto.vault import InvalidPassword, Vault
from limbowave.ui import theme
from limbowave.ui.about_page import AboutPage
from limbowave.ui.appearance_editor import AppearanceEditor
from limbowave.ui.logical_models_tab import LogicalModelsTab
from limbowave.ui.marquee_list import MarqueeListWidget

_PROTOCOLS = [p.value for p in ProviderProtocol]


class SettingsDialog(QDialog):
    """站点端点与逻辑模型的管理界面。

    ``appearance_changed``：外观（主题/字号）改动时外发，上层据此重设全局样式
    并重新渲染——已构造的组件把颜色拼进了内联样式，需要重建才更新。
    """

    appearance_changed = Signal(object)

    def __init__(
        self,
        settings: SettingsService,
        credentials: CredentialService,
        parent: QWidget | None = None,
        preferences: PreferencesService | None = None,
        vault: Vault | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._credentials = credentials
        self._preferences = preferences
        self._vault = vault
        self.setWindowTitle("站点与模型")
        self.resize(760, 520)

        root = QVBoxLayout(self)
        self.appearance_tab: _AppearanceTab | None = None
        self.security_tab: _SecurityTab | None = None

        tabs = QTabWidget()
        self._endpoints_tab = _EndpointsTab(settings, credentials, self)
        self._models_tab = _ModelsTab(settings, self)
        tabs.addTab(self._models_tab, "逻辑模型")
        tabs.addTab(self._endpoints_tab, "站点端点")
        self._endpoints_tab.configuration_changed.connect(self._models_tab.reload)
        if vault is not None:
            self.security_tab = _SecurityTab(vault, self)
            tabs.addTab(self.security_tab, "安全")
        if preferences is not None:
            self.appearance_tab = _AppearanceTab(preferences, self)
            self.appearance_tab.appearance_changed.connect(self.appearance_changed.emit)
            tabs.addTab(self.appearance_tab, "外观")
        self.about_tab = AboutPage(self)
        tabs.addTab(self.about_tab, "关于")
        root.addWidget(tabs, 1)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close_box.rejected.connect(self.reject)
        close_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        root.addWidget(close_box)

    def accept(self) -> None:
        if self.appearance_tab is not None and not self.appearance_tab.resolve_pending_changes():
            return
        super().accept()

    def reject(self) -> None:
        if self.appearance_tab is not None and not self.appearance_tab.resolve_pending_changes():
            return
        super().reject()

    def reload(self) -> None:
        """重新从服务层加载（配置可能被另一端改动）。"""
        self._endpoints_tab.reload()
        self._models_tab.reload()
        if self.security_tab is not None:
            self.security_tab.reload()
        if self.appearance_tab is not None:
            self.appearance_tab.reload()


def _show_error(parent: QWidget, exc: Exception) -> None:
    from limbowave.ui.floating import ask_alert

    ask_alert(parent, "无法保存", str(exc))


class _EndpointsTab(QWidget):
    configuration_changed = Signal()
    discovery_requested = Signal(object)  # EndpointConfig
    endpoint_saved = Signal(object)  # EndpointConfig
    endpoint_selecting = Signal(int)
    endpoint_selected = Signal(object)  # EndpointConfig | None

    """站点端点：列表 + 表单 + 密钥设置。"""

    def __init__(
        self,
        settings: SettingsService,
        credentials: CredentialService,
        parent: QWidget | None = None,
        preferences: PreferencesService | None = None,
        vault: Vault | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._credentials = credentials
        self._preferences = preferences
        self._vault = vault
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setSingleShot(True)
        self._discovery_timer.setInterval(450)
        self._discovery_timer.timeout.connect(self._request_discovery_if_ready)
        self._build()
        self.reload()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = self.sidebar = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._list = MarqueeListWidget()
        self._list.currentItemChanged.connect(self._on_select)
        left_layout.addWidget(self._list, 1)
        new_btn = QPushButton("新建端点")
        new_btn.clicked.connect(self._on_new)
        left_layout.addWidget(new_btn)
        splitter.addWidget(left)

        right = QWidget()
        form = QFormLayout(right)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(10)
        # 只让用户填显示名：站点 ID 与密钥引用都由它派生（中文转拼音），见 domain.slug
        self._name = QLineEdit()
        self._name.setPlaceholderText("如 中转站 A")
        self._name.textChanged.connect(self._refresh_identity_hint)
        self._name.textChanged.connect(self._schedule_discovery)
        self._identity_hint = QLabel("")
        self._identity_hint.setProperty("hint", True)
        self._base_url = QLineEdit()
        self._base_url.setPlaceholderText("https://…/v1")
        self._base_url.textChanged.connect(self._schedule_discovery)
        self._api = QComboBox()
        self._api.addItems(_PROTOCOLS)
        self._api.currentTextChanged.connect(self._schedule_discovery)
        form.addRow("显示名", self._name)
        form.addRow("", self._identity_hint)
        form.addRow("Base URL", self._base_url)
        self._secret = QLineEdit()
        self._secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret.setToolTip("密钥只存入加密库；留空不会删除或覆盖已有密钥")
        form.addRow("密钥", self._secret)
        form.addRow("协议", self._api)
        self._priority = QSpinBox()
        self._priority.setRange(-100, 100)
        self._priority.setToolTip("数字越大越优先；用于备用站点候选排序")
        form.addRow("优先级", self._priority)
        # 参数白名单与删除规则（§二.4）不常用，收进「高级」悬浮面板；这里只存草稿值
        self._param_whitelist: tuple[str, ...] = ()
        self._strip_params: tuple[str, ...] = ()
        # 正在编辑的已保存端点；None = 新建
        self._editing: EndpointConfig | None = None

        buttons = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        buttons.addWidget(save_btn)
        self._advanced_btn = QPushButton("高级…")
        self._advanced_btn.setToolTip("参数白名单与删除规则")
        self._advanced_btn.clicked.connect(self._on_advanced)
        buttons.addWidget(self._advanced_btn)
        delete_btn = QPushButton("删除")
        delete_btn.clicked.connect(self._on_delete)
        buttons.addWidget(delete_btn)
        form.addRow(buttons)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 520])
        root.addWidget(splitter)
        self._refresh_identity_hint()

    # ---------- 数据 ----------

    def reload(self) -> None:
        selected_id = self._selected_id()
        self._list.clear()
        for endpoint in self._settings.load().endpoints:
            item = QListWidgetItem(endpoint.name)
            item.setData(Qt.ItemDataRole.UserRole, endpoint.id)
            item.setToolTip(endpoint.name)
            self._list.addItem(item)
        if selected_id is not None:
            self._select_endpoint(selected_id)

    def _selected_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_select(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        endpoint_id = current.data(Qt.ItemDataRole.UserRole)
        for endpoint in self._settings.load().endpoints:
            if endpoint.id == endpoint_id:
                direction = (
                    -1
                    if previous is not None
                    and self._list.row(current) < self._list.row(previous)
                    else 1
                )
                self.endpoint_selecting.emit(direction)
                # id 是引用锚点，改显示名不改 id（绑定与密钥都挂在它上面）
                self._editing = endpoint
                self._secret.clear()
                self._name.setText(endpoint.name)
                self._base_url.setText(endpoint.base_url)
                self._api.setCurrentText(endpoint.api.value)
                self._param_whitelist = endpoint.param_whitelist
                self._strip_params = endpoint.strip_params
                self._priority.setValue(endpoint.priority)
                self._refresh_identity_hint()
                self._refresh_advanced_label()
                self.endpoint_selected.emit(endpoint)
                return

    def _on_new(self) -> None:
        self._list.setCurrentRow(-1)
        self._editing = None
        self._secret.clear()
        self._name.clear()
        self._base_url.clear()
        self._api.setCurrentIndex(0)
        self._param_whitelist = ()
        self._strip_params = ()
        self._priority.setValue(0)
        self._refresh_identity_hint()
        self._refresh_advanced_label()
        self._name.setFocus()
        self.endpoint_selected.emit(None)

    def _derived_id(self) -> str:
        """已保存的端点沿用原 id；新建时由显示名派生，并避开已有 id。"""
        if self._editing is not None:
            return self._editing.id
        taken = {endpoint.id for endpoint in self._settings.load().endpoints}
        return unique_slug(self._name.text(), taken)

    def _derived_ref(self) -> str:
        """密钥引用默认与 id 相同；旧配置里显式写过的引用名保持不变。"""
        if self._editing is not None and self._editing.credential_ref:
            return self._editing.credential_ref
        return self._derived_id()

    def _has_secret(self, ref: str) -> bool:
        try:
            return bool(self._credentials.resolve(ref))
        except Exception:
            return False

    def _refresh_identity_hint(self, _value: object = None) -> None:
        self._secret.setPlaceholderText(
            "已设置密钥；留空保持不变，输入新密钥可替换"
            if self._has_secret(self._derived_ref())
            else "输入密钥，点击「保存」加密保存（可留空）"
        )
        if self._editing is None and not self._name.text().strip():
            self._identity_hint.setText("站点 ID 与密钥引用由显示名自动生成")
            return
        self._identity_hint.setText(f"站点 ID / 密钥引用：{self._derived_id()}")

    def _refresh_advanced_label(self) -> None:
        count = len(self._param_whitelist) + len(self._strip_params)
        self._advanced_btn.setText(f"高级（{count}）…" if count else "高级…")

    def _form_endpoint(self) -> EndpointConfig:
        ref = self._derived_ref()
        keep_ref = self._editing is not None and self._editing.credential_ref == ref
        return EndpointConfig(
            id=self._derived_id(),
            name=self._name.text().strip(),
            base_url=self._base_url.text().strip(),
            api=ProviderProtocol(self._api.currentText()),
            # 没设过密钥的站点（如本地服务）不带引用，否则发送时会因缺密钥报错
            credential_ref=(
                ref if keep_ref or self._secret.text().strip() or self._has_secret(ref) else None
            ),
            param_whitelist=self._param_whitelist,
            strip_params=self._strip_params,
            priority=self._priority.value(),
        )

    def _schedule_discovery(self, _value: object = None) -> None:
        self._discovery_timer.start()

    def _request_discovery_if_ready(self) -> None:
        # 尚未保存的新密钥不能用于请求，也不能误用旧密钥探测。
        if self._secret.text().strip():
            return
        if not self._base_url.text().strip() or not self._name.text().strip():
            return
        try:
            if not self._has_secret(self._derived_ref()):
                return
            endpoint = self._form_endpoint()
        except Exception:
            return
        self.discovery_requested.emit(endpoint)

    # ---------- 动作 ----------

    def _on_save(self) -> None:
        try:
            endpoint = self._form_endpoint()
        except ValueError as exc:
            _show_error(self, exc)
            return
        secret = self._secret.text().strip()
        if secret:
            try:
                self._credentials.store_secret(self._derived_ref(), secret)
            except Exception:
                # 凭据存储异常可能包含敏感内容，不把原始异常展示到界面。
                _show_error(self, ValueError("密钥保存失败，请重试。"))
                return
        try:
            self._settings.upsert_endpoint(endpoint)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self._secret.clear()
        self._editing = endpoint  # 再次保存是更新，而不是派生出 xxx-2 的新端点
        self.reload()
        self._select_endpoint(endpoint.id)
        self.configuration_changed.emit()
        self.endpoint_saved.emit(endpoint)
        if secret:
            self._schedule_discovery()

    def _select_endpoint(self, endpoint_id: str) -> None:
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == endpoint_id:
                self._list.setCurrentItem(item)
                return

    def _on_delete(self) -> None:
        endpoint_id = self._selected_id()
        if endpoint_id is None:
            return
        try:
            self._settings.delete_endpoint(endpoint_id)
        except ValueError as exc:
            _show_error(self, exc)
            return
        self.reload()
        self.configuration_changed.emit()
        self._on_new()

    def _on_advanced(self) -> None:
        """高级：参数白名单与删除规则（§二.4）。确定只改草稿，点「保存」才落盘。"""
        from limbowave.ui.floating import open_panel

        content = QWidget()
        layout = QVBoxLayout(content)
        form = QFormLayout()
        form.setSpacing(10)
        whitelist = QLineEdit(", ".join(self._param_whitelist))
        whitelist.setPlaceholderText("留空 = 不限制；逗号分隔")
        form.addRow("参数白名单", whitelist)
        strip = QLineEdit(", ".join(self._strip_params))
        strip.setPlaceholderText("发送前去掉的参数，逗号分隔")
        form.addRow("删除规则", strip)
        layout.addLayout(form)
        note = QLabel("发送前先按删除规则去掉，再按白名单裁剪。确定后需点「保存」生效。")
        note.setWordWrap(True)
        note.setProperty("hint", True)
        layout.addWidget(note)
        error = QLabel("")
        error.setWordWrap(True)
        error.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.FS_SMALL}px;")
        error.hide()
        layout.addWidget(error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        row.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setProperty("accent", True)
        row.addWidget(ok)
        layout.addLayout(row)

        panel = open_panel(
            self, "高级：参数规则", content, width=440, anchor=self._advanced_btn
        )
        cancel.clicked.connect(panel.close_panel)

        def _apply() -> None:
            rules = (_split_list(whitelist.text()), _split_list(strip.text()))
            try:
                validate_rules(whitelist=rules[0], strip=rules[1])
            except ValueError as exc:
                error.setText(str(exc))
                error.show()
                return
            self._param_whitelist, self._strip_params = rules
            self._refresh_advanced_label()
            panel.close_panel()

        ok.clicked.connect(_apply)


# 逻辑模型页已独立成模块（用户手动建立 + 按 ID 自动匹配 / 手动配对实际模型）；
# 保留旧名供对话框与设置页沿用。
_ModelsTab = LogicalModelsTab


class _AppearanceTab(AppearanceEditor):
    """Compatibility name retained for the compact dialog and settings page."""

    def __init__(
        self,
        preferences: PreferencesService,
        parent: QWidget | None = None,
        *,
        theme_service: AppearanceThemeService | None = None,
    ) -> None:
        super().__init__(preferences, theme_service=theme_service, parent=parent)


class _SecurityTab(QWidget):
    """安全：系统保护恢复开关（Task 1.3 / §12.3）。

    **如实展示保护等级**：有 Windows Hello 就说需要 Hello 验证；没有就说清楚
    只受 Windows 账户保护，并提示可关闭。开关都要主密码确认——
    启用/关闭恢复本身是敏感操作，不能让人趁人离开时改。
    """

    def __init__(self, vault: Vault, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vault = vault
        self._build()
        self.reload()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setProperty("hint", True)
        root.addWidget(self._detail)

        ops = QHBoxLayout()
        self._enable_btn = QPushButton("启用系统保护恢复")
        self._enable_btn.clicked.connect(self._on_enable)
        ops.addWidget(self._enable_btn)
        self._disable_btn = QPushButton("关闭恢复")
        self._disable_btn.setProperty("flat", True)
        self._disable_btn.clicked.connect(self._on_disable)
        ops.addWidget(self._disable_btn)
        ops.addStretch(1)
        root.addLayout(ops)
        root.addStretch(1)

    def reload(self) -> None:
        from limbowave.domain.platform_capabilities import (
            SYSTEM_RECOVERY_UNAVAILABLE,
            supports_system_identity,
        )

        enabled = self._vault.recovery_enabled()
        if not supports_system_identity():
            self._status.setText("系统保护恢复：当前平台不支持")
            self._detail.setText(SYSTEM_RECOVERY_UNAVAILABLE)
            self._enable_btn.setVisible(False)
            self._disable_btn.setVisible(enabled)  # 可移除迁移来的、不可用的 DPAPI 包装。
            return
        self._status.setText("系统保护恢复：**已启用**" if enabled else "系统保护恢复：未启用")
        self._detail.setText(
            describe_protection()
            if enabled
            else "未启用时，忘记主密码将无法解锁资料库（没有后路）。"
        )
        self._enable_btn.setVisible(not enabled)
        self._disable_btn.setVisible(enabled)

    def _on_enable(self) -> None:
        from limbowave.domain.platform_capabilities import supports_system_identity

        if not supports_system_identity():
            return
        from limbowave.ui.floating import ask_alert, ask_prompt

        def _enable(password: str) -> None:
            try:
                notice = self._vault.enable_recovery(password)
            except InvalidPassword:
                ask_alert(self, "验证失败", "主密码错误，未做任何改动。")
                return
            except Exception as exc:
                ask_alert(self, "启用失败", str(exc))
                return
            ask_alert(self, "已启用", notice)
            self.reload()

        ask_prompt(
            self, "启用系统保护", "先验证主密码：", _enable,
            password=True, animated_password=True,
        )

    def _on_disable(self) -> None:
        from limbowave.ui.floating import ask_alert, ask_confirm

        def _do_disable(ok: bool) -> None:
            if not ok:
                return
            try:
                self._vault.disable_recovery()
            except Exception as exc:
                ask_alert(self, "关闭失败", str(exc))
                return
            self.reload()

        ask_confirm(
            self,
            "关闭恢复",
            "关闭后，忘记主密码将无法再重置（没有后路）。" + chr(10) + chr(10) + "确认关闭？",
            _do_disable,
            confirm_text="关闭",
            danger=True,
        )


def _split_list(text: str) -> tuple[str, ...]:
    """把「逗号分隔」的输入拆成元组。空输入 → 空元组（= 不限制）。"""
    return tuple(part.strip() for part in text.split(",") if part.strip())
