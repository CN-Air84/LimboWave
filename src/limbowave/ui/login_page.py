"""启动登录页：品牌字样逐字进入，随后开放主密码输入。"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QRadialGradient,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
    QStyleOptionFrame,
    QVBoxLayout,
    QWidget,
)

from limbowave.ui import theme
from limbowave.ui.blackout import Blackout
from limbowave.ui.outline_logo import OutlineLogo


class _ScalingGreeting(QLabel):
    """文字绕左上角缩放；绘制变化不会改变两行的布局坐标。"""

    def __init__(self) -> None:
        super().__init__()
        self._font_scale = 1.0

    def _get_font_scale(self) -> float:
        return self._font_scale

    def _set_font_scale(self, value: float) -> None:
        self._font_scale = value
        self.update()

    fontScale = Property(float, _get_font_scale, _set_font_scale)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.setFont(self.font())
        # 固定基准字体的升部高度，避免逐字输入时重算基线或改动布局。
        ascent = QFontMetricsF(self.font()).ascent()
        painter.scale(self._font_scale, self._font_scale)
        painter.drawText(QPointF(0, ascent), self.text())


class _AnimatedBrandName(QLabel):
    """Paint opacity and offset from one reveal progress without moving layout geometry."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(text, parent)
        self._reveal_progress = 0.0

    def _get_reveal_progress(self) -> float:
        return self._reveal_progress

    def _set_reveal_progress(self, value: float) -> None:
        self._reveal_progress = value
        self.update()

    revealProgress = Property(float, _get_reveal_progress, _set_reveal_progress)

    def paintEvent(self, event: QPaintEvent) -> None:
        progress = max(0.0, min(1.0, self._reveal_progress))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setOpacity(progress)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.setFont(self.font())
        ascent = QFontMetricsF(self.font()).ascent()
        painter.drawText(QPointF(0, ascent - 18.0 * (1.0 - progress)), self.text())


class _PasswordDot(QObject):
    """One password dot whose radius animates around a fixed center point."""

    changed = Signal()
    finished = Signal()

    def __init__(self, parent: QObject, *, scale: float) -> None:
        super().__init__(parent)
        self._scale = scale
        self._animation = QPropertyAnimation(self, b"scale", self)
        self._animation.finished.connect(self.finished)

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = value
        self.changed.emit()

    scale = Property(float, _get_scale, _set_scale)

    def animate_to(
        self, value: float, *, duration: int, easing: QEasingCurve.Type
    ) -> None:
        self._animation.stop()
        self._animation.setDuration(duration)
        self._animation.setStartValue(self._scale)
        self._animation.setEndValue(value)
        self._animation.setEasingCurve(easing)
        self._animation.start()


