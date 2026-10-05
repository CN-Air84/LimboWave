"""工具步骤 UI 验收（Phase 9 / §三.2）。

锁住的不变量：
- 折叠态只显示 名称 + 状态 + 摘要；展开才出参数/结果/耗时；
- 失败的步骤有明确状态；
- **一键隐藏只影响展示**——组件隐藏后数据仍在（重新显示即回来）；
- 没有步骤时不占位。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from limbowave.domain.conversation import AssistantMessageSegment
from limbowave.domain.tool_step import ToolStatus, ToolStep
from limbowave.ui.chat_view import ChatView, HistoryEntry
from limbowave.ui.tool_steps import ToolStepsView, make_tool_steps


def _steps() -> tuple[ToolStep, ...]:
    return (
        ToolStep(
            tool_call_id="c1",
            name="read",
            status=ToolStatus.OK,
            duration_ms=12,
            args={"path": "notes.md"},
            result_summary="文件内容摘要",
        ),
        ToolStep(
            tool_call_id="c2",
            name="bash",
            status=ToolStatus.ERROR,
            duration_ms=300,
            error="退出码 1",
        ),
    )


def test_make_returns_none_without_steps() -> None:
    """没有步骤不占位（调用方据此不插组件）。"""
    assert make_tool_steps(()) is None


def test_collapsed_chip_shows_name_status_summary(qtbot: QtBot) -> None:
    view = ToolStepsView(_steps())
    qtbot.addWidget(view)
    text = _chip_texts(view)
    assert any("read" in t and "完成" in t and "文件内容摘要" in t for t in text)
    assert any("bash" in t and "失败" in t for t in text)


def test_details_hidden_until_expanded(qtbot: QtBot) -> None:
    """展开态才显示参数与耗时。"""
    view = ToolStepsView(_steps())
    qtbot.addWidget(view)
    view.show()
    from PySide6.QtWidgets import QLabel

    details = [w for w in view.findChildren(QLabel) if w.objectName().startswith("detail-")]
    assert len(details) == 2
    assert all(d.isHidden() for d in details)  # 默认折叠

    chip = _chip_for(view, "read")
    chip.toggle()
    assert not _detail_for(view, "c1").isHidden()
    assert "notes.md" in _detail_for(view, "c1").text()
    assert "耗时" in _detail_for(view, "c1").text()


def test_error_detail_shows_error(qtbot: QtBot) -> None:
    view = ToolStepsView(_steps())
    qtbot.addWidget(view)
    view.show()
    _chip_for(view, "bash").toggle()
    detail = _detail_for(view, "c2")
    assert "退出码 1" in detail.text()


def test_hide_only_affects_display(qtbot: QtBot) -> None:
    """一键隐藏：组件不可见，但数据与内容仍在（不是删除）。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    row = view._add_bubble("回答", role="assistant", message_id="m1")
    row.set_tool_steps(_steps())
    assert row._tool_steps_view is not None
    assert not row._tool_steps_view.isHidden()

    view.set_tool_steps_visible(False)
    qtbot.waitUntil(row._tool_steps_view.isHidden)

    # 数据没丢：重新显示就回来（内容与折叠态都还在）
    view.set_tool_steps_visible(True)
    assert not row._tool_steps_view.isHidden()
    assert any("read" in text for text in _chip_texts(row._tool_steps_view))


def test_new_rows_respect_hidden_state(qtbot: QtBot) -> None:
    """隐藏状态下新增的消息也不显示工具步骤（否则会突然冒出来）。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.set_tool_steps_visible(False)

    row = view._add_bubble("新回答", role="assistant", message_id="m2")
    row.set_tool_steps(_steps())
    assert row._tool_steps_view is not None
    assert row._tool_steps_view.isHidden()


def _assert_no_body_bar(browser: QWidget | None) -> None:
    assert browser is not None
    bar = browser.horizontalScrollBar()
    assert browser.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert bar.maximum() == 0
    assert bar.height() == 0
    assert bar.isHidden()


def test_hiding_steps_also_hides_long_run_dividers(qtbot: QtBot) -> None:
    """隐藏工具步骤时，长任务的正文分段线也一起消失。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("我先查一下。", "m1")
    view.note_tool_step("read", "call-1", is_error=False, phase="start")
    view.begin_assistant()
    view.end_assistant("这是结论。", "m2")
    row = view._rows[-1]
    divider = row._assistant_panel.findChild(QWidget, "assistantSegmentDivider")
    assert divider is not None and not divider.isHidden()

    view.set_tool_steps_visible(False)

    qtbot.waitUntil(divider.isHidden)
    assert all(not segment.isHidden() for segment in row._segments)

    view.set_tool_steps_visible(True)
    assert not divider.isHidden()


