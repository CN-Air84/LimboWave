from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from limbowave.ui.animated_stack import AnimatedPageStack


@pytest.fixture(params=[Qt.Orientation.Vertical, Qt.Orientation.Horizontal])
def orientation(request: pytest.FixtureRequest) -> Qt.Orientation:
    return request.param


@pytest.fixture
def stack(qtbot: QtBot, orientation: Qt.Orientation) -> AnimatedPageStack:
    # Exercise the default constructor as well as the explicit horizontal opt-in.
    widget = (
        AnimatedPageStack()
        if orientation == Qt.Orientation.Vertical
        else AnimatedPageStack(orientation=orientation)
    )
    qtbot.addWidget(widget)
    widget.resize(400, 300)
    widget.add_page(QLabel("First"))
    widget.add_page(QLabel("Second"))
    widget.show()
    return widget


@pytest.mark.parametrize("capture", [False, True])
def test_refresh_slides_vertically(stack: AnimatedPageStack, capture: bool) -> None:
    if capture:
        stack.capture_refresh()
    stack.animate_refresh(0)
    if capture:
        outgoing = stack._outgoing
        assert outgoing is not None
        assert stack._animation is not None
        stack._animation.setCurrentTime(80)
        assert outgoing.x() == 0
        assert -14 < outgoing.y() < 0
        assert 0.0 < outgoing.graphicsEffect().opacity() < 1.0
        assert stack._effect.opacity() == 0.0
        stack._animation.setCurrentTime(160)
    assert stack._outgoing is None
    assert stack._stack.pos() == QPoint(0, 18)
    assert stack._animation is not None
    stack._animation.setCurrentTime(100)
    assert stack._stack.x() == 0
    assert 0 < stack._stack.y() < 18
    assert 0.0 < stack._effect.opacity() < 1.0
    stack._animation.setCurrentTime(230)
    assert stack._animation is None
    assert stack._stack.pos() == QPoint(0, 0)
    assert not stack._effect.isEnabled()


@pytest.mark.parametrize("direction", [-1, 1])
def test_refresh_follows_list_selection_direction(
    stack: AnimatedPageStack, direction: int
) -> None:
    stack.capture_refresh(direction)
    stack.animate_refresh(0)
    assert stack._outgoing is not None
    assert stack._animation is not None
    stack._animation.setCurrentTime(80)
    assert -14 < stack._outgoing.y() * direction < 0
    stack._animation.setCurrentTime(160)
    assert stack._stack.pos() == QPoint(0, direction * 18)
    stack._animation.setCurrentTime(230)
    assert stack._stack.pos() == QPoint(0, 0)


@pytest.mark.parametrize("direction", [-1, 1])
def test_tab_switch_follows_orientation(
    stack: AnimatedPageStack, direction: int, orientation: Qt.Orientation,
) -> None:
    horizontal = orientation == Qt.Orientation.Horizontal
    offset = QPoint(direction * 18, 0) if horizontal else QPoint(0, direction * 18)
    initial, target = (0, 1) if direction == 1 else (1, 0)
    stack.set_index(initial, animated=False)
    stack.set_index(target)
    assert stack._animation is not None
    stack._animation.setCurrentTime(80)
    assert (stack._stack.y() if horizontal else stack._stack.x()) == 0
    assert -14 < (stack._stack.x() if horizontal else stack._stack.y()) * direction < 0
    stack._animation.setCurrentTime(160)
    assert stack._stack.pos() == offset
    assert stack._animation is not None
    stack._animation.setCurrentTime(100)
    assert (stack._stack.y() if horizontal else stack._stack.x()) == 0
    assert 0 < (stack._stack.x() if horizontal else stack._stack.y()) * direction < 18
    assert 0.0 < stack._effect.opacity() < 1.0
    stack._animation.setCurrentTime(230)
    assert stack.current_index == target
    assert stack._stack.pos() == QPoint(0, 0)


def test_refresh_and_tab_switch_can_interrupt_each_other(
    stack: AnimatedPageStack, orientation: Qt.Orientation,
) -> None:
    stack.capture_refresh()
    stack.animate_refresh(0)
    assert stack._animation is not None
    stack._animation.setCurrentTime(80)
    stack.set_index(1)
    assert stack._outgoing is None
    assert stack._animation is not None
    stack._animation.setCurrentTime(160)
    expected = QPoint(18, 0) if orientation == Qt.Orientation.Horizontal else QPoint(0, 18)
    assert stack._stack.pos() == expected
    stack.capture_refresh()
    stack.animate_refresh(1)
    assert stack._animation is not None
    stack._animation.setCurrentTime(160)
    assert stack._stack.pos() == QPoint(0, 18)
    assert stack._animation is not None
    stack._animation.setCurrentTime(230)
    assert stack.current_index == 1
    assert stack._outgoing is None
    assert stack._stack.pos() == QPoint(0, 0)
    assert not stack._effect.isEnabled()


def test_selecting_source_during_fade_out_cancels_pending_switch(stack: AnimatedPageStack) -> None:
    stack.set_index(1)
    stack._animation.setCurrentTime(80)
    stack.set_index(0)
    assert stack._animation is None
    assert stack.current_index == 0
    assert stack._stack.pos() == QPoint(0, 0)
    assert stack._effect.opacity() == 1.0
    assert not stack._effect.isEnabled()


@pytest.mark.parametrize("incoming", [False, True])
def test_hide_settles_pending_switch_and_reopen_is_visible(
    stack: AnimatedPageStack, incoming: bool,
) -> None:
    stack.set_index(1)
    stack._animation.setCurrentTime(160 if incoming else 80)
    if incoming:
        stack._animation.setCurrentTime(80)
    stack.hide()
    assert stack._animation is None
    assert stack.current_index == 1
    stack.show()
    assert stack._stack.pos() == QPoint(0, 0)
    assert stack._effect.opacity() == 1.0
    assert not stack._effect.isEnabled()