class _AnimatedPasswordEdit(QLineEdit):
    """Password field with center-anchored animated dots and an inner unlock button.

    The real text remains in ``QLineEdit`` for native editing and IME behavior. Its glyphs are
    transparent; dots are painted independently so changing their scale never changes layout.
    """

    _DOT_RADIUS = 4.5
    _DOT_STEP = 17.0
    _BUTTON_INSET = 7

    def __init__(self) -> None:
        super().__init__()
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setStyleSheet(
            "#loginPassword { color: transparent; selection-color: transparent; }"
        )
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0, 0))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0, 0))
        self.setPalette(palette)
        self._reveal = 1.0
        self.enter_button = _EnterButton(self)
        self.setTextMargins(0, 0, _EnterButton.SIZE + self._BUTTON_INSET, 0)
        self._dots: list[_PasswordDot] = []
        self._exiting: list[tuple[_PasswordDot, QPointF]] = []
        self._known_length = 0
        self._caret_visible = True
        self._caret_timer = QTimer(self)
        self._caret_timer.setInterval(530)
        self._caret_timer.timeout.connect(self._toggle_caret)
        self._display_caret_x = 0.0
        self._caret_position_ready = False
        self._caret_move = QPropertyAnimation(self, b"caretX", self)
        self._caret_move.setDuration(145)
        self._caret_move.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._error_strength = 0.0
        self._error_animation = QPropertyAnimation(self, b"errorStrength", self)
        self._error_animation.setDuration(180)
        self._error_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._shake = QPropertyAnimation(self, b"pos", self)
        self._shake.setDuration(410)
        self._shake.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.textChanged.connect(self._sync_dots)
        self.textEdited.connect(self.clear_password_error)
        self.cursorPositionChanged.connect(self._animate_caret_to_target)

    def _get_error_strength(self) -> float:
        return self._error_strength

    def _set_error_strength(self, value: float) -> None:
        self._error_strength = value
        self.update()

    errorStrength = Property(float, _get_error_strength, _set_error_strength)

    def _get_caret_x(self) -> float:
        return self._display_caret_x

    def _set_caret_x(self, value: float) -> None:
        self._display_caret_x = value
        self.update()

    caretX = Property(float, _get_caret_x, _set_caret_x)

    @property
    def reveal_progress(self) -> float:
        return self._reveal

    def set_reveal_progress(self, value: float) -> None:
        """Entry opacity, painted directly so no QGraphicsEffect sits over the field."""
        self._reveal = max(0.0, min(1.0, value))
        self.update()
        self.enter_button.update()

    def show_password_error(self) -> None:
        """Shake in place and retain a red inner stroke until the next user edit."""
        self._error_animation.stop()
        self._error_animation.setStartValue(self._error_strength)
        self._error_animation.setEndValue(1.0)
        self._error_animation.start()

        self._shake.stop()
        origin = self.pos()
        self._shake.setStartValue(origin)
        for step, offset in ((0.14, -10), (0.28, 9), (0.43, -8), (0.58, 6), (0.74, -4), (0.88, 2)):
            self._shake.setKeyValueAt(step, origin + QPoint(offset, 0))
        self._shake.setEndValue(origin)
        self._shake.start()
        self._reset_caret()

    def clear_password_error(self, *_args: object) -> None:
        if self._error_strength <= 0.0 and self._error_animation.state().name != "Running":
            return
        self._error_animation.stop()
        self._error_animation.setStartValue(self._error_strength)
        self._error_animation.setEndValue(0.0)
        self._error_animation.start()

    def _animate_caret_to_target(self, *_args: object) -> None:
        target = self._target_caret_x()
        if not self._caret_position_ready or not self.isVisible():
            self._caret_move.stop()
            self._set_caret_x(target)
            self._caret_position_ready = True
        elif abs(target - self._display_caret_x) < 0.25:
            self._caret_move.stop()
            self._set_caret_x(target)
        else:
            self._caret_move.stop()
            self._caret_move.setStartValue(self._display_caret_x)
            self._caret_move.setEndValue(target)
            self._caret_move.start()
        self._reset_caret()

    def _reset_caret(self, *_args: object) -> None:
        self._caret_visible = True
        if self.hasFocus() and not self._caret_timer.isActive():
            self._caret_timer.start()
        self.update()

    def _toggle_caret(self) -> None:
        self._caret_visible = not self._caret_visible
        self.update()

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        if not self._caret_position_ready:
            self._animate_caret_to_target()
        else:
            self._reset_caret()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self._caret_timer.stop()
        self._caret_visible = False
        super().focusOutEvent(event)
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        size = _EnterButton.SIZE
        self.enter_button.move(
            self.width() - self._BUTTON_INSET - size, (self.height() - size) // 2
        )
        self._caret_move.stop()
        self._set_caret_x(self._target_caret_x())
        self._caret_position_ready = True

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._animate_caret_to_target()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        super().keyPressEvent(event)
        self._animate_caret_to_target()

    def _content_rect(self) -> QRect:
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, self)
        # The style rect ignores text margins; they reserve the inner button's area.
        margins = self.textMargins()
        return rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())

    def _dot_centers(self, count: int | None = None) -> list[QPointF]:
        total = len(self._dots) if count is None else count
        rect = self._content_rect().adjusted(10, 0, -10, 0)
        width = max(0.0, (total - 1) * self._DOT_STEP)
        overflow = max(0.0, width - max(0.0, rect.width() - self._DOT_RADIUS * 2))
        first_x = rect.left() + self._DOT_RADIUS - overflow
        center_y = rect.center().y() + 0.5
        return [QPointF(first_x + index * self._DOT_STEP, center_y) for index in range(total)]

    def _sync_dots(self, text: str) -> None:
        new_length = len(text)
        old_length = self._known_length
        cursor = self.cursorPosition()
        if new_length > old_length:
            added = new_length - old_length
            start = max(0, min(cursor - added, len(self._dots)))
            for offset in range(added):
                dot = _PasswordDot(self, scale=0.0)
                dot.changed.connect(self.update)
                self._dots.insert(start + offset, dot)
                dot.animate_to(1.0, duration=220, easing=QEasingCurve.Type.OutBack)
        elif new_length < old_length:
            removed = old_length - new_length
            old_centers = self._dot_centers(old_length)
            start = max(0, min(cursor, len(self._dots) - removed))
            leaving = self._dots[start : start + removed]
            del self._dots[start : start + removed]
            for offset, dot in enumerate(leaving):
                center = old_centers[start + offset]
                self._exiting.append((dot, center))
                dot.finished.connect(lambda dot=dot: self._remove_exiting(dot))
                dot.animate_to(0.0, duration=150, easing=QEasingCurve.Type.InCubic)
        self._known_length = new_length
        self.update()

    def _remove_exiting(self, target: _PasswordDot) -> None:
        self._exiting = [(dot, center) for dot, center in self._exiting if dot is not target]
        target.deleteLater()
        self.update()

    def prepare_exit_snapshot(self) -> None:
        """Remove password-length indicators before the full-page exit frame is captured."""
        for dot in self._dots:
            dot._animation.stop()
            dot.deleteLater()
        for dot, _center in self._exiting:
            dot._animation.stop()
            dot.deleteLater()
        self._dots.clear()
        self._exiting.clear()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        # Draw only Qt's themed panel. Calling QLineEdit.paintEvent would also paint its native
        # password glyphs and caret, creating a second blinking indicator at another x position.
        if self._reveal <= 0.0:
            return
        painter = QPainter(self)
        painter.setOpacity(self._reveal)
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelLineEdit, option, painter, self
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        content = self._content_rect()
        painter.setClipRect(content)
        if self._should_draw_placeholder():
            painter.setPen(QColor(theme.TEXT_SECONDARY))
            painter.setFont(self.font())
            painter.drawText(
                content.adjusted(10, 0, -10, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self.placeholderText(),
            )
        painter.setBrush(QColor(theme.TEXT_PRIMARY))
        painter.setPen(Qt.PenStyle.NoPen)
        for dot, center in zip(self._dots, self._dot_centers(), strict=True):
            self._paint_dot(painter, dot, center)
        for dot, center in self._exiting:
            self._paint_dot(painter, dot, center)
        if self.hasFocus() and self._caret_visible:
            self._paint_caret(painter)
        if self._error_strength > 0.0:
            color = QColor(theme.DANGER_TEXT)
            color.setAlphaF(min(1.0, self._error_strength))
            painter.setOpacity(self._reveal)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 2.0))
            border = self.rect().adjusted(2, 2, -2, -2)
            radius = max(2, theme.RADIUS_MD - 2)
            painter.drawRoundedRect(border, radius, radius)

    def _should_draw_placeholder(self) -> bool:
        return bool(
            not self.text()
            and self.placeholderText()
            and not self._dots
            and not self._exiting
        )

    def _target_caret_x(self) -> float:
        centers = self._dot_centers()
        cursor = max(0, min(self.cursorPosition(), len(centers)))
        if not centers:
            return self._content_rect().adjusted(10, 0, -10, 0).left() + 1.0
        if cursor == 0:
            return centers[0].x() - self._DOT_STEP / 2
        if cursor == len(centers):
            return centers[-1].x() + self._DOT_STEP / 2
        return (centers[cursor - 1].x() + centers[cursor].x()) / 2

    def _paint_caret(self, painter: QPainter) -> None:
        x = self._display_caret_x if self._caret_position_ready else self._target_caret_x()
        center_y = self._content_rect().center().y() + 0.5
        painter.setOpacity(self._reveal)
        painter.setPen(QPen(QColor(theme.TEXT_PRIMARY), 1.25))
        painter.drawLine(QPointF(x, center_y - 8), QPointF(x, center_y + 8))

    def _paint_dot(self, painter: QPainter, dot: _PasswordDot, center: QPointF) -> None:
        scale = max(0.0, dot._get_scale())
        painter.save()
        painter.setOpacity(min(1.0, scale * 1.35) * self._reveal)
        radius = self._DOT_RADIUS * scale
        painter.drawEllipse(center, radius, radius)
        painter.restore()