def test_hidden_tool_only_segments_collapse_and_body_has_no_horizontal_bar(
    qtbot: QtBot,
) -> None:
    """纯工具段隐藏后不留空位；空正文浏览器和圆角横条都不再占位。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("", "m1")
    view.note_tool_step("read", "call-1", is_error=False, phase="start")
    row = view._rows[-1]
    tool_only = row._segments[0]
    assert tool_only.content is not None
    assert tool_only.content.isHidden()
    assert tool_only.content.height() == 0
    _assert_no_body_bar(tool_only.content)

    view.begin_assistant()
    view.end_assistant("最终结论", "m2")
    final = row._segments[1]
    _assert_no_body_bar(final.content)

    view.set_tool_steps_visible(False)
    qtbot.waitUntil(tool_only.isHidden)
    assert not final.isHidden()
    assert all(divider.isHidden() for divider in row._segment_dividers)
    _assert_no_body_bar(tool_only.content)
    _assert_no_body_bar(final.content)

    view.set_tool_steps_visible(True)
    assert not tool_only.isHidden()
    assert tool_only.content.isHidden()
    assert tool_only.content.height() == 0


def test_hidden_empty_leading_segment_leaves_no_bar_or_divider(qtbot: QtBot) -> None:
    """历史里的空前置段隐藏后，不能留下分割线或空白横条。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(980, 600)
    view.show()
    step = ToolStep(tool_call_id="c1", name="read", status=ToolStatus.OK, duration_ms=12)
    view.load_history(
        [
            HistoryEntry(
                "assistant",
                "读到第60000行，7月22日这场令人精疲力竭的聚餐终于在这一句决绝的总结里落下了帷幕：",
                "",
                "m1",
                "run",
                (step,),
                False,
                True,
                (
                    AssistantMessageSegment(content="", tool_call_ids=(step.tool_call_id,)),
                    AssistantMessageSegment(
                        content="读到第60000行，7月22日这场令人精疲力竭的聚餐终于在这一句决绝的总结里落下了帷幕："
                    ),
                ),
            )
        ]
    )
    row = view._rows[-1]
    empty, final = row._segments
    view.set_tool_steps_visible(False)
    qtbot.waitUntil(lambda: empty.isHidden())
    assert empty.content is not None
    assert empty.content.isHidden()
    assert empty.content.height() == 0
    assert all(divider.isHidden() for divider in row._segment_dividers)
    assert not final.isHidden()
    _assert_no_body_bar(empty.content)
    _assert_no_body_bar(final.content)


def test_user_message_ignores_tool_steps(qtbot: QtBot) -> None:
    """工具步骤只挂助手消息。"""
    view = ChatView()
    qtbot.addWidget(view)
    row = view._add_bubble("问题", role="user", message_id="m3")
    row.set_tool_steps(_steps())
    assert row._tool_steps_view is None


def test_live_accumulation_then_refresh(qtbot: QtBot) -> None:
    """实时：start 先出 chip（运行中），end 到达后状态变完成。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.begin_assistant()
    view.note_tool_step("read", "call-1", is_error=False, phase="start")
    row = view._rows[-1]
    assert row._tool_steps_view is not None
    assert "运行中" in _chip_texts(row._tool_steps_view)[0]

    view.note_tool_step("read", "call-1", is_error=False, phase="end")
    assert "完成" in _chip_texts(row._tool_steps_view)[0]


def test_tool_after_message_end_stays_attached_to_active_run(qtbot: QtBot) -> None:
    """工具通常发生在 message.end 之后，仍应挂到最近的过程消息并显示阶段。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("我来读取目录。", "m-live")
    row = view._rows[-1]

    view.note_tool_step("read", "call-1", is_error=False, phase="start")

    assert row._tool_steps_view is not None
    assert "运行" in _chip_texts(row._tool_steps_view)[0]
    assert row._run_state is not None
    assert "正在运行工具：read" in row._run_state.text()

    view.note_tool_step("read", "call-1", is_error=False, phase="end")
    assert "完" in _chip_texts(row._tool_steps_view)[0]
    assert "Agent 正在继续" in row._run_state.text()


