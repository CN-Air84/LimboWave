"""Foreground-only theme effects: glyph glow, hover surface suspension, combo frames."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QObject,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QGraphicsDropShadowEffect,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QWidget,
)
from shiboken6 import isValid

from limbowave.domain.appearance import MaterialSettings, TextGlowSettings
from limbowave.ui import theme

_GLOW_MARKER = "limbowaveTextGlow"


def _labels(root: QWidget) -> Iterable[QLabel]:
    if isinstance(root, QLabel):
        yield root
    yield from root.findChildren(QLabel)


def apply_text_glow(root: QWidget, settings: TextGlowSettings) -> None:
    """Apply white glow to transparent dark-text labels without touching layout pixels."""
    for label in _labels(root):
        existing = label.graphicsEffect()
        owned = existing is not None and bool(existing.property(_GLOW_MARKER))
        if owned:
            label.setGraphicsEffect(None)  # type: ignore[arg-type]
        if not settings.enabled or label.property("themeGlowDisabled"):
            continue
        pixmap = label.pixmap()
        if pixmap is not None and not pixmap.isNull():
            continue
        css = label.styleSheet().lower().replace(" ", "")
        if "background:" in css and "background:transparent" not in css:
            continue
        color = label.palette().color(label.foregroundRole())
        if not color.isValid():
            color = QColor(theme.TEXT_PRIMARY)
        if color.lightness() > 205:
            continue
        font_size = label.font().pointSizeF()
        if font_size <= 0:
            font_size = float(theme.FS_BASE)
        intensity, radius = settings.values_for_size(font_size, theme.FS_TINY, theme.FS_TITLE)
        effect = QGraphicsDropShadowEffect(label)
        effect.setProperty(_GLOW_MARKER, True)
        glow = QColor("#FFFFFF")
        glow.setAlphaF(intensity)
        effect.setColor(glow)
        effect.setBlurRadius(radius)
        effect.setOffset(0, 0)
        label.setGraphicsEffect(effect)


class _ComboFrameLayer(QWidget):
    """Mouse-transparent child that strokes the combo outline on top of its QSS border."""

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self.color = QColor(0, 0, 0, 0)
        self.setObjectName("comboFrameLayer")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The app sheet paints every QWidget's canvas; this layer must only draw a stroke.
        self.setStyleSheet("background: transparent;")
        self.hide()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.color.alpha() == 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # QSS strokes a 1px border as one device pixel even at fractional scaling; a
        # 1-logical-pixel pen would look thicker and blurrier than the resting frame.
        pen = QPen(self.color, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = 0.5 / max(1.0, self.devicePixelRatioF())
        radius = max(0.0, theme.RADIUS_MD - inset)
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()


class _ComboFrame(QObject):
    """Animate a combo box frame between rest, hover/focus and open.

    Combo boxes never change surface on hover: the frame is their affordance. The
    border colour is interpolated in both directions, including open -> closed,
    which plain QSS pseudo-states cannot do. The stroke is painted by a child layer
    instead of rewriting the combo's stylesheet, so an animation frame never
    re-polishes the popup container while it is opening.
    """

    ENTER_MS = 120
    RESTORE_MS = 220

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self.combo = combo
        self.levels = (0.0, 0.0)  # (emphasis, open)
        self._from = self.levels
        self._to = self.levels
        self.layer = _ComboFrameLayer(combo)
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self._settle)
        # view() creates the popup container; its show/hide is the reliable open/close signal.
        self._popup = combo.view().window()
        combo.installEventFilter(self)
        if self._popup is not combo.window():
            self._popup.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # During teardown Qt deletes the popup container before the combo stops
        # receiving focus events; touching the dead wrapper would raise.
        if not (isValid(self.combo) and isValid(self._popup) and isValid(self.layer)):
            return False
        kind = event.type()
        if watched is self._popup and kind in (QEvent.Type.Show, QEvent.Type.Hide):
            self._retarget(opened=kind == QEvent.Type.Show)
        elif watched is self.combo and kind in (QEvent.Type.Enter, QEvent.Type.Leave):
            self._retarget(hovered=kind == QEvent.Type.Enter)
        elif watched is self.combo and kind in (
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
            QEvent.Type.EnabledChange,
            QEvent.Type.DynamicPropertyChange,
        ):
            self._retarget()
        elif watched is self.combo and kind == QEvent.Type.Resize:
            self.layer.setGeometry(self.combo.rect())
        elif watched is self.combo and kind == QEvent.Type.Hide:
            self._animation.stop()
            self.levels = self._from = self._to = (0.0, 0.0)
            self.layer.hide()
        return False

    def _retarget(self, *, hovered: bool | None = None, opened: bool | None = None) -> None:
        if self.combo.property("themeEffectDisabled"):
            return
        combo = self.combo
        if hovered is None:
            hovered = combo.underMouse()
        if opened is None:
            # Cascading selectors do not show QComboBox's native list container.
            custom_open = combo.property("comboPopupOpen")
            opened = bool(custom_open) if custom_open is not None else self._popup.isVisible()
        emphasis = combo.isEnabled() and (hovered or combo.hasFocus())
        target = (1.0 if emphasis else 0.0, 1.0 if opened else 0.0)
        if target == self._to and (
            self._animation.state() == QAbstractAnimation.State.Running or target == self.levels
        ):
            return
        self._animation.stop()
        self._from, self._to = self.levels, target
        receding = sum(target) < sum(self.levels)
        self._animation.setDuration(self.RESTORE_MS if receding else self.ENTER_MS)
        self._animation.start()

    def _step(self, value: object) -> None:
        progress = float(value)  # type: ignore[arg-type]
        self.levels = tuple(  # type: ignore[assignment]
            start + (end - start) * progress
            for start, end in zip(self._from, self._to, strict=True)
        )
        self._paint()

    def _settle(self) -> None:
        self.levels = self._to
        self._paint()

    def frame_color(self) -> QColor:
        rest, emphasis_color, open_color = (QColor(c) for c in theme.combo_frame_colors())
        if self.combo.property("comboFramelessAtRest"):
            # Deliberately frameless pickers (the chat bar model selector) rest invisible.
            rest = QColor(emphasis_color)
            rest.setAlpha(0)
        emphasis, opened = self.levels
        return _mix(_mix(rest, emphasis_color, emphasis), open_color, opened)

    def _paint(self) -> None:
        if self.levels == (0.0, 0.0):
            # At rest the QSS border is already the resting colour.
            self.layer.hide()
            return
        self.layer.color = self.frame_color()
        self.layer.setGeometry(self.combo.rect())
        self.layer.raise_()
        self.layer.show()
        self.layer.update()


def _mix(left: QColor, right: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor.fromRgbF(
        left.redF() + (right.redF() - left.redF()) * amount,
        left.greenF() + (right.greenF() - left.greenF()) * amount,
        left.blueF() + (right.blueF() - left.blueF()) * amount,
        left.alphaF() + (right.alphaF() - left.alphaF()) * amount,
    )


def _inside_combo(widget: QWidget) -> bool:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QComboBox):
            return True
        parent = parent.parentWidget()
    return False


class _HoverSuspension(QObject):
    _TYPES = (
        QAbstractButton,
        QLineEdit,
        QPlainTextEdit,
        QAbstractSpinBox,
        QAbstractItemView,
    )

    def __init__(self, root: QWidget, settings: MaterialSettings) -> None:
        super().__init__(root)
        self.root = root
        self.settings = settings
        self._animations: dict[QWidget, QVariantAnimation] = {}
        self._original_styles: dict[QWidget, str] = {}
        root.installEventFilter(self)
        self.install_new_children()

    def update_settings(self, settings: MaterialSettings) -> None:
        self.settings = settings
        self.install_new_children()
        if not settings.hover_suspend_enabled or theme.backdrop_chrome_active():
            for widget in tuple(self._original_styles):
                self._restore(widget)

    def install_new_children(self) -> None:
        for widget in self.root.findChildren(QWidget):
            if isinstance(widget, QComboBox):
                # Combo frames animate regardless of the hover-suspension material switch.
                if not isinstance(getattr(widget, "_limbowave_combo_frame", None), _ComboFrame):
                    widget._limbowave_combo_frame = _ComboFrame(widget)  # type: ignore[attr-defined]
                continue
            # Spin boxes and editable combos own a small internal line edit; styling it
            # separately consumes its text area and clips the glyphs on hover. Combo
            # popup backgrounds are managed by popup_material, not hover suspension.
            if not isinstance(widget, self._TYPES) or _inside_combo(widget):
                continue
            # 复选框没有自己的表面，悬停只由指示器描边表达（checkbox_style），不铺底色。
            if isinstance(widget, QCheckBox):
                continue
            if isinstance(widget, QLineEdit) and isinstance(
                widget.parentWidget(), QAbstractSpinBox
            ):
                continue
            if not widget.property("limbowaveHoverFilter"):
                widget.setProperty("limbowaveHoverFilter", True)
                widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.root:
            if event.type() == QEvent.Type.ChildAdded:
                QTimer.singleShot(0, self.install_new_children)
            return False
        if (
            not isinstance(watched, self._TYPES)
            or not self.settings.hover_suspend_enabled
            or theme.backdrop_chrome_active()
        ):
            return False
        if event.type() == QEvent.Type.Destroy:
            self._animations.pop(watched, None)
            self._original_styles.pop(watched, None)
            return False
        if event.type() == QEvent.Type.Enter:
            self._animate(watched, suspended=True)
        elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide):
            self._animate(watched, suspended=False)
        return False

    def _animate(self, widget: QWidget, *, suspended: bool) -> None:
        if widget.property("themeEffectDisabled"):
            return
        if isinstance(widget, QAbstractButton) and widget.property("accent"):
            # Accent buttons paint the accent fill as their surface; suspending them
            # to the theme surface would strand TEXT_ON_ACCENT (light themes lock it
            # white) on a light card. Their hover identity is ACCENT_HOVER, not opacity.
            return
        animation = self._animations.get(widget)
        current = float(animation.currentValue()) if animation is not None else 0.0
        if animation is not None:
            animation.stop()
        if widget not in self._original_styles:
            self._original_styles[widget] = widget.styleSheet()
        duration = self.settings.hover_enter_ms if suspended else self.settings.hover_restore_ms
        if duration == 0:
            self._paint_level(widget, 1.0 if suspended else 0.0)
            if not suspended:
                self._restore(widget)
            return
        animation = QVariantAnimation(widget)
        animation.setStartValue(current)
        animation.setEndValue(1.0 if suspended else 0.0)
        animation.setDuration(duration)
        animation.valueChanged.connect(
            lambda value, target=widget: self._paint_level(target, float(value))
        )
        if not suspended:
            animation.finished.connect(lambda target=widget: self._restore(target))
        self._animations[widget] = animation
        animation.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def _paint_level(self, widget: QWidget, level: float) -> None:
        base = self.settings.content_opacity
        opacity = base + (1.0 - base) * max(0.0, min(1.0, level))
        selector = type(widget).__name__
        original = self._original_styles.get(widget, "")
        widget.setStyleSheet(
            original
            + f"\n{selector} {{ background: {theme.qss_alpha(theme.BG_SURFACE, opacity)}; }}"
        )
        effect = widget.graphicsEffect()
        if effect is not None and effect.property(_GLOW_MARKER):
            effect.setEnabled(level < 0.5)

    def _restore(self, widget: QWidget) -> None:
        original = self._original_styles.pop(widget, None)
        if original is not None:
            widget.setStyleSheet(original)
        animation = self._animations.pop(widget, None)
        if animation is not None:
            animation.stop()
        effect = widget.graphicsEffect()
        if effect is not None and effect.property(_GLOW_MARKER):
            effect.setEnabled(True)


def install_hover_suspension(root: QWidget, settings: MaterialSettings) -> None:
    controller = getattr(root, "_limbowave_hover_suspension", None)
    if isinstance(controller, _HoverSuspension):
        controller.update_settings(settings)
    else:
        root._limbowave_hover_suspension = _HoverSuspension(root, settings)  # type: ignore[attr-defined]
