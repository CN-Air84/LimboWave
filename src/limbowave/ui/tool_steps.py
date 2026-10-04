"""工具步骤展示组件（Phase 9 / 设计计划 §三.2）。

- **折叠态**：一排小 chip，每个显示 名称 + 状态 + 简短摘要；
- **展开态**：点击 chip 展开该步骤的参数、结果、耗时与错误；
- **全局隐藏**：一键隐藏只影响展示——审计数据在消息里（已落库），
  重新打开就回来（设计计划明确要求隐藏 != 删除）。

组件只读：它从 ``ToolStep`` 列表渲染，不改任何数据。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QSize, Qt, QVariantAnimation
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


class _StepDetail(QWidget):
    """Reveal a full-height label through a shrinking viewport, without squeezing its text."""

    def __init__(self, detail: QLabel) -> None:
        super().__init__()
        self.setObjectName(f"{detail.objectName()}-reveal")
        self._detail = detail
        detail.setParent(self)
        self._expanded = False
        self._progress = 0.0
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

    def _natural_height(self, width: int) -> int:
        return max(0, self._detail.heightForWidth(max(1, width)))

    def _set_progress(self, progress: float) -> None:
        self._progress = progress
        self.setFixedHeight(self.heightForWidth(self.width()))

    def sizeHint(self) -> QSize:
        hint = self._detail.sizeHint()
        return QSize(hint.width(), round(hint.height() * self._progress))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def heightForWidth(self, width: int) -> int:
        return round(self._natural_height(width) * self._progress)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            # Inline font/theme changes also alter wrapping without resizing the window.
            self._detail.setGeometry(0, 0, self.width(), self._natural_height(self.width()))
            self._set_progress(self._progress)
        return super().event(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._detail.setGeometry(0, 0, self.width(), self._natural_height(self.width()))
        if event.size().width() != event.oldSize().width():
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
        color = theme.DANGER_TEXT if step.status is ToolStatus.ERROR else theme.TEXT_PRIMARY
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; background: {theme.BG_APP};"
            f" color: {color}; border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_SM}px; padding: 3px 10px;"
            f" font-size: {theme.FS_SMALL}px; }}"
            f"QPushButton:hover {{ background: {theme.BG_SURFACE_HOVER}; }}"
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self.toggle)
        self._refresh_text()

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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        header = QLabel(f"工具步骤（{len(steps)}）")
        header.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px; border: none;"
        )
        layout.addWidget(header)

        row = QHBoxLayout()
        row.setSpacing(6)
        for step in steps:
            chip = _StepChip(step)
            row.addWidget(chip)
        row.addStretch(1)
        layout.addLayout(row)

        # 详情区（默认隐藏，点 chip 展开）
        for step in steps:
            detail = QLabel(self._describe(step))
            detail.setObjectName(f"detail-{step.tool_call_id}")
            detail.setMinimumWidth(0)
            detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            detail.setWordWrap(True)
            detail.setVisible(False)
            detail.setStyleSheet(
                f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_TINY}px;"
                f" background: {theme.BG_APP}; border: 1px solid {theme.BORDER};"
                f" border-radius: {theme.RADIUS_SM}px; padding: 6px 8px;"
            )
            layout.addWidget(_StepDetail(detail))

    def hideEvent(self, event: QHideEvent) -> None:
        for detail in self.findChildren(_StepDetail):
            detail.settle()
        super().hideEvent(event)

    @staticmethod
    def _describe(step: ToolStep) -> str:
        """展开态：参数、结果、耗时、错误（§三.2 要求展开后可见这些）。"""
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
