"""Resize must repaint cached wallpaper without restyling the conversation."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from limbowave.domain.appearance import BackgroundSettings
from limbowave.ui import backdrop
from limbowave.ui.backdrop import BackdropEngine
from limbowave.ui.main_window import MainWindow


class _StyleChanges(QObject):
    def __init__(self, root: QWidget) -> None:
        super().__init__(root)
        self.count = 0
        for widget in (root, *root.findChildren(QWidget)):
            widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.StyleChange:
            self.count += 1
        return False


def test_resize_only_repaints_materials(qtbot: QtBot, tmp_path: Path) -> None:
    image = tmp_path / "wall.png"
    Image.new("RGB", (320, 240), "red").save(image)
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    window.set_backdrop(str(image), 16)
    for index in range(20):
        window.chat.add_user_message(f"Message {index}")
    preview = window.prepare_history_preview()
    preview.add_user_message("History")
    qtbot.wait(250)
    engine = window._backdrop_engine
    count = engine.render_count
    styles = _StyleChanges(window)
    for step in range(8):
        window.resize(1300 + step * 5, 810 + step * 3)
        qtbot.wait(10)
        assert engine.active
        assert engine.render_count == count
    qtbot.waitUntil(lambda: engine.render_count > count, timeout=3000)
    assert engine.render_count == count + 1
    assert styles.count == 0, f"resize delivered {styles.count} StyleChange events"
    assert engine._frame.size() == window._workspace.size()
    window.set_backdrop("", 0)
    assert not engine.active
    assert styles.count > 0  # Removing wallpaper still updates material styles.


def test_cached_request_supersedes_running_render(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    red, blue = tmp_path / "red.png", tmp_path / "blue.png"
    Image.new("RGB", (80, 60), "red").save(red)
    Image.new("RGB", (80, 60), "blue").save(blue)
    workspace = QWidget()
    qtbot.addWidget(workspace)
    workspace.resize(80, 60)
    engine = BackdropEngine(workspace)
    assert engine.prepare(str(red), 0)
    original = backdrop._render_payload
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(backdrop, "_render_payload", blocked)
    try:
        assert engine.request(str(blue), BackgroundSettings(), "#000000", (0,))
        assert entered.wait(2)
        engine._future.add_done_callback(lambda _: delivered.set())
        assert engine.request(str(red), BackgroundSettings(), "#000000", (0,))
        release.set()
        assert delivered.wait(2)
        QApplication.processEvents()
        assert engine._frame.toImage().pixelColor(0, 0).name() == "#ff0000"
        assert engine.render_count == 1
    finally:
        release.set()
        engine.close()


def test_completed_request_waiting_for_gui_is_deduplicated(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "wall.png"
    Image.new("RGB", (80, 60), "red").save(image)
    workspace = QWidget()
    qtbot.addWidget(workspace)
    workspace.resize(80, 60)
    engine = BackdropEngine(workspace)
    original = backdrop._render_payload
    release, delivered = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(backdrop, "_render_payload", blocked)
    try:
        engine.request(str(image), BackgroundSettings(), "#000000", (0,))
        first = engine._future
        first.add_done_callback(lambda _: delivered.set())
        release.set()
        assert delivered.wait(2)
        engine.request(str(image), BackgroundSettings(), "#000000", (0,))
        assert engine._future is first
        qtbot.waitUntil(lambda: engine.active)
        assert engine.render_count == 1
    finally:
        release.set()
        engine.close()


def test_resize_discards_old_result_during_debounce(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "wall.png"
    Image.new("RGB", (80, 60), "red").save(image)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.set_backdrop(str(image), 12)
    qtbot.wait(250)
    engine = window._backdrop_engine
    previous = engine._frame.cacheKey()
    count = engine.render_count
    original = backdrop._render_payload
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(backdrop, "_render_payload", blocked)
    try:
        window.resize(window.width() + 40, window.height() + 20)
        window._backdrop_resize.stop()
        window._prepare_backdrop()
        assert entered.wait(2)
        engine._future.add_done_callback(lambda _: delivered.set())
        window.resize(window.width() + 40, window.height() + 20)
        window._backdrop_resize.stop()  # Hold the debounce boundary deterministically.
        release.set()
        assert delivered.wait(2)
        QApplication.processEvents()
        assert engine._frame.cacheKey() == previous
        assert engine.render_count == count
        window._prepare_backdrop()
        qtbot.waitUntil(lambda: engine.render_count == count + 1, timeout=3000)
        assert engine._frame.size() == window._workspace.size()
    finally:
        release.set()
        engine.close()


def test_cancelled_composition_skips_blur_and_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()

    def compose(*args):
        cancelled.set()
        return Image.new("RGBA", (80, 60), "red")

    def unexpected_blur(*args, **kwargs):
        pytest.fail("obsolete frame must not start expensive blur")

    monkeypatch.setattr(backdrop, "_compose", compose)
    monkeypatch.setattr(Image.Image, "filter", unexpected_blur)
    assert backdrop._render_payload(
        "wall.png", (80, 60), BackgroundSettings(), "#000000", (12, 24),
        cancelled=cancelled,
    ) == {}


def test_failed_render_can_retry_same_request(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "wall.png"
    Image.new("RGB", (80, 60), "red").save(image)
    workspace = QWidget()
    qtbot.addWidget(workspace)
    workspace.resize(80, 60)
    engine = BackdropEngine(workspace)
    engine.prepare(str(image), 0)
    original = backdrop._render_payload

    def broken(*args, **kwargs):
        raise OSError("decode failed")

    try:
        monkeypatch.setattr(backdrop, "_render_payload", broken)
        engine.request(str(image), BackgroundSettings(), "#000000", (12,))
        qtbot.waitUntil(lambda: engine._pending is None)
        assert engine.active
        monkeypatch.setattr(backdrop, "_render_payload", original)
        engine.request(str(image), BackgroundSettings(), "#000000", (12,))
        qtbot.waitUntil(lambda: 12 in engine._frames)
    finally:
        engine.close()


def test_new_frame_repaints_existing_material_surfaces(
    qtbot: QtBot, tmp_path: Path,
) -> None:
    red, blue = tmp_path / "red.png", tmp_path / "blue.png"
    Image.new("RGB", (80, 60), "red").save(red)
    Image.new("RGB", (80, 60), "blue").save(blue)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.set_backdrop(str(red), 12)
    qtbot.wait(250)
    engine = window._backdrop_engine
    surfaces = (window.sidebar, window.chat, window.chat._composer)

    def colors():
        frame = window.grab().toImage()
        return [
            frame.pixelColor(pane.mapTo(window, QPoint(8, pane.height() // 2)))
            for pane in surfaces
        ]

    before = colors()
    styles = _StyleChanges(window)
    count = engine.render_count
    definition = window._appearance_theme
    engine.request(str(blue), definition.background, definition.colors.background, (12,))
    qtbot.waitUntil(lambda: engine.render_count > count)
    after = colors()
    assert styles.count == 0
    for previous, current in zip(before, after, strict=True):
        assert previous.red() > previous.blue()
        assert current.blue() > current.red()