class _EnterButton(QPushButton):
    """Circular unlock button painted inside the password field's right edge."""

    SIZE = 36

    def __init__(self, field: _AnimatedPasswordEdit) -> None:
        super().__init__("➜", field)
        self.setObjectName("loginEnter")
        self.setAccessibleName("解锁")
        self.setToolTip("解锁资料库")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        font = QFont("Segoe UI Symbol")
        font.setPixelSize(round(theme.FS_TITLE * 1.25))
        font.setWeight(QFont.Weight.Normal)
        self.setFont(font)

    def paintEvent(self, event: QPaintEvent) -> None:
        # Painted rather than styled so it shares the field's entry opacity.
        field = self.parentWidget()
        reveal = field.reveal_progress if isinstance(field, _AnimatedPasswordEdit) else 1.0
        if reveal <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setOpacity(reveal)
        if self.isDown():
            fill = QColor("#106B48")
        elif self.underMouse():
            fill = QColor(24, 142, 94, 250)
        else:
            fill = QColor(20, 126, 84, 245)
        if self.hasFocus():
            painter.setPen(QPen(QColor(174, 233, 205, 235), 2.0))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0))
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


class _FormReveal(QWidget):
    """Hosts the password field and raises it into place while it fades in.

    The spare ``RISE`` pixels below the field keep the slide from being clipped by this host.
    The reveal advances in clamped frame steps rather than by wall-clock time, so a busy GUI
    thread during startup pauses the entrance instead of letting it jump straight to its end.
    """

    FIELD_HEIGHT = 50
    RISE = 14
    DURATION_MS = 520
    _FRAME_MS = 16
    _MAX_STEP_MS = 34

    finished = Signal()

    def __init__(self, field: _AnimatedPasswordEdit) -> None:
        super().__init__()
        self._field = field
        field.setParent(self)
        self._progress = 0.0
        self._phase = 0.0
        self._curve = QEasingCurve(QEasingCurve.Type.OutCubic)
        self._clock = QElapsedTimer()
        self._ticker = QTimer(self)
        self._ticker.setTimerType(Qt.TimerType.PreciseTimer)
        self._ticker.setInterval(self._FRAME_MS)
        self._ticker.timeout.connect(self._advance)
        field.set_reveal_progress(0.0)
        self.setFixedHeight(self.FIELD_HEIGHT + self.RISE)

    def start(self) -> None:
        self._phase = 0.0
        self._set_reveal_progress(0.0)
        self._clock.start()
        self._ticker.start()

    def settle(self) -> None:
        """Jump to the final state without emitting ``finished``."""
        self._ticker.stop()
        self._phase = 1.0
        self._set_reveal_progress(1.0)

    def _advance(self) -> None:
        # A stalled event loop resumes the reveal where it paused instead of skipping it.
        step = min(self._clock.restart(), self._MAX_STEP_MS)
        self._phase = min(1.0, self._phase + step / self.DURATION_MS)
        self._set_reveal_progress(self._curve.valueForProgress(self._phase))
        if self._phase >= 1.0:
            self._ticker.stop()
            self.finished.emit()

    def _get_reveal_progress(self) -> float:
        return self._progress

    def _set_reveal_progress(self, value: float) -> None:
        self._progress = value
        self._field.set_reveal_progress(value)
        self._place_field()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._place_field()

    def _place_field(self) -> None:
        offset = round(self.RISE * (1.0 - max(0.0, min(1.0, self._progress))))
        self._field.setGeometry(0, offset, self.width(), self.FIELD_HEIGHT)


