"""输入框的厂牌 → 逻辑模型上拉菜单，以及模型行的站点右键菜单。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QMenu, QWidget

from limbowave.domain.model_brand import MODEL_BRANDS, model_brand
from limbowave.domain.model_display import model_name_sort_key
from limbowave.ui import theme
from limbowave.ui.popup_material import install_popup_material
from limbowave.ui.popup_motion import PopupMotion, UpwardComboBox


@dataclass(frozen=True)
class ModelSite:
    endpoint_id: str
    name: str
    model_id: str
    default: bool = False


class _SelectorMenu(QMenu):
    """右键只请求站点菜单，不能走 QMenu 的普通 action 激活路径。"""

    model_context_requested = Signal(str, QPoint)

    def __init__(self, owner: ModelSelector, parent: QWidget) -> None:
        super().__init__(parent)
        self._owner = owner
        self._submenu_motion = PopupMotion(Qt.UIEffect.UI_AnimateMenu, self)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        self.setToolTipsVisible(True)
        self.setStyleSheet(
            f"QMenu {{ padding: 4px; border-radius: {theme.RADIUS_MD}px; menu-scrollable: 1; }}"
            "QMenu::item { padding: 7px 26px 7px 14px; }"
            f"QMenu::item:selected {{ background: {theme.BG_SURFACE_HOVER}; }}"
        )
        install_popup_material(self, owner=owner, radius=theme.RADIUS_MD)

    def event(self, event: QEvent) -> bool:
        # QMenu 首次子菜单会走原生卷出/淡入，后续子菜单却跳过；卷出代理
        # 会短暂隐藏真正的菜单，点击与延迟弹出碰在一起时可留下已激活但不可见的行。
        # 鼠标悬停/按下后的延迟弹出都在 QMenu 的 timerEvent 中执行。
        suppress_effect = event.type() in (
            QEvent.Type.Timer,
            QEvent.Type.MouseMove,
            QEvent.Type.ContextMenu,
        )
        # QMenu::event 会在 mousePressEvent 之前立刻执行尚未到期的悬停计时器，
        # 因此只包 mousePressEvent 不够；点菜单外部则保留正常收起动画。
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            suppress_effect = self.rect().contains(event.position().toPoint())
        if suppress_effect:
            with self._submenu_motion.native_effect_suppressed():
                return super().event(event)
        return super().event(event)

    def setActiveAction(self, action: QAction) -> None:
        # API 直接展开的路径没有 Timer 事件，也要与鼠标/键盘展开一致。
        with self._submenu_motion.native_effect_suppressed():
            super().setActiveAction(action)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        point = event.globalPosition().toPoint()
        if not self._owner.contains_menu_point(point):
            if self._owner.rect().contains(self._owner.mapFromGlobal(point)):
                self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
            super().mousePressEvent(event)
            self._owner.hidePopup()
            return
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        action = self.actionAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and action is not None and action.menu():
            # 厂牌行是展开入口，不是叶子动作；不让 Qt 的「鼠标未离开弹出原点」
            # 判定把明确点击同一厂牌当成关闭。API 激活同时保证首次点击立即展开。
            self.setActiveAction(action)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            action = self.actionAt(event.position().toPoint())
            if action is not None and action.isEnabled() and isinstance(action.data(), str):
                self.setActiveAction(action)
                self.model_context_requested.emit(action.data(), event.globalPosition().toPoint())
            event.accept()
            return
        action = self.actionAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and action is not None and action.menu():
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _keyboard_context(self) -> None:
        action = self.activeAction()
        if action is not None and action.isEnabled() and isinstance(action.data(), str):
            point = self.mapToGlobal(self.actionGeometry(action).bottomLeft())
            self.model_context_requested.emit(action.data(), point)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Menu or (
            event.key() == Qt.Key.Key_F10 and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._keyboard_context()
            event.accept()
            return
        if event.key() != Qt.Key.Key_Escape and any(a.menu() for a in self.actions()):
            with self._submenu_motion.native_effect_suppressed():
                super().keyPressEvent(event)
        else:
            # 叶子菜单选择和 Esc 仍走原路径，不能压掉一级菜单的收起动效。
            super().keyPressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        # 鼠标菜单已在 release 弹出；只补平台通过 ContextMenu 送来的键盘请求。
        if event.reason() == QContextMenuEvent.Reason.Keyboard:
            self._keyboard_context()
        event.accept()


class ModelSelector(UpwardComboBox):
    """保留 QComboBox 的 ID/显示名 API，实际弹出两级菜单而不是原生平面列表。"""

    model_site_selected = Signal(str, str)  # 逻辑 ID、站点 ID；空站点 = 配置默认

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("逻辑模型（按厂牌选择）")
        self.setProperty("comboPopupOpen", False)
        self._menu: _SelectorMenu | None = None
        self._site_menu: _SelectorMenu | None = None
        self._menu_motion = PopupMotion(Qt.UIEffect.UI_AnimateMenu, self)
        self._menu_animated = False
        self._sites: dict[str, tuple[ModelSite, ...]] = {}
        self._route_model = ""
        self._current_endpoint = ""
        self._overridden = False

    def set_sites(
        self,
        sites: Mapping[str, Sequence[ModelSite]],
        *,
        current_model: str = "",
        current_endpoint: str = "",
        overridden: bool = False,
    ) -> None:
        self.hidePopup()
        self._sites = {key: tuple(value) for key, value in sites.items()}
        self._route_model = current_model
        self._current_endpoint = current_endpoint
        self._overridden = overridden

    def showPopup(self) -> None:
        if not self.isEnabled():
            return
        self.hidePopup()
        if self._site_menu is not None:
            self._site_menu.deleteLater()
            self._site_menu = None
        if self._menu is not None:
            self._menu.deleteLater()
            self._menu = None
        groups: dict[str, list[int]] = {}
        for index in range(self.count()):
            model_id = self.itemData(index)
            if not isinstance(model_id, str) or not model_id:
                continue
            brand = model_brand(
                model_id,
                self.itemText(index),
                (site.model_id for site in self._sites.get(model_id, ())),
            )
            groups.setdefault(brand, []).append(index)
        if not groups:
            return
        menu = _SelectorMenu(self, self)
        self._menu = menu
        menu.aboutToHide.connect(self._menu_hidden)
        for brand in MODEL_BRANDS:
            if brand not in groups:
                continue
            models = _SelectorMenu(self, menu)
            models.setTitle(brand)
            models.model_context_requested.connect(self._show_sites)
            brand_action = menu.addMenu(models)
            brand_action.setCheckable(True)
            brand_action.setChecked(self.currentIndex() in groups[brand])
            for index in sorted(
                groups[brand], key=lambda row: model_name_sort_key(self.itemText(row))
            ):
                model_id = str(self.itemData(index))
                action = models.addAction(self.itemText(index).replace("&", "&&"))
                action.setData(model_id)
                action.setCheckable(True)
                action.setChecked(index == self.currentIndex())
                action.setToolTip(f"{model_id}\n右键选择站点（键盘：Shift+F10）")
                action.triggered.connect(
                    lambda _checked=False, value=model_id: self._pick_model(value)
                )
        menu.ensurePolished()
        menu.adjustSize()
        origin = self.mapToGlobal(QPoint())
        position = QPoint(origin.x(), origin.y() - menu.sizeHint().height() - 4)
        with self._menu_motion.native_effect_suppressed() as animated:
            self._menu_animated = animated
            menu.popup(position)
        self.setProperty("comboPopupOpen", menu.isVisible())
        if animated:
            self._menu_motion.reveal(menu, upward=menu.y() < origin.y())

    def contains_menu_point(self, point: QPoint) -> bool:
        if self._menu is None:
            return False
        menus = [self._menu, *self._menu.findChildren(_SelectorMenu)]
        return any(
            menu.isVisible() and menu.rect().contains(menu.mapFromGlobal(point)) for menu in menus
        )

    def _menu_hidden(self) -> None:
        self.setProperty("comboPopupOpen", False)
        if self._menu_animated:
            self._menu_motion.collapse()
        self._menu_animated = False
        self._close_sites()
        # 自定义 showPopup 也必须复位 QComboBox 内部的 pressed/弹层状态。
        super().hidePopup()

    def _close_sites(self) -> None:
        if self._site_menu is not None:
            self._site_menu.close()

    def hidePopup(self) -> None:
        self._close_sites()
        if self._menu is not None:
            self._menu.close()
        super().hidePopup()

    def _pick_model(self, model_id: str) -> None:
        self.hidePopup()
        index = self.findData(model_id)
        if index >= 0:
            self.setCurrentIndex(index)

    def _show_sites(self, model_id: str, position: QPoint) -> None:
        self._close_sites()
        if self._site_menu is not None:
            self._site_menu.deleteLater()
        # 归属当前二级菜单，Esc 只收站点菜单，仍可继续挑选同厂牌模型。
        parent = self.sender()
        menu = _SelectorMenu(self, parent if isinstance(parent, QMenu) else self)
        self._site_menu = menu
        menu.addSection("选择站点")
        sites = self._sites.get(model_id, ())
        if not sites:
            menu.addAction("该逻辑模型尚未绑定站点").setEnabled(False)
        for site in sites:
            current = model_id == self._route_model and site.endpoint_id == self._current_endpoint
            restore = site.default and model_id == self._route_model and self._overridden
            note = "恢复默认" if restore else "默认" if site.default else ""
            if current:
                note = f"{note} · 当前" if note else "当前"
            text = f"{site.name}（{note}）" if note else site.name
            action = menu.addAction(text.replace("&", "&&"))
            endpoint_id = "" if site.default else site.endpoint_id
            action.setData(endpoint_id)
            action.setToolTip(f"{site.endpoint_id} → {site.model_id}")
            action.setCheckable(True)
            action.setChecked(current)
            action.setEnabled(not current or restore)
            action.triggered.connect(
                lambda _checked=False, target=endpoint_id: self._pick_site(model_id, target)
            )
        with self._menu_motion.native_effect_suppressed():
            menu.popup(position)

    def _pick_site(self, model_id: str, endpoint_id: str) -> None:
        self.hidePopup()
        # 不先 setCurrentIndex：否则会先走默认站点，再异步竞争一次覆盖。
        self.model_site_selected.emit(model_id, endpoint_id)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.TEXT_SECONDARY), 1.5))
        x = self.width() - 10
        y = self.height() // 2 + 2
        painter.drawLine(x - 4, y, x, y - 4)
        painter.drawLine(x, y - 4, x + 4, y)
        painter.end()
