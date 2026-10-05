"""Deterministic work budgets for tool transitions (no timing-sensitive assertions)."""

from PySide6.QtCore import QAbstractAnimation
from PySide6.QtWidgets import QApplication

from limbowave.domain.tool_step import ToolStep
from limbowave.ui.chat_view import ChatView
from tests.ui.test_tool_step_visibility_animation import chat as chat


def _spacers(layout):
    return [layout.itemAt(i).spacerItem() for i in range(layout.count())
            if layout.itemAt(i).spacerItem() is not None]


def test_motion_reuses_spacers_between_frames(chat, qtbot):
    row = chat._rows[-1]
    chat.set_tool_steps_visible(False)
    animation = row._tool_steps_animation
    animation.pause()
    animation.setCurrentTime(animation.duration() * 3 // 4)
    before = _spacers(row._column_layout)
    assert before
    animation.setCurrentTime(animation.duration() // 2)
    assert _spacers(row._column_layout) == before
    animation.setCurrentTime(animation.duration() // 4)
    assert _spacers(row._column_layout) == before


def test_motion_does_not_remeasure_wrapping_each_frame(chat, qtbot, monkeypatch):
    tools = chat._rows[-1]._segments[0].tool_steps_view
    qtbot.wait(30)
    layout = tools._body.layout()
    original = layout.totalHeightForWidth
    calls = []

    def measure(width):
        calls.append(width)
        return original(width)

    monkeypatch.setattr(layout, "totalHeightForWidth", measure)
    for progress in (0.9, 0.8, 0.7, 0.6, 0.5):
        tools.set_reveal_progress(progress)
    assert len(calls) <= 1


def test_offscreen_rows_settle_without_animators(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(980, 720)
    for i in range(30):
        row = view._add_bubble("Reply " + str(i), role="assistant", message_id=str(i))
        row.set_tool_steps((ToolStep(str(i), "read"),))
    view.show()
    qtbot.wait(100)
    scroll = view._scroll.verticalScrollBar()
    scroll.setValue(scroll.maximum())
    qtbot.wait(30)
    first, last = view._rows[0], view._rows[-1]
    view.set_tool_steps_visible(False)
    assert first._tool_steps_animation is None
    assert first._segments[0].tool_steps_view.isHidden()
    assert last._tool_steps_animation.state() == QAbstractAnimation.State.Running
    qtbot.waitUntil(lambda: last._tool_steps_animation.state() == QAbstractAnimation.State.Stopped)
    view.set_tool_steps_visible(True)
    assert first._tool_steps_animation is None
    assert not first._segments[0].tool_steps_view.isHidden()


def test_prepared_step_is_rendered_without_reformatting_arguments(qtbot):
    class ForbiddenRepr:
        def __repr__(self):
            raise AssertionError("GUI formatted tool arguments")

    from limbowave.ui.tool_steps import ToolStepsView

    step = ToolStep("c", "read", args={"value": ForbiddenRepr()},
                    display_summary="prepared summary", display_detail="prepared detail")
    view = ToolStepsView((step,))
    qtbot.addWidget(view)
    assert "prepared summary" in view._chips["c"]._full_text
    assert view._details["c"]._detail.text() == "prepared detail"
    view.update_steps((step,))
    view._chips["c"].toggle()
    assert view._details["c"]._detail.text() == "prepared detail"
