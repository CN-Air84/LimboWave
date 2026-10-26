"""Reversible presentation-only motion for listening-view chrome."""
from __future__ import annotations

from typing import cast

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QHideEvent, QPainter, QPaintEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QSplitter, QWidget

ZEN_TRANSITION_MS = 360


class ZenMotion(QObject):
    changed = Signal(float)
    finished = Signal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.value = 0.0
        self.target = False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(ZEN_TRANSITION_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.valueChanged.connect(self._update)
        self.animation.finished.connect(self.finished)

    @property
    def running(self) -> bool:
        return self.animation.state() != QAbstractAnimation.State.Stopped

    def _update(self, value: object) -> None:
        self.value = float(cast(float, value))
        self.changed.emit(self.value)

    def set_target(self, target: bool, *, animate: bool) -> None:
        self.target = target
        if not animate or not QApplication.isEffectEnabled(Qt.UIEffect.UI_General):
            self.settle()
            return
        if not self.running and self.value == float(target):
            self.finished.emit()
            return
        self.animation.setDirection(QAbstractAnimation.Direction.Forward if target
                                    else QAbstractAnimation.Direction.Backward)
        if not self.running:
            self.animation.start()
        elif self.animation.state() == QAbstractAnimation.State.Paused:
            self.animation.resume()

    def settle(self) -> None:
        self.animation.stop()
        self._update(float(self.target))
        self.finished.emit()


class SidebarReveal(QWidget):
    """Clip a full-width sidebar as it slides left; never squeeze its contents."""

    def __init__(self, sidebar: QWidget) -> None:
        super().__init__()
        self.sidebar = sidebar
        sidebar.setParent(self)
        self._width = 292
        self._was_visible = True
        self._sliding = False
        self._changing_visibility = False
        self.setMinimumWidth(240)
        self.setMaximumWidth(420)
        self.motion = ZenMotion(self)
        self.motion.changed.connect(self._apply)
        self.motion.finished.connect(self._finish)
        sidebar.installEventFilter(self)

    def sizeHint(self) -> QSize:
        return QSize(self._width, self.sidebar.sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0 if self._sliding else 240, self.sidebar.minimumSizeHint().height())

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed and not self.motion.running and self.motion.value == 0.0:
            self._was_visible = not self.sidebar.isHidden()
            self._width = self.width()
        animate = self.window().isVisible() and self._was_visible
        self._sliding = True
        self._changing_visibility = True
        try:
            if self._was_visible:
                self.setFixedWidth(round(self._width * (1.0 - self.motion.value)))
                self.sidebar.setFixedWidth(self._width)
                self.show()
                self.sidebar.show()
                self._position_sidebar()
        finally:
            self._changing_visibility = False
        splitter = self.parentWidget()
        if isinstance(splitter, QSplitter):
            splitter.handle(1).setEnabled(False)
        self.motion.set_target(collapsed, animate=animate)

    def _apply(self, progress: float) -> None:
        if not self._was_visible:
            return
        width = round(self._width * (1.0 - progress))
        self.setFixedWidth(width)
        splitter = self.parentWidget()
        if isinstance(splitter, QSplitter):
            splitter.setSizes([width, max(1, splitter.width() - width - splitter.handleWidth())])
        self._position_sidebar()

    def _position_sidebar(self) -> None:
        width = self._width if self._sliding else self.width()
        self.sidebar.setGeometry(self.width() - width, 0, width, self.height())

    def _finish(self) -> None:
        self._changing_visibility = True
        self._sliding = False
        try:
            visible = not self.motion.target and self._was_visible
            self.sidebar.setVisible(visible)
            self.setVisible(visible)
            self.sidebar.setMinimumWidth(240)
            self.sidebar.setMaximumWidth(420)
            if visible:
                self.setMinimumWidth(240)
                self.setMaximumWidth(420)
                splitter = self.parentWidget()
                if isinstance(splitter, QSplitter):
                    splitter.setSizes([self._width, max(1, splitter.width() - self._width - 1)])
                self._position_sidebar()
        finally:
            self._changing_visibility = False
        splitter = self.parentWidget()
        if isinstance(splitter, QSplitter):
            splitter.handle(1).setEnabled(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.sidebar and not self._changing_visibility and not self._sliding:
            if event.type() == QEvent.Type.Hide and self.sidebar.isHidden():
                self.hide()
            elif event.type() == QEvent.Type.Show and self.isHidden():
                self.show()
        return False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_sidebar()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        if self.motion.running:
            self.motion.settle()


class ZenControlFade:
    """Temporary effects on individual controls; never fade the input or its parent."""

    def __init__(self, widgets: tuple[QWidget, ...]) -> None:
        self.widgets = widgets
        self.effects: dict[QWidget, QGraphicsOpacityEffect] = {}
        self._mouse_flags: dict[QWidget, bool] = {}
        self._focus_policies: dict[QWidget, Qt.FocusPolicy] = {}

    def prepare(self) -> None:
        for widget in self.widgets:
            if widget not in self.effects:
                self._mouse_flags[widget] = widget.testAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents
                )
                self._focus_policies[widget] = widget.focusPolicy()
                effect = QGraphicsOpacityEffect(widget)
                widget.setGraphicsEffect(effect)
                self.effects[widget] = effect
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            widget.show()

    def apply(self, progress: float) -> None:
        for effect in self.effects.values():
            effect.setOpacity(1.0 - progress)

    def finish(self, hidden: bool) -> None:
        for widget in self.widgets:
            widget.setVisible(not hidden)
            if widget in self.effects:
                widget.setGraphicsEffect(None)  # type: ignore[arg-type]
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                                    self._mouse_flags[widget])
                widget.setFocusPolicy(self._focus_policies[widget])
        self.effects.clear()
        self._mouse_flags.clear()
        self._focus_policies.clear()


class ZenPanelSnapshot(QWidget):
    """Fade an advanced panel snapshot instead of nesting graphics effects."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.frame = QPixmap()
        self.opacity = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def capture(self, panel: QWidget) -> None:
        self.frame = panel.grab()
        self.setGeometry(panel.geometry())
        self.show()
        self.raise_()

    def clear(self) -> None:
        self.hide()
        self.frame = QPixmap()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setOpacity(self.opacity)
        painter.drawPixmap(self.rect(), self.frame)
