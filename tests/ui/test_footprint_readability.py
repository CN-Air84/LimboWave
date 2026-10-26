"""Reading boards protect text from wallpaper; the hero yields space on scroll."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QAbstractSlider

from limbowave.ui import theme
from limbowave.ui.backdrop import BackdropEngine
from tests.ui.test_footprint_background import (
    gallery_style as gallery_style,
)
from tests.ui.test_footprint_background import (
    show_gallery as _show_gallery,
)


def show_gallery(qtbot, qapp):
    page = _show_gallery(qtbot, qapp)
    # Loading finishes before Qt processes the new layout's scroll-range update.
    qtbot.wait(30)
    return page


def contrast(first, second):
    def luminance(color):
        channels = [c / 255 for c in (color.red(), color.green(), color.blue())]
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(a * b for a, b in zip(channels, (0.2126, 0.7152, 0.0722), strict=True))

    values = sorted((luminance(first), luminance(second)))
    return (values[1] + 0.05) / (values[0] + 0.05)


@pytest.mark.parametrize("palette", ["dark", "light"])
@pytest.mark.parametrize("wallpaper", ["#ffffff", "#000000", "#57bbed"])
def test_reading_boards_keep_text_contrast_on_arbitrary_wallpaper(
    qtbot,
    qapp,
    gallery_style,
    tmp_path,
    palette,
    wallpaper,
):
    theme.set_palette(palette)
    page = show_gallery(qtbot, qapp)
    path = tmp_path / "wall.png"
    image = QImage(950, 760, QImage.Format.Format_RGB32)
    image.fill(QColor(wallpaper))
    assert image.save(str(path))
    engine = BackdropEngine(page)
    assert engine.prepare(str(path), 0)
    page.set_backdrop_engine(engine)
    frame = page.grab().toImage()
    assert frame.pixelColor(6, 90) == QColor(wallpaper)
    for panel in (page._header_panel, page.cards[0]):
        point = panel.mapTo(page, QPoint(12, 40))
        surface = frame.pixelColor(point)
        assert contrast(QColor(theme.TEXT_PRIMARY), surface) >= 7
        assert contrast(QColor(theme.TEXT_SECONDARY), surface) >= 4.5
    list_gap = page._reading_panel.mapTo(page, QPoint(2, 30))
    assert frame.pixelColor(list_gap) == QColor(wallpaper)
    engine.close()


def collapsed(qtbot, page):
    qtbot.waitUntil(lambda: page._title_animation.state() == page._title_animation.State.Stopped)
    qtbot.waitUntil(lambda: page._title_region.height() == 0 or not page._title_region.isVisible())
    qtbot.waitUntil(lambda: not page._title_settling)


def test_scrolling_hides_the_hero_but_keeps_tools_visible(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    before = page._scroll.viewport().height()
    assert page._title_region.isVisible()
    assert page.summary.isVisible() and page._local_hint.isVisible()
    page._scroll.verticalScrollBar().setValue(180)
    collapsed(qtbot, page)
    assert page._title_collapsed
    assert not page.summary.isVisible() and not page._local_hint.isVisible()
    assert page._scroll.viewport().height() > before + 100
    for widget in (
        page.search,
        page.scope_filter,
        page.source_filter,
        page.month_filter,
        page.order,
        page.privacy_button,
        page.refresh_button,
    ):
        assert widget.isVisible() and widget.isEnabled()
    assert page._compact_title.isVisible()
    assert page.privacy_button.visibleRegion().isEmpty()
    page._scroll.verticalScrollBar().setValue(0)
    qtbot.waitUntil(lambda: page._title_region.isVisible() and not page._title_collapsed)
    qtbot.waitUntil(lambda: page._title_animation.state() == page._title_animation.State.Stopped)
    assert page._scroll.viewport().height() == before
    assert page.summary.isVisible() and page._local_hint.isVisible()
    page.privacy_button.click()
    assert page.privacy.isVisible()
    assert "可能随请求发送" in page.privacy.text()


def test_scrollbar_keyboard_and_filter_reset_restore_the_title(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    bar = page._scroll.verticalScrollBar()
    bar.triggerAction(QAbstractSlider.SliderAction.SliderToMaximum)
    collapsed(qtbot, page)
    bar.setFocus()
    qtbot.keyClick(bar, Qt.Key.Key_Home)
    qtbot.waitUntil(lambda: not page._title_collapsed)
    qtbot.waitUntil(lambda: page._title_animation.state() == page._title_animation.State.Stopped)
    qtbot.waitUntil(lambda: not page._title_settling)
    bar.setValue(180)
    collapsed(qtbot, page)
    page.search.setText("does-not-exist")
    qtbot.waitUntil(lambda: not page.cards)
    assert not page._title_collapsed and page._title_region.isVisible()
    assert page.status.isVisible()


def test_small_scroll_ranges_do_not_toggle_after_collapse(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._entries = page._entries[:3]
    page._filter()
    page.resize(950, 630)
    qtbot.wait(30)
    bar = page._scroll.verticalScrollBar()
    assert bar.maximum() > 32
    bar.setValue(bar.maximum())
    collapsed(qtbot, page)
    qtbot.wait(400)
    assert page._title_collapsed
    assert not page._title_region.isVisible()
    assert len(page.cards) == 3


def test_dispose_stops_title_motion(qtbot, qapp, gallery_style):
    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page._scroll.verticalScrollBar().setValue(180)
    assert page._title_animation.state() == page._title_animation.State.Running
    page.dispose()
    assert page._title_animation.state() == page._title_animation.State.Stopped


@pytest.mark.parametrize("width", [720, 1080])
def test_return_button_is_rightmost_and_returns_after_header_undocks(
    qtbot,
    qapp,
    gallery_style,
    width,
):
    from PySide6.QtWidgets import QPushButton

    theme.set_palette("dark")
    page = show_gallery(qtbot, qapp)
    page.resize(width, 760)
    qtbot.wait(30)
    back = page.findChild(QPushButton, "footprintReturn")
    assert back is not None and back.text() == "返回对话"
    assert back.property("flat") is True
    before = back.mapTo(page, QPoint(0, 0))
    for other in (page.refresh_button, page.privacy_button):
        assert before.x() > other.mapTo(page, QPoint(other.width(), 0)).x()
    assert page.width() - before.x() - back.width() <= 48
    page._scroll.verticalScrollBar().setValue(180)
    collapsed(qtbot, page)
    assert back.visibleRegion().isEmpty()
    page._scroll.verticalScrollBar().setValue(0)
    qtbot.waitUntil(lambda: page._header_motion.state() == page._header_motion.State.Stopped)
    qtbot.waitUntil(lambda: not page._title_settling)
    assert not back.visibleRegion().isEmpty() and back.isEnabled()
    assert back.mapTo(page, QPoint(0, 0)).x() == before.x()
    with qtbot.waitSignal(page.close_requested):
        qtbot.mouseClick(back, Qt.MouseButton.LeftButton)
