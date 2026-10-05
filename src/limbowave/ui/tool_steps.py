"""工具步骤展示组件（Phase 9 / 设计计划 §三.2）。

- **折叠态**：一排小 chip，每个显示 名称 + 状态 + 简短摘要；
- **展开态**：点击 chip 展开该步骤的参数、结果、耗时与错误；
- **全局隐藏**：一键隐藏只影响展示——审计数据在消息里（已落库），
  重新打开就回来（设计计划明确要求隐藏 != 删除）。

组件只读：它从 ``ToolStep`` 列表渲染，不改任何数据。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QRect,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QHideEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.domain.tool_step import ToolStatus, ToolStep
from limbowave.ui import theme

_STATUS_TEXT = {
    ToolStatus.RUNNING: "运行中",
    ToolStatus.OK: "完成",
    ToolStatus.ERROR: "失败",
}

_CHIP_MAX_SIZE_HINT_WIDTH = 360
_CHIP_TEXT_INSET = 24
_DETAIL_REVEAL_MS = 220
_TOOL_SLIDE_OFFSET = 12


class _StepDetail(QWidget):
    """Reveal a full-height label through a shrinking viewport, without squeezing its text."""

    geometry_changed = Signal()

    def __init__(self, detail: QLabel) -> None:
        super().__init__()
        self.setObjectName(f"{detail.objectName()}-reveal")
        self._detail = detail
        detail.setParent(self)
        self._expanded = False
        self._progress = 0.0
        self._height_cache: dict[int, int] = {}
        self._size_hint_cache: QSize | None = None
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(_DETAIL_REVEAL_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._set_progress)
        self._animation.finished.connect(self.settle)
        self.hide()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        parent = self.parentWidget()
        if (
            parent is None
            or not parent.isVisible()
            or not QApplication.isEffectEnabled(Qt.UIEffect.UI_General)
        ):
            self.settle()
            return
        self._set_progress(self._progress)
        self._detail.show()
        self.show()
        self._animation.setDirection(
            QAbstractAnimation.Direction.Forward
            if expanded else QAbstractAnimation.Direction.Backward
        )
        # Reversing the same timeline keeps both the height and easing continuous.
        if self._animation.state() == QAbstractAnimation.State.Stopped:
            self._animation.start()
        elif self._animation.state() == QAbstractAnimation.State.Paused:
            self._animation.resume()

    def settle(self) -> None:
        """Finish immediately when hidden; keep the user's chosen expansion state."""
        self._animation.stop()
        self._set_progress(float(self._expanded))
        self._detail.setVisible(self._expanded)
        self.setVisible(self._expanded)
        self.updateGeometry()

    def _invalidate_text_metrics(self) -> None:
        self._height_cache.clear()
        self._size_hint_cache = None

    def _natural_height(self, width: int) -> int:
        width = max(1, width)
        if width not in self._height_cache:
            # Qt probes preferred and actual widths in the same layout pass. A single
            # last-width cache thrashes even when neither the text nor viewport changed.
            if len(self._height_cache) >= 4:
                self._height_cache.pop(next(iter(self._height_cache)))
            self._height_cache[width] = max(0, self._detail.heightForWidth(width))
        return self._height_cache[width]

    def _set_progress(self, progress: float) -> None:
        self._progress = progress
        height = self.heightForWidth(self.width())
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)
            self.geometry_changed.emit()

    def sizeHint(self) -> QSize:
        if self._progress == 0.0:
            return QSize(0, 0)
        if self._size_hint_cache is None:
            self._size_hint_cache = self._detail.sizeHint()
        hint = self._size_hint_cache
        return QSize(hint.width(), round(hint.height() * self._progress))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def heightForWidth(self, width: int) -> int:
        return round(self._natural_height(width) * self._progress) if self._progress else 0

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self._invalidate_text_metrics()
            # Inline font/theme changes also alter wrapping without resizing the window.
            self._detail.setGeometry(0, 0, self.width(), self._natural_height(self.width()))
            self._set_progress(self._progress)
        return super().event(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._detail.setGeometry(0, 0, self.width(), self._natural_height(self.width()))
            self._set_progress(self._progress)


class _StepChip(QPushButton):
    """单个工具步骤：折叠时是一行，点开显示详情。"""

    def __init__(self, step: ToolStep, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._step = step
        self._expanded = False
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self.toggle)
        self.update_step(step)

    def update_step(self, step: ToolStep) -> None:
        if self._step is step and self._full_text:
            return
        self._step = step
        color = theme.DANGER_TEXT if step.status is ToolStatus.ERROR else theme.TEXT_PRIMARY
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; background: {theme.BG_APP};"
            f" color: {color}; border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_SM}px; padding: 3px 10px;"
            f" font-size: {theme.FS_SMALL}px; }}"
            f"QPushButton:hover {{ background: {theme.BG_SURFACE_HOVER}; }}"
        )
        self._refresh_text()
        self.updateGeometry()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_text()
        parent = self.parentWidget()
        if parent is None:
            return
        detail = parent.findChild(_StepDetail, f"detail-{self._step.tool_call_id}-reveal")
        if detail is not None:
            detail.set_expanded(self._expanded)

    def _refresh_text(self) -> None:
        step = self._step
        arrow = "▾" if self._expanded else "▸"
        status = _STATUS_TEXT.get(step.status, step.status.value)
        duration = f" · {step.duration_ms}ms" if step.duration_ms else ""
        summary = f" · {step.summary}" if step.summary else ""
        self._full_text = f"{arrow} {step.name} · {status}{duration}{summary}"
        self.setAccessibleName(self._full_text)
        self._elide_text()
        self.setToolTip("点击收起详情" if self._expanded else "点击展开详情")

    def _elide_text(self) -> None:
        """Elide only the collapsed display; expanded details keep the full result."""
        available = max(1, self.width() - _CHIP_TEXT_INSET)
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                available,
            )
        )

    def sizeHint(self) -> QSize:
        """A long tool result must not become the parent's minimum width."""
        base = super().sizeHint()
        natural = self.fontMetrics().horizontalAdvance(self._full_text) + _CHIP_TEXT_INSET
        return QSize(min(natural, _CHIP_MAX_SIZE_HINT_WIDTH), base.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide_text()


class ToolStepsView(QWidget):
    """一条助手消息的工具步骤区。默认折叠；可整体隐藏。"""

    def __init__(self, steps: tuple[ToolStep, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._reveal_progress = 1.0
        self._height_cache: tuple[int, int] | None = None
        self._body = QWidget(self)
        self._body.installEventFilter(self)
        layout = QVBoxLayout(self._body)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        header = QLabel(f"工具步骤（{len(steps)}）")
        header.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; border: none;"
        )
        layout.addWidget(header)

        self._header = header
        self._row = QHBoxLayout()
        self._row.setSpacing(6)
        self._row.addStretch(1)
        layout.addLayout(self._row)
        self._chips: dict[str, _StepChip] = {}
        self._details: dict[str, _StepDetail] = {}
        self.update_steps(steps)

    def update_steps(self, steps: tuple[ToolStep, ...]) -> None:
        """按调用 ID 就地更新，完成事件不销毁用户正在查看的展开详情。"""
        layout = self._body.layout()
        assert isinstance(layout, QVBoxLayout)
        keys = {step.tool_call_id for step in steps}
        for key in list(self._chips):
            if key not in keys:
                chip = self._chips.pop(key)
                old_panel = self._details.pop(key)
                self._row.removeWidget(chip)
                layout.removeWidget(old_panel)
                chip.setParent(None)
                old_panel.setParent(None)
                chip.deleteLater()
                old_panel.deleteLater()
        self._header.setText(f"工具步骤（{len(steps)}）")
        for index, step in enumerate(steps):
            key = step.tool_call_id
            if key in self._chips:
                chip = self._chips[key]
                chip.update_step(step)
                panel = self._details[key]
                description = self._describe(step)
                if panel._detail.text() != description:
                    panel._invalidate_text_metrics()
                    panel._detail.setText(description)
                    panel._detail.updateGeometry()
            else:
                chip = _StepChip(step)
                self._chips[key] = chip
                detail = QLabel(self._describe(step))
                detail.setObjectName(f"detail-{key}")
                detail.setTextFormat(Qt.TextFormat.PlainText)
                # A text control retains wrapped layout between paints and allows copying.
                detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                detail.setMinimumWidth(0)
                detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                detail.setWordWrap(True)
                detail.setVisible(False)
                detail.setStyleSheet(
                    f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px;"
                    f" background: {theme.BG_APP}; border: 1px solid {theme.BORDER};"
                    f" border-radius: {theme.RADIUS_SM}px; padding: 6px 8px;"
                )
                panel = _StepDetail(detail)
                panel.geometry_changed.connect(self._invalidate_height)
                self._details[key] = panel
            if self._row.indexOf(chip) != index:
                self._row.insertWidget(index, chip)
            if layout.indexOf(panel) != 2 + index:
                layout.insertWidget(2 + index, panel)
        self._height_cache = None
        self._layout_body()

    def _invalidate_height(self) -> None:
        # The child size change already invalidated its QLayout. Invalidating it again
        # from LayoutRequest posts another request and keeps idle/paused panels busy.
        self._height_cache = None
        self._layout_body()

    def set_reveal_progress(self, progress: float) -> None:
        """Slide a natural-height body through a shrinking viewport; never squash its text."""
        self._reveal_progress = progress
        self._layout_body()

    def _natural_height(self, width: int) -> int:
        width = max(1, width)
        if self._height_cache is not None and self._height_cache[0] == width:
            return self._height_cache[1]
        layout = self._body.layout()
        assert layout is not None
        height = layout.totalHeightForWidth(width)
        natural = max(0, height if height >= 0 else layout.totalSizeHint().height())
        self._height_cache = (width, natural)
        return natural

    def _layout_body(self) -> None:
        natural = self._natural_height(self.width())
        offset = round(min(_TOOL_SLIDE_OFFSET, natural) * (1.0 - self._reveal_progress))
        geometry = QRect(0, -offset, self.width(), natural)
        if self._body.geometry() != geometry:
            self._body.setGeometry(geometry)
        height = round(natural * self._reveal_progress)
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)

    def sizeHint(self) -> QSize:
        hint = self._body.sizeHint()
        return QSize(hint.width(), round(hint.height() * self._reveal_progress))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def heightForWidth(self, width: int) -> int:
        return round(self._natural_height(width) * self._reveal_progress)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._body and event.type() == QEvent.Type.LayoutRequest:
            self._invalidate_height()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._layout_body()

    def hideEvent(self, event: QHideEvent) -> None:
        for detail in self.findChildren(_StepDetail):
            detail.settle()
        super().hideEvent(event)

    @staticmethod
    def _describe(step: ToolStep) -> str:
        """展开态：参数、结果、耗时、错误（§三.2 要求展开后可见这些）。"""
        if step.display_detail is not None:
            return step.display_detail
        parts = [f"参数：{step.args or '（无）'}"]
        if step.status is ToolStatus.ERROR:
            parts.append(f"错误：{step.error or '未知'}")
        else:
            parts.append(f"结果：{step.result_summary or '（无摘要）'}")
        parts.append(f"耗时：{step.duration_ms}ms")
        return "\n".join(parts)


def make_tool_steps(steps: tuple[ToolStep, ...]) -> ToolStepsView | None:
    """有步骤才返回组件（没有就返回 None，调用方据此不占位）。"""
    return ToolStepsView(steps) if steps else None