def test_long_tool_result_cannot_widen_message_column(qtbot: QtBot) -> None:
    """A long one-line result is elided inside the chip, not the whole page."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    long_result = '{"entries":[' + ",".join(
        f'{{"name":"file-{index}"}}' for index in range(600)
    ) + "]}"
    step = ToolStep(
        tool_call_id="long-result",
        name="list_directory",
        status=ToolStatus.OK,
        duration_ms=5188,
        result_summary=long_result,
    )
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    row = view._add_bubble("answer", role="assistant", message_id="m-long")
    row.set_tool_steps((step,))

    qtbot.waitUntil(lambda: row.width() == view._column_width)
    tool_view = row._tool_steps_view
    assert tool_view is not None
    chip = tool_view.findChild(QPushButton)
    assert chip is not None
    qtbot.waitUntil(lambda: chip.width() <= row.width())

    assert chip.text().endswith("…")
    assert chip.accessibleName().endswith(long_result)
    assert view._transcript_host.width() <= view._scroll.viewport().width()
    assert (
        view._scroll.horizontalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert view._scroll.horizontalScrollBar().maximum() == 0


def _chip_texts(view) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [b.text() for b in view.findChildren(QPushButton)]


def _chip_for(view, name: str):
    from PySide6.QtWidgets import QPushButton

    for button in view.findChildren(QPushButton):
        if f" {name} " in button.text():
            return button
    raise AssertionError(f"未找到 {name} 的 chip")


def _detail_for(view, call_id: str):
    from PySide6.QtWidgets import QLabel

    detail = view.findChild(QLabel, f"detail-{call_id}")
    assert detail is not None
    return detail


@pytest.fixture
def animated_steps(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> Iterator[ToolStepsView]:
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _effect: True))
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    view = ToolStepsView(_steps())
    layout.addWidget(view)
    layout.addStretch(1)
    host.resize(580, 600)
    host.show()
    qtbot.waitExposed(host)
    yield view


def test_details_animate_both_directions(animated_steps: ToolStepsView, qtbot: QtBot) -> None:
    view = animated_steps
    chip = _chip_for(view, "read")
    detail = _detail_for(view, "c1")
    panel = detail.parentWidget()
    collapsed_height = view.height()
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    animation = panel._animation
    assert animation.state() == QAbstractAnimation.State.Running
    animation.pause()
    assert panel.height() == 0
    animation.setCurrentTime(animation.duration() // 2)
    QApplication.processEvents()
    middle_height = panel.height()
    assert 0 < middle_height < detail.height()
    assert view.height() > collapsed_height
    animation.resume()
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    QApplication.processEvents()
    assert panel.height() == detail.height()
    assert chip.toolTip() == "点击收起详情"

    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    animation.pause()
    assert detail.isVisible()  # Keep drawing until the closing animation finishes.
    animation.setCurrentTime(animation.duration() // 2)
    QApplication.processEvents()
    assert panel.height() == middle_height
    animation.resume()
    qtbot.waitUntil(panel.isHidden)
    QApplication.processEvents()
    assert detail.isHidden()
    qtbot.waitUntil(lambda: view.height() == collapsed_height)
    assert chip.toolTip() == "点击展开详情"


def test_toggle_reverses_without_jumping(animated_steps: ToolStepsView, qtbot: QtBot) -> None:
    chip = _chip_for(animated_steps, "read")
    panel = _detail_for(animated_steps, "c1").parentWidget()
    chip.toggle()
    animation = panel._animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    QApplication.processEvents()
    middle_height = panel.height()
    chip.toggle()
    assert panel.height() == middle_height
    assert animation.direction() == QAbstractAnimation.Direction.Backward
    chip.toggle()
    assert panel.height() == middle_height
    assert animation.direction() == QAbstractAnimation.Direction.Forward
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    assert panel.isVisible()


@pytest.mark.parametrize("expanded", [False, True])
def test_hiding_settles_motion_without_losing_state(
    animated_steps: ToolStepsView, qtbot: QtBot, expanded: bool
) -> None:
    chip = _chip_for(animated_steps, "read")
    detail = _detail_for(animated_steps, "c1")
    panel = detail.parentWidget()
    chip.toggle()
    animation = panel._animation
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    if not expanded:
        chip.toggle()
    animated_steps.hide()
    assert animation.state() == QAbstractAnimation.State.Stopped
    animated_steps.show()
    QApplication.processEvents()
    assert panel.isVisible() == expanded
    assert detail.isVisible() == expanded
    if expanded:
        assert panel.height() == detail.height()
    assert "notes.md" in detail.text()


@pytest.mark.parametrize("hidden", [False, True])
def test_hidden_or_disabled_motion_settles_immediately(
    animated_steps: ToolStepsView, monkeypatch: pytest.MonkeyPatch, hidden: bool
) -> None:
    if hidden:
        animated_steps.hide()
    else:
        monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _effect: False))
    chip = _chip_for(animated_steps, "read")
    detail = _detail_for(animated_steps, "c1")
    panel = detail.parentWidget()
    chip.toggle()
    assert panel._animation.state() == QAbstractAnimation.State.Stopped
    assert not detail.isHidden()
    chip.toggle()
    assert panel.isHidden()
    assert detail.isHidden()


def test_steps_animate_independently(animated_steps: ToolStepsView, qtbot: QtBot) -> None:
    first = _detail_for(animated_steps, "c1").parentWidget()
    second = _detail_for(animated_steps, "c2").parentWidget()
    _chip_for(animated_steps, "read").toggle()
    _chip_for(animated_steps, "bash").toggle()
    assert first._animation is not second._animation
    _chip_for(animated_steps, "read").toggle()
    qtbot.waitUntil(first.isHidden)
    qtbot.waitUntil(lambda: second._animation.state() == QAbstractAnimation.State.Stopped)
    assert second.isVisible()


@pytest.mark.parametrize("during_animation", [False, True])
def test_wrapped_details_follow_width_changes(
    animated_steps: ToolStepsView, qtbot: QtBot, during_animation: bool
) -> None:
    detail = _detail_for(animated_steps, "c1")
    detail.setText("A long tool result with wrapped parameters and output. " * 30)
    panel = detail.parentWidget()
    _chip_for(animated_steps, "read").toggle()
    animation = panel._animation
    if during_animation:
        animation.pause()
        animation.setCurrentTime(animation.duration() // 2)
    else:
        qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    QApplication.processEvents()
    original_height = detail.height()
    host = animated_steps.window()
    host.resize(320, 600)
    QApplication.processEvents()
    QApplication.processEvents()
    assert detail.width() == panel.width()
    assert detail.height() > original_height
    assert detail.height() == detail.heightForWidth(panel.width())
    if during_animation:
        assert panel.height() == round(detail.height() * 0.5)
        animation.resume()
        qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    QApplication.processEvents()
    assert panel.height() == detail.height()
    host.resize(700, 600)
    QApplication.processEvents()
    QApplication.processEvents()
    assert detail.height() < original_height
    assert panel.height() == detail.height()


def test_animation_reflows_chat_rows_without_horizontal_overflow(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _effect: True))
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    row = view._add_bubble("answer", role="assistant", message_id="motion")
    row.set_tool_steps(_steps())
    following = view._add_bubble("next answer", role="assistant", message_id="next")
    qtbot.waitUntil(lambda: row.width() == view._column_width)
    QApplication.processEvents()
    steps = row._tool_steps_view
    detail = _detail_for(steps, "c1")
    panel = detail.parentWidget()
    chip = _chip_for(steps, "read")
    collapsed_y = following.y()
    chip.toggle()
    panel._animation.pause()
    panel._animation.setCurrentTime(panel._animation.duration() // 2)
    for _ in range(5):
        QApplication.processEvents()
    middle_y = following.y()
    assert middle_y > collapsed_y
    panel._animation.resume()
    qtbot.waitUntil(lambda: panel._animation.state() == QAbstractAnimation.State.Stopped)
    qtbot.waitUntil(lambda: following.y() > middle_y)
    assert view._scroll.horizontalScrollBar().maximum() == 0
    chip.toggle()
    qtbot.waitUntil(panel.isHidden)
    qtbot.waitUntil(lambda: following.y() == collapsed_y)



def test_expanded_details_reflow_after_font_change(
    animated_steps: ToolStepsView, qtbot: QtBot
) -> None:
    detail = _detail_for(animated_steps, "c1")
    detail.setText("Long parameters and output. " * 15)
    panel = detail.parentWidget()
    _chip_for(animated_steps, "read").toggle()
    qtbot.waitUntil(lambda: panel._animation.state() == QAbstractAnimation.State.Stopped)
    old_height = panel.height()
    detail.setStyleSheet(detail.styleSheet() + "font-size: 24px;")
    qtbot.waitUntil(lambda: panel.height() > old_height)
    assert panel.height() == detail.heightForWidth(panel.width())
    assert panel.height() == detail.height()
