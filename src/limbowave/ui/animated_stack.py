"""Shared fade-and-slide page transition used by primary and secondary settings tabs."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QStackedWidget,
    QTabBar,
    QWidget,
)


class AnimatedTabBar(QTabBar):
    """Horizontal tab bar with an independently animated underline indicator."""

    def __init__(
        self, parent: QWidget | None = None, *, indicator_name: str = "appearanceSubtabIndicator"
    ) -> None:
        super().__init__(parent)
        self._indicator = QFrame(self)
        self._indicator.setObjectName(indicator_name)
        self._indicator.setFixedHeight(2)
        self._indicator.raise_()
        self._indicator_animation = QPropertyAnimation(self._indicator, b"geometry", self)
        self._indicator_animation.setDuration(230)
        self._indicator_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.currentChanged.connect(self._move_indicator)

    def preferred_width(self) -> int:
        return sum(self.tabSizeHint(index).width() for index in range(self.count()))

    def refresh_width_constraints(self, available_width: int | None = None) -> None:
        preferred = self.preferred_width()
        if available_width is None:
            parent = self.parentWidget()
            available_width = parent.width() if parent is not None else preferred
        target = min(preferred, max(0, available_width)) if preferred else 0
        self.setUsesScrollButtons(target < preferred)
        self.setMinimumWidth(target)
        self.setMaximumWidth(target)
        self.updateGeometry()

    def _target_rect(self, index: int) -> QRect:
        if not 0 <= index < self.count():
            return QRect()
        tab = self.tabRect(index)
        inset = min(12, max(4, tab.width() // 5))
        return QRect(
            tab.left() + inset,
            max(0, self.height() - 2),
            max(12, tab.width() - inset * 2),
            2,
        )

    def _move_indicator(self, index: int) -> None:
        target = self._target_rect(index)
        if not target.isValid():
            return
        current = self._indicator.geometry()
        if self.isVisible() and current.isValid() and current.width() > 0:
            self._indicator_animation.stop()
            self._indicator_animation.setStartValue(current)
            self._indicator_animation.setEndValue(target)
            self._indicator_animation.start()
        else:
            self._indicator.setGeometry(target)
        self._indicator.raise_()

    def sync_indicator(self) -> None:
        self._indicator_animation.stop()
        self._indicator.setGeometry(self._target_rect(self.currentIndex()))
        self._indicator.raise_()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.refresh_width_constraints()
        self.sync_indicator()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.refresh_width_constraints()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if self._indicator_animation.state() != QAbstractAnimation.State.Running:
            self.sync_indicator()


class AnimatedPageStack(QWidget):
    """Slide/fade pages along the navigation axis; list refreshes stay vertical."""

    current_changed = Signal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        orientation: Qt.Orientation = Qt.Orientation.Vertical,
    ) -> None:
        super().__init__(parent)
        self._orientation = orientation
        self._stack = QStackedWidget(self)
        self._effect = QGraphicsOpacityEffect(self._stack)
        self._effect.setOpacity(1.0)
        self._stack.setGraphicsEffect(self._effect)
        self._effect.setEnabled(False)
        self._animation: QParallelAnimationGroup | None = None
        self._generation = 0
        self._target_index: int | None = None
        self._refresh_snapshot: QPixmap | None = None
        self._refresh_direction = QPoint(0, 1)
        self._outgoing: QLabel | None = None

    @property
    def current_index(self) -> int:
        return self._stack.currentIndex()

    def currentIndex(self) -> int:
        return self.current_index

    def count(self) -> int:
        return self._stack.count()

    def add_page(self, page: QWidget) -> int:
        return self._stack.addWidget(page)

    def addWidget(self, page: QWidget) -> int:
        return self.add_page(page)

    def replace_page(self, index: int, page: QWidget) -> QWidget | None:
        if not 0 <= index < self._stack.count():
            return None
        self._generation += 1
        self._stop_animation()
        self._target_index = None
        current = self._stack.currentIndex()
        old = self._stack.widget(index)
        if old is None:
            return None
        self._stack.removeWidget(old)
        self._stack.insertWidget(index, page)
        self._stack.setCurrentIndex(index if current == index else current)
        self._normalize()
        return old

    def set_index(self, index: int, *, animated: bool = True) -> None:
        if not 0 <= index < self._stack.count():
            return
        current = self._stack.currentIndex()
        if index == current:
            self._generation += 1
            self._stop_animation()
            self._target_index = None
            return
        self._generation += 1
        generation = self._generation
        self._stop_animation()
        self._target_index = None
        step = 1 if index > current else -1
        direction = (
            QPoint(step, 0)
            if self._orientation == Qt.Orientation.Horizontal
            else QPoint(0, step)
        )
        if not animated or not self.isVisible():
            self._stack.setCurrentIndex(index)
            self._normalize()
            self.current_changed.emit(index)
            return
        self._target_index = index
        self._animate_out(index, direction, generation)

    def capture_refresh(self, direction: int = 1) -> None:
        """Capture the old form before its widgets are updated in place."""
        self._generation += 1
        self._stop_animation()
        self._target_index = None
        self._refresh_direction = QPoint(0, -1 if direction < 0 else 1)
        self._refresh_snapshot = self._stack.grab() if self.isVisible() else None

    def animate_refresh(self, index: int) -> None:
        """Slide refreshed list content vertically, independent of tab orientation."""
        if not 0 <= index < self._stack.count():
            return
        self._generation += 1
        self._stop_animation()
        self._target_index = index
        self._stack.setCurrentIndex(index)
        snapshot, self._refresh_snapshot = self._refresh_snapshot, None
        direction, self._refresh_direction = self._refresh_direction, QPoint(0, 1)
        if not self.isVisible():
            return
        self._effect.setEnabled(True)
        if snapshot is None:
            self._begin_incoming(index, direction, self._generation)
            return
        self._outgoing = QLabel(self)
        self._outgoing.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._outgoing.setPixmap(snapshot)
        self._outgoing.resize(self.size())
        outgoing_effect = QGraphicsOpacityEffect(self._outgoing)
        outgoing_effect.setOpacity(1.0)
        self._outgoing.setGraphicsEffect(outgoing_effect)
        self._effect.setOpacity(0.0)
        self._outgoing.show()
        self._outgoing.raise_()
        self._animate_out(index, direction, self._generation)

    def _animate_out(self, index: int, direction: QPoint, generation: int) -> None:
        self._effect.setEnabled(True)
        group = QParallelAnimationGroup(self)
        outgoing = self._outgoing if self._outgoing is not None else self._stack
        effect = outgoing.graphicsEffect()
        assert isinstance(effect, QGraphicsOpacityEffect)
        opacity = QPropertyAnimation(effect, b"opacity", group)
        opacity.setDuration(160)
        opacity.setStartValue(effect.opacity())
        opacity.setEndValue(0.0)
        opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        position = QPropertyAnimation(outgoing, b"pos", group)
        position.setDuration(160)
        position.setStartValue(outgoing.pos())
        position.setEndValue(direction * -14)
        position.setEasingCurve(QEasingCurve.Type.InCubic)
        group.addAnimation(opacity)
        group.addAnimation(position)
        group.finished.connect(lambda: self._begin_incoming(index, direction, generation))
        group.finished.connect(group.deleteLater)
        self._animation = group
        group.start()

    def _begin_incoming(self, index: int, direction: QPoint, generation: int) -> None:
        if generation != self._generation:
            return
        self._clear_outgoing()
        self._stack.setCurrentIndex(index)
        self._stack.move(direction * 18)
        self._effect.setOpacity(0.0)
        group = QParallelAnimationGroup(self)
        opacity = QPropertyAnimation(self._effect, b"opacity", group)
        opacity.setDuration(230)
        opacity.setStartValue(0.0)
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        position = QPropertyAnimation(self._stack, b"pos", group)
        position.setDuration(230)
        position.setStartValue(self._stack.pos())
        position.setEndValue(QPoint(0, 0))
        position.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(opacity)
        group.addAnimation(position)
        group.finished.connect(lambda: self._finish_switch(index, generation))
        group.finished.connect(group.deleteLater)
        self._animation = group
        group.start()

    def _finish_switch(self, index: int, generation: int) -> None:
        if generation != self._generation:
            return
        self._normalize()
        self._target_index = None
        self._animation = None
        self.current_changed.emit(index)

    def _stop_animation(self) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
            self._animation = None
        self._normalize()

    def _clear_outgoing(self) -> None:
        if self._outgoing is not None:
            self._outgoing.hide()
            self._outgoing.deleteLater()
            self._outgoing = None

    def _normalize(self) -> None:
        self._clear_outgoing()
        self._effect.setOpacity(1.0)
        self._effect.setEnabled(False)
        self._stack.move(0, 0)
        self._stack.resize(self.size())

    def hideEvent(self, event: Any) -> None:
        # Hidden settings pages are reused. Never keep a half-transparent stack
        # (or a stale destination callback) alive across a close/reopen.
        target = self._target_index
        self._generation += 1
        self._stop_animation()
        self._target_index = None
        self._refresh_snapshot = None
        if target is not None:
            self._stack.setCurrentIndex(target)
            self.current_changed.emit(target)
        super().hideEvent(event)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._stack.resize(self.size())
