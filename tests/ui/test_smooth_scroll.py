from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QCursor, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QTextBrowser
from pytestqt.qtbot import QtBot

from limbowave.ui.smooth_scroll import SmoothScrollFilter, install_smooth_scrolling


def _wheel(
    *,
    angle: QPoint | None = None,
    pixel: QPoint | None = None,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QWheelEvent:
    return QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        pixel or QPoint(),
        angle or QPoint(),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def _scroll_area(qtbot: QtBot) -> QScrollArea:
    area = QScrollArea()
    content = QLabel("content", area)
    content.setFixedSize(200, 1600)
    area.setWidget(content)
    area.resize(240, 240)
    qtbot.addWidget(area)
    area.show()
    qtbot.waitExposed(area)
    return area


def test_discrete_wheel_scrolls_with_animation(qtbot: QtBot) -> None:
    area = _scroll_area(qtbot)
    effect = SmoothScrollFilter(area)
    bar = area.verticalScrollBar()
    bar.setValue(400)

    handled = effect.eventFilter(area.viewport(), _wheel(angle=QPoint(0, 120)))

    assert handled
    assert bar.value() == 400
    qtbot.waitUntil(lambda: bar.value() < 400)
    qtbot.waitUntil(lambda: bar.value() == 340)


def test_touchpad_and_control_wheel_remain_native(qtbot: QtBot) -> None:
    area = _scroll_area(qtbot)
    effect = SmoothScrollFilter(area)

    assert not effect.eventFilter(area.viewport(), _wheel(pixel=QPoint(0, 12)))
    assert not effect.eventFilter(
        area.viewport(),
        _wheel(angle=QPoint(0, 120), modifiers=Qt.KeyboardModifier.ControlModifier),
    )


def test_global_install_is_idempotent(qapp: QApplication) -> None:
    first = install_smooth_scrolling(qapp)
    second = install_smooth_scrolling(qapp)

    assert first is second


@pytest.fixture
def middle_scroll(qapp: QApplication) -> Iterator[SmoothScrollFilter]:
    effect = SmoothScrollFilter(qapp)
    qapp.installEventFilter(effect)
    yield effect
    effect._stop_auto_scroll()
    qapp.removeEventFilter(effect)
    effect.deleteLater()


def _mouse(
    area: QScrollArea,
    kind: QEvent.Type = QEvent.Type.MouseButtonPress,
    button: Qt.MouseButton = Qt.MouseButton.MiddleButton,
) -> QMouseEvent:
    local = QPointF(80, 80)
    return QMouseEvent(
        kind, local, QPointF(area.viewport().mapToGlobal(local.toPoint())),
        button,
        Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button,
        Qt.KeyboardModifier.NoModifier,
    )


def _start_middle_scroll(
    effect: SmoothScrollFilter, area: QScrollArea, monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = area.viewport().mapToGlobal(QPoint(80, 80))
    monkeypatch.setattr(QCursor, "pos", lambda: origin)
    assert effect.eventFilter(area.viewport(), _mouse(area))
    assert effect._auto_scroll is not None


def test_middle_click_latches_with_marker_and_cursor(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    bar = area.verticalScrollBar()
    bar.setValue(400)
    previous_cursor = QApplication.overrideCursor()
    previous_shape = previous_cursor.shape() if previous_cursor is not None else None
    _start_middle_scroll(middle_scroll, area, monkeypatch)

    assert middle_scroll.eventFilter(
        area.viewport(), _mouse(area, QEvent.Type.MouseButtonRelease),
    )
    assert middle_scroll._auto_timer.isActive()
    state = middle_scroll._auto_scroll
    assert state is not None
    assert state.marker.isVisible()
    assert state.marker.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert state.marker.geometry().center() == QPoint(80, 80)
    assert QApplication.overrideCursor().shape() == Qt.CursorShape.SizeVerCursor
    middle_scroll._advance_auto_scroll(QPoint(0, 8), 0.1)
    assert bar.value() == 400

    assert middle_scroll.eventFilter(area.viewport(), _mouse(area))
    assert middle_scroll._auto_scroll is None
    assert not middle_scroll._auto_timer.isActive()
    assert not state.marker.isVisible()
    cursor = QApplication.overrideCursor()
    assert (cursor.shape() if cursor is not None else None) == previous_shape


def test_middle_scroll_moves_both_directions_and_clamps(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    bar = area.verticalScrollBar()
    bar.setValue(500)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    middle_scroll._advance_auto_scroll(QPoint(0, 45), 0.1)
    slow_distance = bar.value() - 500
    assert slow_distance > 0
    bar.setValue(500)
    middle_scroll._advance_auto_scroll(QPoint(0, 100), 0.1)
    assert bar.value() - 500 > slow_distance
    middle_scroll._advance_auto_scroll(QPoint(0, -100), 0.1)
    assert abs(bar.value() - 500) <= 1
    bar.setValue(bar.maximum() - 1)
    middle_scroll._advance_auto_scroll(QPoint(0, 1000), 0.1)
    assert bar.value() == bar.maximum()
    middle_scroll._advance_auto_scroll(QPoint(0, -100), 0.1)
    assert bar.value() < bar.maximum()


def test_middle_scroll_timer_polls_cursor_after_button_release(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    middle_scroll.eventFilter(area.viewport(), _mouse(area, QEvent.Type.MouseButtonRelease))
    state = middle_scroll._auto_scroll
    assert state is not None
    monkeypatch.setattr(QCursor, "pos", lambda: state.origin + QPoint(0, 100))
    qtbot.waitUntil(lambda: area.verticalScrollBar().value() > 0)


def test_middle_scroll_horizontal_axis_and_nested_browser(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    browser = QTextBrowser(area.widget())
    browser.setGeometry(0, 0, 180, 120)
    browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    browser.setHtml("<pre>" + "long line " * 100 + "</pre>")
    browser.show()
    qtbot.waitUntil(lambda: browser.horizontalScrollBar().maximum() > 0)
    monkeypatch.setattr(QCursor, "pos", lambda: area.viewport().mapToGlobal(QPoint(80, 80)))
    assert middle_scroll.eventFilter(browser.viewport(), _mouse(area))
    middle_scroll._advance_auto_scroll(QPoint(100, 100), 0.1)
    assert area.verticalScrollBar().value() > 0
    assert browser.horizontalScrollBar().value() > 0
    assert browser.verticalScrollBar().value() == 0
    assert QApplication.overrideCursor().shape() == Qt.CursorShape.SizeAllCursor


@pytest.mark.parametrize("button", [Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton,
                                    Qt.MouseButton.RightButton])
def test_cancelling_mouse_click_does_not_activate_control(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
    button: Qt.MouseButton,
) -> None:
    area = _scroll_area(qtbot)
    control = QPushButton("Do not click", area.widget())
    control.show()
    clicks: list[bool] = []
    control.clicked.connect(lambda: clicks.append(True))
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    middle_scroll.eventFilter(area.viewport(), _mouse(area, QEvent.Type.MouseButtonRelease))
    qtbot.mouseClick(control, button)
    assert middle_scroll._auto_scroll is None
    assert clicks == []
    qtbot.mouseClick(control, Qt.MouseButton.LeftButton)
    assert clicks == [True]


def test_escape_and_wheel_stop_auto_scroll(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    assert middle_scroll.eventFilter(
        area.viewport(), QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                  Qt.KeyboardModifier.NoModifier),
    )
    assert middle_scroll._auto_scroll is None
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    assert middle_scroll.eventFilter(area.viewport(), _wheel(angle=QPoint(0, -120)))
    assert middle_scroll._auto_scroll is None
    qtbot.waitUntil(lambda: area.verticalScrollBar().value() > 0)


@pytest.mark.parametrize("action", ["hide", "close", "delete", "disable", "deactivate"])
def test_middle_scroll_stops_when_target_is_unavailable(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    area = _scroll_area(qtbot)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    if action == "hide":
        area.hide()
    elif action == "close":
        area.close()
    elif action == "delete":
        area.deleteLater()
    elif action == "disable":
        area.setEnabled(False)
    else:
        QApplication.sendEvent(area, QEvent(QEvent.Type.WindowDeactivate))
    qtbot.waitUntil(lambda: middle_scroll._auto_scroll is None)
    assert not middle_scroll._auto_timer.isActive()


@pytest.mark.parametrize("reason", ["no_range", "policy", "disabled", "opt_out", "scrollbar"])
def test_middle_scroll_ignores_ineligible_targets(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, reason: str,
) -> None:
    area = _scroll_area(qtbot)
    if reason == "no_range":
        area.widget().setFixedSize(100, 100)
    elif reason == "policy":
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    elif reason == "disabled":
        area.setEnabled(False)
    elif reason == "opt_out":
        area.setProperty("smoothScrollDisabled", True)
    QApplication.processEvents()
    watched = area.verticalScrollBar() if reason == "scrollbar" else area.viewport()
    assert not middle_scroll.eventFilter(watched, _mouse(area))
    assert middle_scroll._auto_scroll is None
    assert not middle_scroll._auto_timer.isActive()


def test_middle_scroll_cancels_pending_wheel_animation(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    bar = area.verticalScrollBar()
    middle_scroll.eventFilter(area.viewport(), _wheel(angle=QPoint(0, -120)))
    assert bar in middle_scroll._motions
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    assert bar not in middle_scroll._motions
    qtbot.wait(200)
    assert bar.value() == 0


@pytest.mark.parametrize("start_at_bottom", [False, True])
def test_slow_middle_scroll_accumulates_fractional_steps_at_edges(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
    start_at_bottom: bool,
) -> None:
    area = _scroll_area(qtbot)
    bar = area.verticalScrollBar()
    start = bar.maximum() if start_at_bottom else bar.minimum()
    bar.setValue(start)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    offset = QPoint(0, -13 if start_at_bottom else 13)
    for _ in range(10):
        middle_scroll._advance_auto_scroll(offset, 0.1)
    assert (bar.value() < start) if start_at_bottom else (bar.value() > start)


def test_middle_scroll_horizontal_only(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    area.widget().setFixedSize(1600, 100)
    qtbot.waitUntil(lambda: area.horizontalScrollBar().maximum() > 0)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    assert QApplication.overrideCursor().shape() == Qt.CursorShape.SizeHorCursor
    middle_scroll._advance_auto_scroll(QPoint(100, 100), 0.1)
    assert area.horizontalScrollBar().value() > 0
    assert area.verticalScrollBar().value() == 0


def test_middle_click_is_dispatched_from_child_and_preserves_cursor_stack(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    content = area.widget()
    assert content is not None
    origin = content.mapToGlobal(QPoint(50, 50))
    monkeypatch.setattr(QCursor, "pos", lambda: origin)
    QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
    try:
        qtbot.mouseClick(content, Qt.MouseButton.MiddleButton, pos=QPoint(50, 50))
        assert middle_scroll._auto_scroll is not None
        assert middle_scroll._auto_scroll.origin == origin
        qtbot.keyClick(area, Qt.Key.Key_Escape)
        assert middle_scroll._auto_scroll is None
        assert QApplication.overrideCursor().shape() == Qt.CursorShape.WaitCursor
    finally:
        middle_scroll._stop_auto_scroll()
        QApplication.restoreOverrideCursor()


def test_chat_middle_scroll_leaves_bottom_follow_mode(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from limbowave.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    view.resize(600, 400)
    view.show()
    qtbot.waitExposed(view)
    view.add_user_message("question")
    view.begin_assistant()
    for _ in range(40):
        view.append_assistant_delta("A long response line.\n\n")
    area = view._scroll
    bar = area.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0 and bar.value() == bar.maximum())
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    middle_scroll._advance_auto_scroll(QPoint(0, -100), 0.1)
    assert not view._stick_to_bottom
    position = bar.value()
    view.append_assistant_delta("More output.\n\n" * 10)
    assert bar.value() == position
    middle_scroll._stop_auto_scroll()


def test_wheel_animation_survives_target_deletion(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter,
) -> None:
    area = _scroll_area(qtbot)
    bar = area.verticalScrollBar()
    middle_scroll.eventFilter(area.viewport(), _wheel(angle=QPoint(0, -120)))
    area.deleteLater()
    qtbot.waitUntil(lambda: bar not in middle_scroll._motions)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_middle_scroll_marker_stays_legible_in_both_themes(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from limbowave.ui import theme

    palette = theme.LIGHT if mode == "light" else theme.DARK
    monkeypatch.setattr(theme, "BG_ELEVATED", palette.bg_elevated)
    monkeypatch.setattr(theme, "TEXT_PRIMARY", palette.text_primary)
    monkeypatch.setattr(theme, "BORDER", palette.border)
    area = _scroll_area(qtbot)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    state = middle_scroll._auto_scroll
    assert state is not None
    image = state.marker.grab().toImage()
    scale = image.devicePixelRatio()
    foreground = image.pixelColor(round(14 * scale), round(14 * scale))
    background = image.pixelColor(round(14 * scale), round(11 * scale))
    assert abs(foreground.lightnessF() - background.lightnessF()) > 0.4


def test_escape_reserves_shortcut_before_stopping_scroll(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = _scroll_area(qtbot)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    event = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Escape,
                      Qt.KeyboardModifier.NoModifier)
    assert middle_scroll.eventFilter(area.viewport(), event)
    assert event.isAccepted()
    assert middle_scroll._auto_scroll is not None
    qtbot.keyClick(area, Qt.Key.Key_Escape)
    assert middle_scroll._auto_scroll is None


@pytest.mark.parametrize("pixel", [False, True])
def test_native_wheel_stops_middle_scroll_without_consuming_input(
    qtbot: QtBot, middle_scroll: SmoothScrollFilter, monkeypatch: pytest.MonkeyPatch,
    pixel: bool,
) -> None:
    area = _scroll_area(qtbot)
    _start_middle_scroll(middle_scroll, area, monkeypatch)
    wheel = _wheel(pixel=QPoint(0, 12)) if pixel else _wheel(
        angle=QPoint(0, 120), modifiers=Qt.KeyboardModifier.ControlModifier,
    )
    assert not middle_scroll.eventFilter(area.viewport(), wheel)
    assert middle_scroll._auto_scroll is None
