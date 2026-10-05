"""Global tool visibility fades without fading text or losing live detail state."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QAbstractAnimation
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect
from pytestqt.qtbot import QtBot

from limbowave.domain.tool_step import ToolStep, finish
from limbowave.ui.chat_view import ChatView


@pytest.fixture
def chat(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> ChatView:
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(980, 720)
    view.show()
    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("", "m1")
    view.note_tool_step(
        "read", "c1", phase="start", is_error=False,
        step=ToolStep("c1", "read", args={"path": "notes.md"}),
    )
    view.begin_assistant()
    view.end_assistant("Final answer", "m2")
    qtbot.waitExposed(view)
    return view


def test_global_toggle_fades_tools_and_dividers_before_collapsing(chat, qtbot):
    row = chat._rows[-1]
    tool_only, final = row._segments
    tools = tool_only.tool_steps_view
    divider = row._segment_dividers[0]
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    assert animation.state() == QAbstractAnimation.State.Running
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    QApplication.processEvents()
    for widget in (tools, divider):
        assert widget.isVisible()
        effect = widget.graphicsEffect()
        assert isinstance(effect, QGraphicsOpacityEffect)
        assert effect.opacity() == pytest.approx(0.5)
    assert tool_only.isVisible()
    assert final.isVisible()
    assert final.content.graphicsEffect() is None
    assert row._assistant_panel.graphicsEffect() is None
    animation.resume()
    qtbot.waitUntil(tool_only.isHidden)
    assert tools.isHidden()
    assert divider.isHidden()
    assert tools.graphicsEffect() is None
    assert divider.graphicsEffect() is None
    assert final.isVisible()

    chat.set_tool_steps_visible(True)
    assert animation.state() == QAbstractAnimation.State.Running
    animation.pause()
    assert tools.isVisible()
    assert tools.graphicsEffect().opacity() == 0.0
    animation.setCurrentTime(animation.duration() // 2)
    assert tools.graphicsEffect().opacity() == pytest.approx(0.5)
    assert divider.graphicsEffect().opacity() == pytest.approx(0.5)
    animation.resume()
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    assert tools.isVisible()
    assert tools.graphicsEffect() is None
    assert divider.graphicsEffect() is None


def test_global_toggle_reverses_and_repeated_target_does_not_restart(chat, qtbot):
    row = chat._rows[-1]
    tools = row._segments[0].tool_steps_view
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 3)
    opacity = tools.graphicsEffect().opacity()
    chat.set_tool_steps_visible(False)
    assert animation.state() == QAbstractAnimation.State.Paused
    assert tools.graphicsEffect().opacity() == opacity
    chat.set_tool_steps_visible(True)
    assert animation.direction() == QAbstractAnimation.Direction.Forward
    assert tools.graphicsEffect().opacity() == opacity
    chat.set_tool_steps_visible(False)
    assert animation.direction() == QAbstractAnimation.Direction.Backward
    assert tools.graphicsEffect().opacity() == opacity
    qtbot.waitUntil(tools.isHidden)


def test_live_updates_keep_fade_progress_and_expanded_details(chat, qtbot):
    row = chat._rows[-1]
    tools = row._segments[0].tool_steps_view
    tools._chips["c1"].click()
    detail = tools._details["c1"]
    detail.settle()
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    step = row._segments[0].live_tool_steps[0]
    chat.note_tool_step(
        "read", "c1", phase="end", is_error=False,
        step=finish(step, result="updated result", duration_ms=12),
    )
    assert tools.isVisible()
    assert tools.graphicsEffect().opacity() == pytest.approx(0.5)
    assert "updated result" in detail._detail.text()
    chat.note_tool_step(
        "search", "c2", phase="start", is_error=False, step=ToolStep("c2", "search"),
    )
    new_tools = row._segments[-1].tool_steps_view
    assert new_tools.isVisible()
    assert new_tools.graphicsEffect().opacity() == pytest.approx(0.5)
    animation.resume()
    qtbot.waitUntil(tools.isHidden)
    assert new_tools.isHidden()
    chat.set_tool_steps_visible(True)
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    assert tools._chips["c1"]._expanded
    assert detail._detail.isVisible()
    assert "updated result" in detail._detail.text()


@pytest.mark.parametrize("visible", [False, True])
def test_hiding_window_settles_fade(chat, qtbot, visible):
    row = chat._rows[-1]
    tools = row._segments[0].tool_steps_view
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    if visible:
        chat.set_tool_steps_visible(True)
    chat.hide()
    assert animation.state() == QAbstractAnimation.State.Stopped
    assert tools.graphicsEffect() is None
    chat.show()
    qtbot.waitExposed(chat)
    assert tools.isVisible() == visible
    assert row._segments[0].isVisible() == visible


@pytest.mark.parametrize("hidden", [False, True])
def test_hidden_or_disabled_motion_switches_immediately(chat, monkeypatch, hidden):
    row = chat._rows[-1]
    tools = row._segments[0].tool_steps_view
    if hidden:
        chat.hide()
    else:
        monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: False))
    chat.set_tool_steps_visible(False)
    assert tools.isHidden()
    assert row._segments[0].isHidden()
    assert tools.graphicsEffect() is None
    chat.set_tool_steps_visible(True)
    assert not tools.isHidden()
    assert not row._segments[0].isHidden()
    assert tools.graphicsEffect() is None


@pytest.mark.parametrize("placement", ["tool_only", "after_text", "before_text"])
def test_slide_interpolates_panel_height_without_endpoint_jump(chat, qtbot, placement):
    row = chat._rows[-1]
    part = row._segments[0]
    tools = part.tool_steps_view
    if placement != "tool_only":
        part.raw_text = "Text next to the tool area"
        part.content.setPlainText(part.raw_text)
        row._sync_segment_presentation()
        if placement == "before_text":
            part.column.insertWidget(part.column.indexOf(part.content), tools)
    tools._chips["c1"].click()
    tools._details["c1"].settle()
    qtbot.wait(30)
    # Verify painted geometry, not QSizeHint (word-wrapped QLabel hints can be cached).
    shown_height = row._assistant_panel.height()
    natural_height = tools.height()
    detail_height = tools._details["c1"]._detail.height()
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    heights = []
    for fraction in (1.0, 0.75, 0.5, 0.25, 0.01):
        animation.setCurrentTime(round(animation.duration() * fraction))
        qtbot.wait(10)
        heights.append(row._assistant_panel.height())
        if fraction == 0.5:
            assert tools.height() == pytest.approx(natural_height / 2, abs=1)
            assert tools._body.y() < 0
            assert tools._details["c1"]._detail.height() == detail_height
    row._settle_tool_steps_visibility()
    qtbot.wait(10)
    hidden_height = row._assistant_panel.height()
    assert shown_height == heights[0]
    assert heights[0] > heights[1] > heights[2] > heights[3] >= heights[4]
    assert abs(heights[-1] - hidden_height) <= 1
    chat.set_tool_steps_visible(True)
    animation.pause()
    qtbot.wait(10)
    assert abs(row._assistant_panel.height() - hidden_height) <= 1
    animation.setCurrentTime(animation.duration() // 2)
    qtbot.wait(10)
    assert row._assistant_panel.height() == heights[2]
    assert tools._body.y() < 0
    animation.resume()
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    qtbot.wait(10)
    assert row._assistant_panel.height() == shown_height
    assert tools._body.y() == 0


def test_slide_reversal_preserves_height_and_position(chat, qtbot):
    row = chat._rows[-1]
    tools = row._segments[0].tool_steps_view
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    qtbot.wait(10)
    geometry = (tools.height(), tools._body.y(), row._assistant_panel.height())
    chat.set_tool_steps_visible(True)
    animation.pause()
    qtbot.wait(10)
    assert (tools.height(), tools._body.y(), row._assistant_panel.height()) == geometry


def test_slide_remeasures_wrapped_details_on_resize(chat, qtbot):
    row = chat._rows[-1]
    tools = row._segments[0].tool_steps_view
    tools._chips["c1"].click()
    detail = tools._details["c1"]
    detail._detail.setText("Long wrapped tool output. " * 100)
    detail.settle()
    qtbot.wait(30)
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    qtbot.wait(10)
    old_height = tools.height()
    chat.resize(520, 720)
    qtbot.wait(30)
    assert tools.height() > old_height
    assert tools.height() == pytest.approx(tools._body.height() / 2, abs=1)


def test_new_segment_during_slide_keeps_layout_order(chat, qtbot):
    row = chat._rows[-1]
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    row.begin_segment()
    row.append_thinking("Working on another step")
    row.append_delta("New output")
    row.set_tool_steps((ToolStep("c2", "search"),))
    part = row._segments[-1]
    qtbot.wait(20)
    assert part.column.indexOf(part.thinking) < part.column.indexOf(part.content)
    assert part.column.indexOf(part.content) < part.column.indexOf(part.tool_steps_view)
    assert row._column_layout.indexOf(row._segment_dividers[-1]) < row._column_layout.indexOf(part)
    assert row._column_layout.indexOf(part) < row._column_layout.indexOf(row._run_state)
    assert part.tool_steps_view._reveal_progress == pytest.approx(0.5)
    animation.resume()
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    assert part.tool_steps_view.isHidden()
    assert part.isVisible()
    for layout in (part.column, row._column_layout):
        assert layout.spacing() == 8
        assert all(layout.itemAt(i).widget() is not None for i in range(layout.count()))


def test_retry_during_slide_cleans_temporary_spacing(chat):
    row = chat._rows[-1]
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    row.reset_for_retry()
    assert animation.state() == QAbstractAnimation.State.Stopped
    assert row._column_layout.count() == 2
    assert row._column_layout.itemAt(0).widget() is row._run_state
    assert row._column_layout.itemAt(1).widget() is row._actions
    row.begin_segment()
    row.set_tool_steps((ToolStep("retry", "read"),))
    assert row._segments[0].tool_steps_view.isHidden()
