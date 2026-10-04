"""Theme-aware logo with an unskippable draw/hold/erase loading sequence."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from PySide6.QtCore import QEasingCurve, QElapsedTimer, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QHideEvent, QPainter, QPaintEvent, QShowEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from limbowave.ui import theme


class OutlineLogo(QWidget):
    """Keep original SVG contours and animate their dash offsets, never solid fills.

    A bounded frame clock deliberately resumes after GUI-thread stalls instead of
    skipping to the end. Backend readiness can request erasure, but cannot cut short
    the drawing stage. Slow work holds the fully drawn mark until readiness arrives.
    """

    finished = Signal()
    ASSET_PATH = Path(__file__).with_name("assets") / "limbowave-logo-outline.svg"
    ASPECT_RATIO = 975 / 529
    BLACKOUT_DURATION_MS = 100
    DRAW_DURATION_MS = 1800
    ERASE_DURATION_MS = 650
    MAX_FRAME_MS = 32

    def __init__(self, parent: QWidget | None = None, *, on_black: bool = False) -> None:
        super().__init__(parent)
        self._on_black = on_black
        self.setObjectName("loginOutlineLogo")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: transparent;")
        self.setAccessibleName("LimboWave")
        root = ElementTree.parse(self.ASSET_PATH).getroot()
        self._view_box = root.attrib["viewBox"]
        self._contours = tuple(
            (path.attrib["d"], float(path.attrib["data-length"]) + 1.0)
            for path in root.findall(".//{http://www.w3.org/2000/svg}path")
        )
        self._progress = 1.0
        self._erasure = 0.0
        self._phase = "idle"
        self._finish_requested = False
        self._elapsed_ms = 0
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance)
        self._easing = QEasingCurve(QEasingCurve.Type.InOutSine)
        self._render_key: tuple[float, float, str, float] | None = None
        self._renderer = QSvgRenderer(self)
        self._update_renderer()

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, value))
        self._update_renderer()
        self.update()

    def start_loading(self, *, defer_until_shown: bool = False) -> None:
        if self._phase in ("drawing", "holding", "erasing"):
            return
        self._finish_requested = False
        self._erasure = 0.0
        self._set_progress(0.0)
        self._begin_phase("drawing")
        if defer_until_shown:
            self._timer.stop()

    def finish_loading(self) -> None:
        """Allow erasure once the complete path has been drawn, never before."""
        self._finish_requested = True
        if self._phase == "holding":
            self._begin_phase("erasing")

    def _begin_phase(self, phase: str) -> None:
        self._phase = phase
        self._elapsed_ms = -self.BLACKOUT_DURATION_MS if phase == "drawing" else 0
        self._clock.start()
        self._timer.start()

    def _advance(self) -> None:
        if self._phase not in ("drawing", "erasing"):
            return
        self._elapsed_ms += min(self.MAX_FRAME_MS, self._clock.restart())
        duration = (
            self.DRAW_DURATION_MS if self._phase == "drawing" else self.ERASE_DURATION_MS
        )
        fraction = max(0.0, min(1.0, self._elapsed_ms / duration))
        progress = self._easing.valueForProgress(fraction)
        if self._phase == "drawing":
            self._set_progress(progress)
        elif self._phase == "erasing":
            self._erasure = progress
            self._update_renderer()
            self.update()
        if fraction < 1.0:
            return
        self._timer.stop()
        if self._phase == "drawing":
            self._phase = "holding"
            if self._finish_requested:
                self._begin_phase("erasing")
        elif self._phase == "erasing":
            self._phase = "finished"
            self.finished.emit()

    def cancel_loading(self) -> None:
        """Abort an invalid-password attempt without announcing successful completion."""
        self._timer.stop()
        self._phase = "idle"
        self._finish_requested = False
        self._elapsed_ms = 0
        self._erasure = 0.0
        self.hide()
        self._set_progress(1.0)

    def settle(self) -> None:
        """Restore the idle mark, but never truncate an active loading sequence."""
        if self._phase in ("drawing", "holding", "erasing"):
            return
        self._timer.stop()
        self._phase = "idle"
        self._erasure = 0.0
        self._set_progress(1.0)

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._phase in ("drawing", "erasing"):
            self._clock.start()
            self._timer.start()

    def _update_renderer(self) -> None:
        # The loading scene stays black even under light/custom application themes.
        color = "#e6e9ef" if self._on_black else QColor(theme.TEXT_SECONDARY).name()
        # Keep thin outlines readable in compact windows and sharp on high-DPI displays.
        stroke = max(2.6, 1.05 * 975 / max(1, self.width()))
        key = (self._progress, self._erasure, color, stroke)
        if key == self._render_key:
            return
        paths: list[str] = []
        for index, (data, length) in enumerate(self._contours):
            delay = 0.36 * index / max(1, len(self._contours) - 1)
            reveal = max(0.0, min(1.0, (self._progress - delay) / 0.64))
            erased = max(0.0, min(1.0, (self._erasure - delay) / 0.64))
            if reveal <= 0.0 or erased >= 1.0:
                continue
            dash = (
                f' stroke-dasharray="{length:.3f} {length:.3f}"'
                f' stroke-dashoffset="{length * (1.0 - reveal):.3f}"'
                if reveal < 1.0 else ""
            )
            if erased > 0.0:
                dash = (
                    f' stroke-dasharray="{length:.3f} {length:.3f}"'
                    f' stroke-dashoffset="{-length * erased:.3f}"'
                )
            paths.append(f'<path d="{data}"{dash}/>')
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{self._view_box}">'
            f'<g fill="none" stroke="{color}" stroke-opacity="0.68"'
            f' stroke-width="{stroke:.3f}" stroke-linecap="round" stroke-linejoin="round">'
            + "".join(paths) + "</g></svg>"
        )
        self._renderer.load(svg.encode("utf-8"))
        self._render_key = key

    def paintEvent(self, event: QPaintEvent) -> None:
        self._update_renderer()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._renderer.render(painter, QRectF(self.rect()))
