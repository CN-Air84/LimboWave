"""Popup surfaces sample the workspace's cached wallpaper, never the screen.

Qt menus and combo containers are separate windows. Making them transparent is
not enough: their backing store must paint the matching patch of the shared
blurred frame before Qt paints the items. No snapshots or blur jobs run here.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QEvent, QObject, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from limbowave.domain.appearance import AppearanceTheme
from limbowave.ui.backdrop import BackdropEngine

SurfaceKind = Literal["card", "selection"]
_MARKER = "limbowavePopupMaterial"


class PopupMaterials(QObject):
    """Window-local material context; only registered surfaces receive updates."""

    changed = Signal()

    def __init__(
        self,
        owner: QWidget,
        workspace: QWidget,
        engine: BackdropEngine,
        definition: AppearanceTheme,
    ) -> None:
        super().__init__(owner)
        self.workspace = workspace
        self.engine = engine
        self.definition = definition
        workspace.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self.changed.emit()
        return False

    def set_theme(self, definition: AppearanceTheme) -> None:
        self.definition = definition
        self.changed.emit()

    def enabled(self, kind: SurfaceKind) -> bool:
        materials = self.definition.materials
        category_enabled = (
            materials.cards_enabled if kind == "card" else materials.control_enabled("selections")
        )
        return (
            self.workspace.isVisible()
            and self.engine.active
            and materials.content_enabled
            and category_enabled
        )


class _PopupMaterial(QObject):
    def __init__(
        self, target: QWidget, owner: QWidget, kind: SurfaceKind, radius: float, selector: str
    ) -> None:
        super().__init__(target)
        self.target = target
        self.owner = owner
        self.kind = kind
        self.radius = radius
        self._context: PopupMaterials | None = None
        self._active = False
        # Local, opt-in rule: normal popups retain their solid QSS/palette. The
        # caller supplies an ID selector only when its existing surface uses one.
        target.setStyleSheet(
            target.styleSheet() + f'\n{selector}[{_MARKER}="true"] {{ background: transparent; }}'
        )
        target.installEventFilter(self)
        self.refresh()

    def _bind_context(self) -> None:
        owner: QWidget | None = self.owner
        context = None
        while owner is not None and isValid(owner):
            candidate = getattr(owner, "_popup_materials", None)
            if isinstance(candidate, PopupMaterials):
                context = candidate
                break
            owner = owner.parentWidget()
        if context is self._context:
            return
        if self._context is not None and isValid(self._context):
            self._context.changed.disconnect(self.refresh)
        self._context = context
        if context is not None:
            context.changed.connect(self.refresh)

    def refresh(self) -> None:
        if not isValid(self.target):
            return
        self._bind_context()
        active = self._context is not None and self._context.enabled(self.kind)
        if active != self._active:
            self._active = active
            self.target.setProperty(_MARKER, active)
            style = self.target.style()
            style.unpolish(self.target)
            style.polish(self.target)
        self.target.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.ParentChange):
            self.refresh()
        elif kind == QEvent.Type.Move and self._active:
            # Re-sample at the new position; this is only a source-rect blit.
            self.target.update()
        elif kind == QEvent.Type.Paint and self._active:
            self._paint()
        # Qt still paints the frame, items, selection and text at full opacity.
        return False

    def _paint(self) -> None:
        context = self._context
        if context is None or not isValid(context) or not context.engine.active:
            return
        definition = context.definition
        color = definition.colors.card if self.kind == "card" else definition.colors.component
        tint = QColor(color)
        tint.setAlphaF(definition.materials.content_opacity)
        painter = QPainter(self.target)
        if self.radius:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.target.rect()), self.radius, self.radius)
            painter.setClipPath(path)
        context.engine.paint(
            self.target,
            painter,
            tint=tint.name(QColor.NameFormat.HexArgb),
            radius=definition.materials.content_blur_radius,
            fallback=color,
        )
        painter.end()


def install_popup_material(
    target: QWidget,
    *,
    owner: QWidget | None = None,
    kind: SurfaceKind = "selection",
    radius: float = 0.0,
    selector: str = "QWidget",
) -> None:
    """Install once, even when a combo reuses its container on every opening."""
    existing = getattr(target, "_popup_material", None)
    if isinstance(existing, _PopupMaterial):
        existing.refresh()
        return
    target._popup_material = _PopupMaterial(  # type: ignore[attr-defined]
        target, owner if owner is not None else target, kind, radius, selector
    )
