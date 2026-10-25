"""Attachment previews fit the viewport on every resize, without degrading the source."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QLabel, QScrollArea
from pytestqt.qtbot import QtBot

from limbowave.ui.sent_attachments import ImagePreviewDialog, SentAttachment


def _preview(qtbot: QtBot, source: QPixmap) -> tuple[ImagePreviewDialog, QScrollArea, QLabel]:
    dialog = ImagePreviewDialog(SentAttachment("photo.png", "PNG", source))
    qtbot.addWidget(dialog)
    dialog.show()
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    image = scroll.widget()
    assert isinstance(image, QLabel)
    return dialog, scroll, image


def _assert_fitted(source: QPixmap, scroll: QScrollArea, image: QLabel) -> None:
    pixmap = image.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    expected = source.size().scaled(
        image.contentsRect().size() * image.devicePixelRatioF(),
        Qt.AspectRatioMode.KeepAspectRatio,
    )
    assert pixmap.size() == expected
    assert pixmap.devicePixelRatio() == image.devicePixelRatioF()
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() == 0
    assert image.alignment() == Qt.AlignmentFlag.AlignCenter


@pytest.mark.parametrize("source_size", [(1600, 900), (900, 1600), (800, 800), (80, 60)])
@pytest.mark.parametrize("source_dpr", [1.0, 2.0])
def test_preview_tracks_growing_and_shrinking_viewport(
    qtbot: QtBot, source_size: tuple[int, int], source_dpr: float
) -> None:
    source = QPixmap(*source_size)
    source.fill(Qt.GlobalColor.red)
    source.setDevicePixelRatio(source_dpr)
    original = source.toImage()
    dialog, scroll, image = _preview(qtbot, source)
    previous_size = QSize()
    for width, height in [(320, 240), (720, 540), (240, 180), (600, 280), (280, 600)]:
        dialog.resize(width, height)
        qtbot.waitUntil(lambda: scroll.viewport().size() == image.size())
        _assert_fitted(source, scroll, image)
        current_size = image.pixmap().size()
        if (width, height) == (720, 540):
            assert current_size.width() > previous_size.width()
            assert current_size.height() > previous_size.height()
        previous_size = current_size
    assert source.toImage() == original
    assert dialog.windowTitle() == "photo.png"
    assert any(label.text() == "PNG" for label in dialog.findChildren(QLabel))


def test_preview_rescales_from_original_after_shrinking(qtbot: QtBot) -> None:
    pattern = QImage(192, 128, QImage.Format.Format_RGB32)
    for y in range(pattern.height()):
        for x in range(pattern.width()):
            pattern.setPixelColor(x, y, QColor((x * 37) % 256, (y * 53) % 256, (x + y) % 256))
    source = QPixmap.fromImage(pattern)
    dialog, scroll, image = _preview(qtbot, source)
    for size in [QSize(640, 480), QSize(180, 140), QSize(720, 540), QSize(640, 480)]:
        dialog.resize(size)
        qtbot.waitUntil(lambda: scroll.viewport().size() == image.size())
        expected = source.scaled(
            image.contentsRect().size() * image.devicePixelRatioF(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        expected.setDevicePixelRatio(image.devicePixelRatioF())
        assert image.pixmap().toImage() == expected.toImage()


def test_preview_fits_maximized_and_restored_window(qtbot: QtBot) -> None:
    source = QPixmap(1600, 900)
    source.fill(Qt.GlobalColor.blue)
    dialog, scroll, image = _preview(qtbot, source)
    dialog.resize(320, 240)
    qtbot.waitUntil(lambda: scroll.viewport().size() == image.size())
    normal_size = image.size()
    dialog.showMaximized()
    qtbot.waitUntil(lambda: image.width() > normal_size.width())
    _assert_fitted(source, scroll, image)
    dialog.showNormal()
    qtbot.waitUntil(lambda: image.size() == normal_size)
    _assert_fitted(source, scroll, image)
