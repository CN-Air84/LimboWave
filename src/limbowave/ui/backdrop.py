"""Bounded, generation-safe background compositor for the main workspace.

Image decode, scaling and Gaussian blur run in one worker. The GUI thread only
installs QPixmaps and draws source rectangles, so message scrolling and hover do
not trigger image processing.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PIL import Image, ImageColor, ImageFilter, ImageOps
from PySide6.QtCore import QObject, QPoint, QRect, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from limbowave.domain.appearance import BackgroundSettings

MAX_RENDER_WIDTH = 3840
MAX_RENDER_HEIGHT = 2160
MAX_SOURCE_PIXELS = 64_000_000
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_CACHE_BYTES = 192 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _RenderKey:
    path: str
    modified_ns: int
    file_size: int
    width: int
    height: int
    fit_mode: str
    position: str
    image_opacity: int
    mask_color: str
    mask_opacity: int
    base_color: str
    radius: int


def _factors(position: str) -> tuple[float, float]:
    horizontal = (
        0.0
        if position.endswith("left") or position == "left"
        else 1.0
        if position.endswith("right") or position == "right"
        else 0.5
    )
    vertical = (
        0.0
        if position.startswith("top") or position == "top"
        else 1.0
        if position.startswith("bottom") or position == "bottom"
        else 0.5
    )
    return horizontal, vertical


def _scaled_size(
    source: tuple[int, int], target: tuple[int, int], *, cover: bool
) -> tuple[int, int]:
    source_width, source_height = source
    target_width, target_height = target
    factor = (
        max(target_width / source_width, target_height / source_height)
        if cover
        else min(target_width / source_width, target_height / source_height)
    )
    return max(1, round(source_width * factor)), max(1, round(source_height * factor))


def _place(canvas: Image.Image, image: Image.Image, position: str) -> None:
    horizontal, vertical = _factors(position)
    left = round((canvas.width - image.width) * horizontal)
    top = round((canvas.height - image.height) * vertical)
    canvas.alpha_composite(image, (left, top))


def _compose(
    path: str,
    output: tuple[int, int],
    settings: BackgroundSettings,
    base_color: str,
) -> Image.Image:
    with Image.open(path) as opened:
        if opened.width * opened.height > MAX_SOURCE_PIXELS:
            raise ValueError("background image exceeds pixel limit")
        source = ImageOps.exif_transpose(opened).convert("RGBA")
    canvas = Image.new("RGBA", output, (*ImageColor.getrgb(base_color), 255))
    if settings.fit_mode == "stretch":
        layer = source.resize(output, Image.Resampling.LANCZOS)
        _place(canvas, layer, "center")
    elif settings.fit_mode == "tile":
        layer = source
        horizontal, vertical = _factors(settings.position)
        start_x = round(-layer.width * horizontal)
        start_y = round(-layer.height * vertical)
        while start_x > 0:
            start_x -= layer.width
        while start_y > 0:
            start_y -= layer.height
        for top in range(start_y, output[1], layer.height):
            for left in range(start_x, output[0], layer.width):
                canvas.alpha_composite(layer, (left, top))
    else:
        scaled = _scaled_size(source.size, output, cover=settings.fit_mode == "cover")
        layer = source.resize(scaled, Image.Resampling.LANCZOS)
        _place(canvas, layer, settings.position)
    if settings.image_opacity < 1.0:
        # Recompose from the base so image opacity does not make the final frame transparent.
        image_layer = canvas.copy()
        image_layer.putalpha(round(settings.image_opacity * 255))
        base = Image.new("RGBA", output, (*ImageColor.getrgb(base_color), 255))
        canvas = Image.alpha_composite(base, image_layer)
    if settings.mask_opacity:
        mask = Image.new(
            "RGBA",
            output,
            (*ImageColor.getrgb(settings.mask_color), round(settings.mask_opacity * 255)),
        )
        canvas = Image.alpha_composite(canvas, mask)
    return canvas


def _edge_extend(image: Image.Image, padding: int) -> Image.Image:
    if padding <= 0:
        return image.copy()
    output = ImageOps.expand(image, border=padding)
    width, height = image.size
    output.paste(image.crop((0, 0, width, 1)).resize((width, padding)), (padding, 0))
    output.paste(
        image.crop((0, height - 1, width, height)).resize((width, padding)),
        (padding, padding + height),
    )
    output.paste(image.crop((0, 0, 1, height)).resize((padding, height)), (0, padding))
    output.paste(
        image.crop((width - 1, 0, width, height)).resize((padding, height)),
        (padding + width, padding),
    )
    for point, destination in (
        ((0, 0), (0, 0)),
        ((width - 1, 0), (padding + width, 0)),
        ((0, height - 1), (0, padding + height)),
        ((width - 1, height - 1), (padding + width, padding + height)),
    ):
        output.paste(Image.new("RGBA", (padding, padding), image.getpixel(point)), destination)
    return output


def _blur(image: Image.Image, radius: int) -> Image.Image:
    if radius <= 0:
        return image.copy()
    padding = max(1, math.ceil(radius * 3))
    expanded = _edge_extend(image, padding)
    return expanded.filter(ImageFilter.GaussianBlur(radius)).crop(
        (padding, padding, padding + image.width, padding + image.height)
    )


def _render_payload(
    path: str,
    output: tuple[int, int],
    settings: BackgroundSettings,
    base_color: str,
    radii: tuple[int, ...],
) -> dict[int, tuple[bytes, int, int]]:
    composed = _compose(path, output, settings, base_color)
    payload: dict[int, tuple[bytes, int, int]] = {}
    for radius in radii:
        # Pillow extends edge pixels for GaussianBlur; uniform-edge regression tests
        # protect against dark halos without allocating a second padded 4K surface.
        frame = composed.filter(ImageFilter.GaussianBlur(radius)) if radius else composed
        payload[radius] = (frame.tobytes("raw", "RGBA"), frame.width, frame.height)
    return payload


class BackdropEngine(QObject):
    changed = Signal()
    _payload_ready = Signal(object)

    def __init__(self, workspace: QWidget) -> None:
        super().__init__(workspace)
        self._workspace = workspace
        self._frame = QPixmap()
        self._frames: dict[int, QPixmap] = {}
        self._cache: OrderedDict[_RenderKey, QPixmap] = OrderedDict()
        self._cache_bytes = 0
        self._generation = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="limbowave-theme")
        self._future: Future[dict[int, tuple[bytes, int, int]]] | None = None
        self._payload_ready.connect(self._install_payload)
        self._pending: tuple[int, tuple[_RenderKey, ...]] | None = None
        self.render_count = 0

    @property
    def active(self) -> bool:
        return bool(self._frames)

    def clear(self) -> None:
        self._generation += 1
        self._frame = QPixmap()
        self._frames.clear()
        self._pending = None
        self.changed.emit()

    def close(self) -> None:
        self._generation += 1
        if self._future is not None:
            self._future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def prepare(self, image_path: str, radius: int) -> bool:
        """Synchronous compatibility entry point used by focused tests."""
        settings = BackgroundSettings(asset=image_path)
        request = self._request_parts(image_path, settings, "#000000", (radius,))
        if request is None:
            self.clear()
            return False
        path, output, radii, keys = request
        cached = {key.radius: self._cache[key] for key in keys if key in self._cache}
        if len(cached) == len(keys):
            self._frames = cached
            self._frame = cached[radii[0]]
            for key in keys:
                self._cache.move_to_end(key)
            self.changed.emit()
            return True
        payload = _render_payload(path, output, settings, "#000000", radii)
        self._install(0, keys, payload, force=True)
        return True

    def request(
        self,
        image_path: str,
        settings: BackgroundSettings,
        base_color: str,
        radii: Iterable[float],
    ) -> bool:
        request = self._request_parts(image_path, settings, base_color, radii)
        if request is None:
            self.clear()
            return False
        path, output, normalized_radii, keys = request
        cached = {key.radius: self._cache[key] for key in keys if key in self._cache}
        if len(cached) == len(keys):
            self._frames = cached
            self._frame = cached[normalized_radii[0]]
            for key in keys:
                self._cache.move_to_end(key)
            self.changed.emit()
            return True
        if (
            self._pending is not None
            and self._pending[1] == keys
            and self._future is not None
            and not self._future.done()
        ):
            return True
        if self._future is not None and not self._future.done():
            self._future.cancel()
        self._generation += 1
        generation = self._generation
        self._pending = generation, keys
        future = self._executor.submit(
            _render_payload, path, output, settings, base_color, normalized_radii
        )
        self._future = future

        def completed(done: Future[dict[int, tuple[bytes, int, int]]]) -> None:
            try:
                payload = done.result()
            except Exception:
                payload = {}
            self._payload_ready.emit((generation, keys, payload))

        future.add_done_callback(completed)
        return True

    def _request_parts(
        self,
        image_path: str,
        settings: BackgroundSettings,
        base_color: str,
        radii: Iterable[float],
    ) -> tuple[str, tuple[int, int], tuple[int, ...], tuple[_RenderKey, ...]] | None:
        try:
            path = Path(image_path)
            stat = path.stat()
            if not path.is_file() or stat.st_size > MAX_IMAGE_BYTES:
                return None
            with Image.open(path) as image:
                if image.width * image.height > MAX_SOURCE_PIXELS:
                    return None
        except (OSError, ValueError):
            return None
        width, height = self._workspace.width(), self._workspace.height()
        if width <= 0 or height <= 0:
            return None
        factor = min(1.0, MAX_RENDER_WIDTH / width, MAX_RENDER_HEIGHT / height)
        output = max(1, round(width * factor)), max(1, round(height * factor))
        normalized = tuple(dict.fromkeys(max(0, min(64, round(value * factor))) for value in radii))
        if not normalized:
            normalized = (0,)
        resolved = str(path.resolve())
        keys = tuple(
            _RenderKey(
                resolved,
                stat.st_mtime_ns,
                stat.st_size,
                output[0],
                output[1],
                settings.fit_mode,
                settings.position,
                round(settings.image_opacity * 1000),
                settings.mask_color,
                round(settings.mask_opacity * 1000),
                base_color,
                radius,
            )
            for radius in normalized
        )
        return resolved, output, normalized, keys

    def _install_payload(self, result: object) -> None:
        generation, keys, payload = cast(
            tuple[int, tuple[_RenderKey, ...], dict[int, tuple[bytes, int, int]]], result
        )
        self._install(generation, keys, payload)

    def _install(
        self,
        generation: int,
        keys: tuple[_RenderKey, ...],
        payload: dict[int, tuple[bytes, int, int]],
        *,
        force: bool = False,
    ) -> None:
        if not force and generation != self._generation:
            return
        if not payload:
            if not self.active:
                self.clear()
            return
        installed: dict[int, QPixmap] = {}
        for key in keys:
            raw, width, height = payload[key.radius]
            image = QImage(raw, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
            pixmap = QPixmap.fromImage(image)
            installed[key.radius] = pixmap
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= previous.width() * previous.height() * 4
            self._cache[key] = pixmap
            self._cache_bytes += pixmap.width() * pixmap.height() * 4
        while self._cache_bytes > MAX_CACHE_BYTES and len(self._cache) > len(installed):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.width() * evicted.height() * 4
        self._frames = installed
        self._frame = next(iter(installed.values()))
        self.render_count += 1
        self.changed.emit()

    def paint(
        self,
        widget: QWidget,
        painter: QPainter,
        *,
        tint: str,
        radius: float | None = None,
        fallback: str | None = None,
    ) -> None:
        if not self.active:
            return
        if fallback is not None:
            # Independent popups may extend beyond the workspace's wallpaper.
            painter.fillRect(widget.rect(), QColor(fallback))
        requested = max(0, round(radius or 0))
        chosen = min(self._frames, key=lambda value: abs(value - requested))
        frame = self._frames[chosen]
        # A combo/menu has a parent QObject but a separate native window;
        # global coordinates work for both it and ordinary workspace children.
        origin = self._workspace.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
        scale_x = frame.width() / max(1, self._workspace.width())
        scale_y = frame.height() / max(1, self._workspace.height())
        source = QRect(
            round(origin.x() * scale_x),
            round(origin.y() * scale_y),
            round(widget.width() * scale_x),
            round(widget.height() * scale_y),
        )
        painter.drawPixmap(widget.rect(), frame, source)
        painter.fillRect(widget.rect(), QColor(tint))
