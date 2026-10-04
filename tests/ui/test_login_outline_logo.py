"""The login wordmark stays outline-only throughout its one-shot reveal."""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from pytestqt.qtbot import QtBot

from limbowave.ui import theme
from limbowave.ui.login_page import LoginPage
from limbowave.ui.outline_logo import OutlineLogo


def _frame(logo: OutlineLogo) -> QImage:
    image = QImage(488, 265, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    logo._renderer.render(painter, QRectF(0, 0, 488, 265))
    painter.end()
    return image


def _ink(image: QImage) -> int:
    return sum(alpha > 0 for alpha in bytes(image.constBits())[3::4])


def test_outline_asset_preserves_all_original_contours() -> None:
    source = Path(__file__).parents[2] / "docs/branding/limbowave-logo-transparent.svg"
    original = ElementTree.parse(source)
    outlined = ElementTree.parse(OutlineLogo.ASSET_PATH)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    originals = original.findall(".//svg:path", ns)
    outlines = outlined.findall(".//svg:path", ns)
    original_contours = [
        contour.strip()
        for path in originals
        for contour in re.findall(r"M[^M]+", path.attrib["d"])
    ]
    assert [path.attrib["d"] for path in outlines] == original_contours
    assert all(float(path.attrib["data-length"]) > 0 for path in outlines)
    assert outlined.find(".//svg:g", ns).attrib["fill"] == "none"
    assert not outlined.findall(".//svg:rect", ns)


def test_logo_draws_more_edges_without_filling_letters(qtbot: QtBot) -> None:
    logo = OutlineLogo()
    qtbot.addWidget(logo)
    assert logo._renderer.isValid()
    logo._set_progress(0.0)
    assert _ink(_frame(logo)) == 0
    logo._set_progress(0.45)
    partial = _ink(_frame(logo))
    logo.settle()
    image = _frame(logo)
    assert 0 < partial < _ink(image) < image.width() * image.height() * 0.08
    # Inside the vertical l, inside the b counter, and outside all lettering.
    for x, y in ((110, 230), (620, 317), (400, 150)):
        px = round((x - 68) / 975 * image.width())
        py = round((y - 85) / 529 * image.height())
        assert image.pixelColor(px, py).alpha() == 0


@pytest.mark.parametrize("width,height", [(1280, 800), (800, 600), (640, 480), (320, 240)])
def test_loading_logo_is_centered_at_every_window_size(
    qtbot: QtBot, width: int, height: int
) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(width, height)
    page.show()
    assert not page._logo.isVisible()
    page.request_exit()
    logo = page._logo
    qtbot.waitUntil(logo.isVisible, timeout=800)
    assert page.rect().contains(logo.geometry())
    assert abs(logo.geometry().center().x() - page.rect().center().x()) <= 1
    assert abs(logo.geometry().center().y() - page.rect().center().y()) <= 1
    assert logo.width() >= width * 0.35
    assert not page._content.isVisible()
    page.resize(width + 100, height + 80)
    assert abs(logo.geometry().center().x() - page.rect().center().x()) <= 1
    assert abs(logo.geometry().center().y() - page.rect().center().y()) <= 1
    assert abs(logo.width() / logo.height() - 975 / 529) < 0.03
    assert logo.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert logo.focusPolicy() == Qt.FocusPolicy.NoFocus


def _ready_page(qtbot: QtBot) -> LoginPage:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    page._typing.stop()
    page._ready = True
    page._form.setEnabled(True)
    page._form.settle()
    return page


def test_idle_login_has_no_logo(qtbot: QtBot) -> None:
    page = _ready_page(qtbot)
    assert not page._logo.isVisible()
    page.hide()
    page.show()
    assert not page._logo.isVisible()
    assert page._logo._get_progress() == 1
    assert page._logo._phase == "idle"
    assert not page._logo._timer.isActive()


@pytest.mark.parametrize("trigger", ["return", "button"])
def test_submission_starts_loading_and_blocks_duplicates(qtbot: QtBot, trigger: str) -> None:
    page = _ready_page(qtbot)
    submitted: list[str] = []
    cancelled: list[bool] = []
    page.submitted.connect(submitted.append)
    page.cancelled.connect(lambda: cancelled.append(True))
    page._input.setText("password")
    if trigger == "return":
        qtbot.keyClick(page._input, Qt.Key.Key_Return)
    else:
        qtbot.mouseClick(page._enter, Qt.MouseButton.LeftButton)
    assert submitted == ["password"]
    assert page.loading
    assert page._logo._phase == "drawing"
    assert page._logo._get_progress() == 0
    assert page._content.isVisible()  # The veil fades over the unchanged login layout.
    assert page._blackout.opacity == 0
    assert page._input.text() == ""
    page._input.setText("duplicate")
    page._submit()
    qtbot.keyClick(page, Qt.Key.Key_Escape)
    assert submitted == ["password"]
    assert cancelled == []


def test_erasure_removes_paths_not_a_filled_silhouette(qtbot: QtBot) -> None:
    logo = OutlineLogo()
    qtbot.addWidget(logo)
    full = _ink(_frame(logo))
    logo._erasure = 0.5
    logo._update_renderer()
    assert 0 < _ink(_frame(logo)) < full
    logo._erasure = 1.0
    logo._update_renderer()
    assert _ink(_frame(logo)) == 0


def test_fast_readiness_cannot_skip_drawing_or_erasure(qtbot: QtBot) -> None:
    page = _ready_page(qtbot)
    page._input.setText("password")
    page._submit()
    completed: list[bool] = []
    page.exit_ready.connect(lambda: completed.append(True))
    page.request_exit()
    page.request_exit()  # Repeated handoff requests do not restart or bypass the sequence.
    page.prepare_exit_snapshot()
    assert page._logo._phase == "drawing"
    assert page._logo._get_progress() == 0
    assert not completed
    qtbot.waitUntil(lambda: page._logo._phase == "erasing", timeout=3000)
    assert page._logo._get_progress() == 1
    assert not completed
    qtbot.waitUntil(lambda: bool(completed), timeout=1500)
    assert completed == [True]
    assert page._logo._erasure == 1
    assert not page._logo._timer.isActive()


def test_slow_readiness_holds_complete_logo(qtbot: QtBot) -> None:
    page = _ready_page(qtbot)
    page._input.setText("password")
    page._submit()
    qtbot.waitUntil(lambda: page._logo._phase == "holding", timeout=3000)
    assert page.loading
    assert page._logo._get_progress() == 1
    assert page._logo._erasure == 0
    assert not page._logo._timer.isActive()
    page.request_exit()
    assert page._logo._phase == "erasing"
    assert page.loading


def test_stalls_and_hiding_cannot_skip_loading_frames(qtbot: QtBot) -> None:
    import time

    page = _ready_page(qtbot)
    page._input.setText("password")
    page._submit()
    logo = page._logo
    qtbot.waitUntil(logo.isVisible, timeout=800)
    before = logo._elapsed_ms
    time.sleep(0.2)
    logo._advance()
    assert 0 < logo._elapsed_ms - before <= logo.MAX_FRAME_MS
    progress = logo._get_progress()
    page.hide()
    assert not logo._timer.isActive()
    assert logo._phase == "drawing"
    page.show()
    assert logo._timer.isActive()
    assert logo._get_progress() == progress


@pytest.mark.parametrize("stage", ["fade_in", "drawing", "holding"])
def test_wrong_password_immediately_shakes_without_return_fade(qtbot: QtBot, stage: str) -> None:
    page = _ready_page(qtbot)
    page._input.setText("wrong")
    page._submit()
    if stage == "drawing":
        qtbot.waitUntil(lambda: page._logo._get_progress() > 0.1, timeout=1400)
    elif stage == "holding":
        qtbot.waitUntil(lambda: page._logo._phase == "holding", timeout=3500)
    completions: list[str] = []
    page._blackout.finished.connect(lambda: completions.append("veil"))
    page._logo.finished.connect(lambda: completions.append("logo"))
    page.exit_ready.connect(lambda: completions.append("exit"))

    page.show_password_error()
    page.set_prompt("重新输入主密码", "密码错误")
    assert not page.loading
    assert page._content.isVisible()
    assert page._input.isEnabled()
    qtbot.waitUntil(page._input.hasFocus, timeout=500)
    assert page._input.placeholderText() == "重新输入主密码"
    assert not page._blackout.isVisible()
    assert page._blackout.opacity == 0
    assert not page._blackout._timer.isActive()
    assert not page._blackout._active
    assert page._logo._phase == "idle"
    assert not page._logo.isVisible()
    assert not page._logo._timer.isActive()
    assert page._input._shake.state().name == "Running"
    origin = page._input.pos()
    qtbot.waitUntil(lambda: page._input.pos() != origin, timeout=400)
    page.hide()
    page.show()
    qtbot.wait(350)
    assert completions == []
    assert not page._blackout.isVisible()
    assert not page._logo.isVisible()

    page._input.setText("correct")
    page._submit()
    assert page.loading
    assert page._blackout.opacity == 0
    assert page._blackout._timer.isActive()
    assert page._logo._phase == "drawing"
    assert page._logo._get_progress() == 0
    assert page._logo._erasure == 0
    page.request_exit()
    assert page._logo._phase == "drawing"  # Success still cannot bypass the full sequence.


def test_logo_recolors_with_theme_without_replaying(qtbot: QtBot) -> None:
    logo = OutlineLogo()
    qtbot.addWidget(logo)
    logo.resize(488, 265)
    logo.show()
    logo.settle()
    previous = theme.current_palette().name
    try:
        theme.set_palette("dark")
        logo.grab()  # paintEvent picks up theme changes, even after the animation stops.
        dark = _frame(logo)
        theme.set_palette("light")
        logo.grab()
        light = _frame(logo)
        assert _ink(dark) == _ink(light) > 0
        assert dark != light
        assert logo._get_progress() == 1
    finally:
        theme.set_palette(previous)


def test_busy_or_empty_input_cannot_start_loading(qtbot: QtBot) -> None:
    page = _ready_page(qtbot)
    page._input.setText("   ")
    page._submit()
    assert not page.loading
    page.set_busy("正在准备", "等待启动任务")
    page._input.setText("password")
    page._submit()
    assert not page.loading


def test_startup_worker_keeps_loading_clock_running(qtbot: QtBot) -> None:
    import time

    from limbowave.app import _run_startup_task

    page = _ready_page(qtbot)
    page._input.setText("password")
    page._submit()
    _run_startup_task(lambda: time.sleep(0.6))
    assert page._logo._get_progress() > 0
    assert page._logo._phase == "drawing"
    assert page.loading


@pytest.mark.parametrize("palette", ["light", "dark"])
def test_loading_fades_to_black_then_draws_contrasting_logo(qtbot: QtBot, palette: str) -> None:
    previous = theme.current_palette().name
    try:
        theme.set_palette(palette)
        page = _ready_page(qtbot)
        page._input.setText("password")
        page._submit()
        logo = page._logo
        assert not logo.isVisible()
        assert page._blackout.opacity == 0
        qtbot.waitUntil(lambda: 0.1 < page._blackout.opacity < 0.9, timeout=800)
        partial = page.grab().toImage().pixelColor(4, 4)
        assert partial.lightness() > 0
        assert logo._get_progress() == 0
        qtbot.waitUntil(lambda: page._blackout.opacity == 1, timeout=800)
        initial = page.grab().toImage()
        # Fully black before drawing: no greeting, glow, baseline or static logo.
        assert all(
            initial.pixelColor(x, y).name() == "#000000"
            for y in range(0, initial.height(), 10)
            for x in range(0, initial.width(), 10)
        )
        qtbot.waitUntil(lambda: logo._get_progress() > 0.1, timeout=1200)
        frame = page.grab().toImage()
        assert frame.pixelColor(4, 4).name() == "#000000"
        assert frame.pixelColor(frame.width() // 2, frame.height() - 40).name() == "#000000"
        # Verify the actual rendered strokes, not theme metadata.
        image = _frame(logo)
        colors = [
            image.pixelColor(x, y)
            for y in range(image.height()) for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 100
        ]
        assert colors
        assert min(color.lightness() for color in colors) > 180
    finally:
        theme.set_palette(previous)
