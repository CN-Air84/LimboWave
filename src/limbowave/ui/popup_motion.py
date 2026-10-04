"""上拉弹层的进出场动效：进场用遮罩逐步露出，退场用截图残影逐步收起。

Qt 自带的菜单 / 下拉动画按它自己算的位置播放（Windows 上固定自上而下卷出），
而我们的弹层贴在控件上方展开，方向对不上。所以弹出时先关掉自带动画，
定好位置后再按实际方向补播；收起时 Qt 没有对应动画，由残影窗口代播。

退场不能拖住弹层本身：弹层还在时它仍是活动 popup，会吞掉关闭它的那次点击。
所以弹层照常立即隐藏，原位换上一张不接收输入的截图来播收起。

下拉框直接用 ``UpwardComboBox``；菜单这类自己管弹出位置的弹层用 ``PopupMotion``。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QComboBox, QWidget

from limbowave.ui.popup_material import install_popup_material

_REVEAL_MS = 150  # 与 Qt 自带下拉动画时长一致
_COLLAPSE_MS = 120  # 退场略快，收起不该让人等


class _PopupGhost(QWidget):
    """弹层收起时留在原位的截图，只负责播退场，不抢焦点也不接收输入。"""

    def __init__(self, pixmap: QPixmap, geometry: QRect, *, upward: bool) -> None:
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        # 截图可能带圆角外的透明区（Windows 11 样式的菜单是半透明窗口）
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._pixmap = pixmap
        self._upward = upward
        self._shown = geometry.height()
        self.setGeometry(geometry)

    def set_shown(self, shown: int) -> None:
        self._shown = shown
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        # 只画还没收走的那一段：在上方时贴着底边（靠近控件）留，否则贴着顶边留
        y = self.height() - self._shown if self._upward else 0
        painter = QPainter(self)
        painter.setClipRect(0, y, self.width(), self._shown)
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()


class PopupMotion(QObject):
    """给一个上拉弹层配进出场动效。

    用法：弹出时包在 ``native_effect_suppressed()`` 里，定好位置后调 ``reveal()``；
    弹层隐藏前（仍可见时）调 ``collapse()``。
    """

    def __init__(self, effect: Qt.UIEffect, parent: QObject) -> None:
        super().__init__(parent)
        self._effect = effect
        self._popup: QWidget | None = None
        self._upward = True
        self._shown = 0
        self._reveal_anim: QVariantAnimation | None = None
        self._collapse_anim: QVariantAnimation | None = None
        self._ghost: _PopupGhost | None = None

    @contextmanager
    def native_effect_suppressed(self) -> Iterator[bool]:
        """临时关掉 Qt 自带的弹出动画；产出系统原本是否开着动画。"""
        enabled = QApplication.isEffectEnabled(self._effect)
        if enabled:
            QApplication.setEffectEnabled(self._effect, False)
        try:
            yield enabled
        finally:
            if enabled:
                QApplication.setEffectEnabled(self._effect, True)

    def reveal(self, popup: QWidget, *, upward: bool) -> None:
        """用遮罩逐步露出弹层：在上方时从底边向上长，否则从顶边向下长。"""
        self._stop_collapse()
        self._stop_reveal()
        self._popup = popup
        self._upward = upward
        width, height = popup.width(), popup.height()

        def _apply(value: object) -> None:
            shown = max(1, int(cast(float, value)))
            self._shown = shown
            y = height - shown if upward else 0
            popup.setMask(QRegion(0, y, width, shown))

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(float(height))
        anim.setDuration(_REVEAL_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(_apply)
        anim.finished.connect(self._stop_reveal)
        _apply(0.0)
        self._reveal_anim = anim
        anim.start()

    def collapse(self) -> None:
        """弹层即将隐藏：在原位放一张截图，朝控件方向收起。

        只对播过进场的弹层生效；系统关了动画时直接略过。
        """
        popup = self._popup
        revealing = self._reveal_anim is not None
        self._stop_reveal()
        if popup is None or not popup.isVisible():
            return
        if not QApplication.isEffectEnabled(self._effect):
            return
        # 进场没播完就被关掉时，从当前露出的高度接着收
        start = self._shown if revealing else popup.height()
        self._stop_collapse()
        ghost = _PopupGhost(popup.grab(), popup.geometry(), upward=self._upward)

        def _apply(value: object) -> None:
            ghost.set_shown(max(1, int(cast(float, value))))

        anim = QVariantAnimation(self)
        anim.setStartValue(float(start))
        anim.setEndValue(0.0)
        anim.setDuration(_COLLAPSE_MS)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.valueChanged.connect(_apply)
        anim.finished.connect(self._stop_collapse)
        _apply(float(start))
        ghost.show()
        self._ghost = ghost
        self._collapse_anim = anim
        anim.start()

    def _stop_reveal(self) -> None:
        anim = self._reveal_anim
        if anim is None:
            return
        self._reveal_anim = None
        anim.stop()
        anim.deleteLater()
        if self._popup is not None:
            self._popup.clearMask()

    def _stop_collapse(self) -> None:
        anim, ghost = self._collapse_anim, self._ghost
        self._collapse_anim = None
        self._ghost = None
        if anim is not None:
            anim.stop()
            anim.deleteLater()
        if ghost is not None:
            ghost.hide()
            ghost.deleteLater()


class UpwardComboBox(QComboBox):
    """弹层固定从控件上方展开的下拉框，进出场按实际方向播放。

    上方放不下时仍由 Qt 摆在下方，动效方向随之翻转。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._motion = PopupMotion(Qt.UIEffect.UI_AnimateCombo, self)

    def showPopup(self) -> None:
        view = self.view()
        popup = view.window()
        # Install before the first show so Qt never exposes a solid first frame.
        # Both the native container and the list viewport own backing surfaces.
        for surface in (popup, view, view.viewport()):
            install_popup_material(surface, owner=self)
        # Qt 自带的展开动画按它自己算的位置播放（通常向下），播完才轮到我们上移，
        # 于是动画方向和最终位置相反。这里先关掉它，定好位置后再按实际方向补播。
        with self._motion.native_effect_suppressed() as animate:
            super().showPopup()

        # 不能对弹层 adjustSize()：容器的 sizeHint 极小，会把 Qt 已算好的
        # 列表尺寸压成几个像素，导致之后再点只弹出一个看不见的弹层。
        popup = self.view().window()
        origin = self.mapToGlobal(QPoint(0, 0))
        top = origin.y() - popup.height() - 4
        screen = self.screen()
        if screen is None or top >= screen.availableGeometry().top():
            popup.move(popup.x(), top)
        if animate:
            self._motion.reveal(popup, upward=popup.y() < origin.y())

    def hidePopup(self) -> None:
        # 再次点击控件收起时，Qt 的容器已自带 WA_NoMouseReplay，不会紧接着再弹出
        self._motion.collapse()
        super().hidePopup()