class LoginPage(QWidget):
    """主窗口内的全屏入口；不持有密码，只通过信号递交一次输入。"""

    submitted = Signal(str)
    cancelled = Signal()
    exit_ready = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("loginPage")
        self._ready = False
        self._started = False
        self._position = 0
        self._loading = False
        self._exit_pending = False
        self._blackout = Blackout(self)
        self._blackout.finished.connect(self._blackout_finished)
        self._logo = OutlineLogo(self, on_black=True)
        self._logo.hide()
        self._logo.finished.connect(self._loading_finished)

        self._hello = _ScalingGreeting()
        self._hello.setObjectName("loginHello")
        self._name_slot = QWidget()
        self._name_slot.setObjectName("loginNameSlot")
        self._name = _AnimatedBrandName("LimboWave", self._name_slot)
        self._name.setObjectName("loginName")
        self._name_intro = QParallelAnimationGroup(self)
        self._name_reveal = QPropertyAnimation(self._name, b"revealProgress", self)
        self._name_reveal.setDuration(620)
        self._name_reveal.setStartValue(0.0)
        self._name_reveal.setEndValue(1.0)
        self._name_reveal.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hello_shrink = QPropertyAnimation(self._hello, b"fontScale", self)
        self._hello_shrink.setDuration(620)
        self._hello_shrink.setStartValue(1.0)
        self._hello_shrink.setEndValue(0.65 / 0.8)
        self._hello_shrink.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._name_intro.addAnimation(self._name_reveal)
        self._name_intro.addAnimation(self._hello_shrink)
        self._name_intro.finished.connect(self._reveal_form)
        self._input = _AnimatedPasswordEdit()
        self._input.setObjectName("loginPassword")
        self._input.setAccessibleName("主密码")
        self._input.returnPressed.connect(self._submit)
        self._enter = self._input.enter_button
        self._enter.clicked.connect(self._submit)

        self._form = _FormReveal(self._input)
        self._form.setObjectName("loginForm")
        self._form.setEnabled(False)
        self._form.finished.connect(self._input.setFocus)

        self._content = QWidget(self)
        self._content.setObjectName("loginContent")
        stack = QVBoxLayout(self._content)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        stack.addWidget(self._hello)
        stack.addWidget(self._name_slot)
        stack.addSpacing(34)
        stack.addWidget(self._form)

        self._typing = QTimer(self)
        self._typing.setInterval(52)
        self._typing.timeout.connect(self._type_next)
        self._resize_content()

    def set_prompt(self, title: str, label: str) -> None:
        """同一页面复用设置、确认、重试输入，不重播开屏。"""
        self._input.clear()
        self._input.setPlaceholderText(title)
        self._input.setAccessibleDescription(label)
        if self._loading:
            self._logo.finish_loading()
            return
        self._input.setEnabled(True)
        if self._ready:
            self._input.setFocus()

    def set_busy(self, title: str, label: str) -> None:
        """显示启动期耗时步骤并暂时禁止重复提交。"""
        self._input.clear()
        self._input.setPlaceholderText(title)
        self._input.setAccessibleDescription(label)
        self._input.clearFocus()
        self._input.setEnabled(False)

    def show_password_error(self) -> None:
        if self._loading:
            self._logo.cancel_loading()
            self._blackout.dismiss()
            self._loading = False
            self._content.show()
            self._form.setEnabled(True)
            self._input.setEnabled(True)
            self.update()
        self._input.show_password_error()
        self._input.setFocus()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            self._typing.start()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._resize_content()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if not self._loading:
                self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        """低对比度的光晕与基线，不引入另一套全局背景资源。"""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.BG_APP))
        accent = QColor(theme.ACCENT)
        accent.setAlpha(22 if theme.current_palette().name == "dark" else 14)
        radius = max(self.width(), self.height()) * 0.65
        glow = QRadialGradient(self.width() * 0.82, self.height() * 0.22, radius)
        glow.setColorAt(0, accent)
        glow.setColorAt(1, QColor(theme.BG_APP))
        painter.fillRect(self.rect(), glow)
        painter.setPen(QColor(theme.BORDER))
        painter.drawLine(40, self.height() - 40, self.width() - 40, self.height() - 40)

    def _resize_content(self) -> None:
        """x 由逻辑窗口宽高共同约束：第二行 x，第一行 0.8x。"""
        logo_width = max(1, round(min(self.width() * 0.4, self.height() * 0.7, 540)))
        self._logo.resize(logo_width, round(logo_width / OutlineLogo.ASPECT_RATIO))
        self._logo.move(
            (self.width() - self._logo.width()) // 2,
            (self.height() - self._logo.height()) // 2,
        )
        width, height = max(self.width(), 320), max(self.height(), 240)
        size = round(max(24, min(width * 0.075, height * 0.125, 106)))
        hello_font = QFont(self.font())
        hello_font.setPixelSize(round(size * 0.8))
        hello_font.setWeight(QFont.Weight.Medium)
        name_font = QFont(self.font())
        name_font.setPixelSize(size)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -size * 0.035)
        self._hello.setFont(hello_font)
        self._name.setFont(name_font)
        self._hello.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;"
            f" font-size: {round(size * 0.8)}px; font-weight: 500;"
        )
        self._name.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; background: transparent;"
            f" font-size: {size}px; font-weight: 600;"
        )
        self._content.setStyleSheet("background: transparent;")
        self._form.setStyleSheet("background: transparent;")
        self._form.setMaximumWidth(650)
        self._hello.setFixedHeight(round(size * 0.95))
        self._name_slot.setFixedHeight(round(size * 1.25))
        self._content.setFixedSize(
            min(width - 48, max(310, round(width * 0.78))),
            self._hello.height() + self._name_slot.height() + 34 + self._form.height(),
        )
        self._name.setFixedSize(self._content.width(), self._name_slot.height())
        self._content.move(
            max(24, (self.width() - self._content.width()) // 2),
            max(20, (self.height() - self._content.height()) // 2),
        )

    def _type_next(self) -> None:
        target = "Hello There"
        self._position += 1
        self._hello.setText(target[:self._position] + "▍")
        if self._position < len(target):
            return
        self._hello.setText(target)
        self._typing.stop()
        QTimer.singleShot(90, self._name_intro.start)

    def _reveal_form(self) -> None:
        self._ready = True
        if self._loading:
            return
        self._form.setEnabled(True)
        self._form.start()

    def prepare_exit_snapshot(self) -> None:
        """Freeze the final visual state before the page transition captures a frame.

        Qt renders ``QGraphicsEffect`` children at incorrect offsets when a widget is painted
        into a scaled pixmap, so the page avoids them and only settles its painted reveals here.
        """
        self._typing.stop()
        self._name_intro.stop()
        self._logo.settle()
        self._name._set_reveal_progress(1.0)
        self._form.settle()
        self._input.clearFocus()
        self._input.prepare_exit_snapshot()
        self.update()

    @property
    def loading(self) -> bool:
        return self._loading

    def _begin_loading(self) -> None:
        self._loading = True
        self._typing.stop()
        self._name_intro.stop()
        self._form.settle()
        self._form.setEnabled(False)
        self._input.clearFocus()
        self._input.prepare_exit_snapshot()
        self._logo.start_loading(defer_until_shown=True)
        self._blackout.fade_to(1.0, Blackout.FADE_IN_MS)

    def request_exit(self) -> None:
        """Gate workspace handoff on the full draw-and-erase loading sequence."""
        if self._exit_pending:
            return
        self._exit_pending = True
        if not self._loading:
            self._begin_loading()
        self._logo.finish_loading()

    def _loading_finished(self) -> None:
        self._logo.hide()
        if self._exit_pending:
            self._loading = False
            self.exit_ready.emit()
            return
        # A confirmation prompt is revealed through the veil; errors bypass this path.
        self._content.show()
        self._blackout.fade_to(0.0, Blackout.FADE_OUT_MS)

    def _blackout_finished(self) -> None:
        if self._blackout.opacity == 1.0:
            self._content.hide()
            self._logo.show()
            self._logo.raise_()
            return
        self._blackout.hide()
        self._loading = False
        self._logo.settle()
        self._form.setEnabled(True)
        self._input.setEnabled(True)
        self._input.setFocus()

    def _submit(self) -> None:
        if (
            self._ready and not self._loading and self._input.isEnabled()
            and (value := self._input.text().strip())
        ):
            self._input.clear()
            self._begin_loading()
            self.submitted.emit(value)

