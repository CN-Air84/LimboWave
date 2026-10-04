"""ChatView 的 UI 测试：视图只管展示与发信号。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QImage, QKeyEvent
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from limbowave.domain.permissions import PermissionPreset
from limbowave.ui.chat_view import (
    _COMPOSER_MAX_WIDTH,
    COMPRESSION_DRAIN_PER_SEC,
    HISTORY_PAGE,
    ChatView,
    HistoryEntry,
    _composer_span,
)


@pytest.mark.parametrize(
    ("available", "collapsed", "expanded"),
    [
        # 放得下：两态同宽；收起时居中，展开时与右侧高级栏并排占满
        (876, (600, 138), (600, 0)),
        # 很宽：输入框封顶 800，收起、展开都居中
        (1596, (800, 398), (800, 260)),
        # 很窄：收起时放宽到 480，不为不在场的高级栏白白让位
        (526, (480, 23), (250, 0)),
    ],
)
def test_composer_span_keeps_width_and_centers(
    available: int, collapsed: tuple[int, int], expanded: tuple[int, int]
) -> None:
    panel = 276  # 高级栏 260 + 间距 16
    assert _composer_span(available, panel, 0.0) == collapsed
    assert _composer_span(available, panel, 1.0) == expanded


def test_attachment_chips_do_not_widen_composer(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1200, 800)
    view.show()
    qtbot.waitUntil(lambda: view._composer.width() == _COMPOSER_MAX_WIDTH)

    locked_width = view._composer.width()
    long_subtitle = "PNG 1930x1203 535.8 KB about 8857 tokens (estimated) " * 3
    view._attachment_bar.add_attachment("a1", "pasted-image.png", long_subtitle)
    view._attachment_bar.add_attachment("a2", "pasted-image.png", long_subtitle)

    qtbot.waitUntil(lambda: view._attachment_bar._chips_scroll.horizontalScrollBar().maximum() > 0)
    assert view._composer.width() == locked_width
    assert view._composer.width() <= _COMPOSER_MAX_WIDTH
    labels = view._attachment_bar._chips["a1"].findChildren(QLabel)
    assert any("8857 tokens (estimated)" in label.text() for label in labels)


def test_submit_emits_signal_and_clears(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("你好")

    with qtbot.waitSignal(view.message_submitted, timeout=1000) as blocker:
        view._on_send()

    assert blocker.args == ["你好"]
    assert view._input.toPlainText() == ""


def test_empty_input_does_not_emit(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view._input.setPlainText("   ")
    # 不应发信号：直接调用并确认没有挂起
    view._on_send()


def test_streaming_updates_bubble(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.begin_assistant()
    view.append_assistant_delta("你")
    view.append_assistant_delta("好")
    view.end_assistant("你好")

    # 流式气泡定稿为最终文本
    assert view._stream_row is None


def test_streaming_does_not_yank_user_scrolled_up(qtbot: QtBot) -> None:
    """流式输出时用户往上滚阅读，不应被强制拉回底部；停在底部时则自动跟随。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(600, 400)
    view.show()
    qtbot.waitExposed(view)
    bar = view._scroll.verticalScrollBar()

    view.add_user_message("问题")
    view.begin_assistant()
    for _ in range(40):
        view.append_assistant_delta("很长的一行输出\n\n")
    qtbot.waitUntil(lambda: bar.maximum() > 0 and bar.value() == bar.maximum())

    # 用户上翻：后续输出不再把视图拉回底部
    bar.setValue(0)
    old_max = bar.maximum()
    for _ in range(20):
        view.append_assistant_delta("更多输出\n\n")
    qtbot.waitUntil(lambda: bar.maximum() > old_max)
    assert bar.value() == 0

    # 用户滚回底部：恢复跟随
    bar.setValue(bar.maximum())
    old_max = bar.maximum()
    for _ in range(20):
        view.append_assistant_delta("继续输出\n\n")
    qtbot.waitUntil(lambda: bar.maximum() > old_max)
    assert bar.value() == bar.maximum()

    # 用户主动发送新消息：即便处于上翻状态也回到底部
    view.end_assistant("完成")
    bar.setValue(0)
    view.add_user_message("下一个问题")
    qtbot.waitUntil(lambda: bar.value() == bar.maximum())


def test_delayed_range_growth_follows_the_previous_bottom(qtbot: QtBot) -> None:
    """Deferred history layout must follow the old bottom to the final bottom."""
    view = ChatView()
    qtbot.addWidget(view)
    bar = view._scroll.verticalScrollBar()

    # Reproduce Qt's problematic signal order deterministically: the range has
    # already grown, but valueChanged(old_bottom) reaches us before rangeChanged.
    bar.blockSignals(True)
    bar.setRange(0, 200)
    bar.setValue(100)
    bar.blockSignals(False)
    view._scroll_maximum = 100
    view._stick_to_bottom = True

    view._on_scroll_value_changed(100)
    assert not view._stick_to_bottom
    view._on_scroll_range_changed(0, 200)

    assert view._stick_to_bottom
    assert bar.value() == 200


def test_jump_to_bottom_button(qtbot: QtBot) -> None:
    """上翻时浮现「回到底部」按钮，点击后回到底部、恢复跟随并隐藏。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(600, 800)
    view.show()
    qtbot.waitExposed(view)
    bar = view._scroll.verticalScrollBar()
    button = view._jump_btn

    view.add_user_message("问题")
    view.begin_assistant()
    for _ in range(80):
        view.append_assistant_delta("很长的一行输出\n\n")
    qtbot.waitUntil(lambda: bar.maximum() > 0 and bar.value() == bar.maximum())
    assert not button.isVisible()

    bar.setValue(0)
    # 进场：立刻显示，但从下方透明处起步，动画结束时浮到就位点并完全不透明
    assert button.isVisible()
    assert button.progress < 1.0
    start_y = button.y()
    qtbot.waitUntil(lambda: view._jump_anim is None)
    assert button.progress == 1.0
    assert button.y() < start_y
    # 底部居中，并且完整落在视口内
    viewport = view._scroll.viewport()
    assert viewport.rect().contains(button.geometry())
    assert abs(button.geometry().center().x() - viewport.rect().center().x()) <= 1

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bar.value() == bar.maximum())
    # 退场：先沉下去渐隐（期间不接收点击），动画结束才真正隐藏
    assert button.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    qtbot.waitUntil(lambda: not button.isVisible())
    assert button.progress == 0.0

    # 回底后继续输出，恢复自动跟随
    old_max = bar.maximum()
    for _ in range(20):
        view.append_assistant_delta("继续输出\n\n")
    qtbot.waitUntil(lambda: bar.maximum() > old_max)
    assert bar.value() == bar.maximum()
    assert not button.isVisible()


def test_busy_toggles_buttons(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.set_available(True)
    view._input.setPlainText("可发送")
    view.set_busy(True)
    assert not view._send_btn.isEnabled()
    assert view._stop_btn.isEnabled()

    view.set_busy(False)
    assert view._send_btn.isEnabled()


def test_unavailable_disables_input(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.set_available(False, "未配置")
    assert not view._input.isEnabled()
    assert not view._send_btn.isEnabled()


def test_load_history_replaces_transcript(qtbot: QtBot) -> None:
    """切换会话：历史渲染前必须清空旧消息区。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("旧会话的消息")

    view.load_history(
        [
            ("user", "第一轮", "", "m1"),
            ("assistant", "回复一", "", "m2"),
            ("user", "第二轮", "", "m3"),
        ]
    )

    texts = [
        view._transcript.itemAt(i).widget().content_text()
        for i in range(view._transcript.count() - 1)  # 末尾是 stretch
    ]
    assert texts == ["第一轮", "回复一", "第二轮"]


def test_empty_view_history_transition_fades_and_uses_short_lift(qtbot: QtBot) -> None:
    """空白页进入历史会话时同时渐显，且只从下方轻抬 8px。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)
    rest = view._transcript_host.pos()

    view.transition_history([("user", "旧会话", "", "m1")])

    assert view._history_opacity is not None
    qtbot.waitUntil(lambda: view._history_rest_pos is not None)
    assert view._history_rest_pos == rest
    assert 0 <= view._transcript_host.y() - rest.y() <= 8
    qtbot.waitUntil(lambda: view._history_transition is None)
    assert view._history_opacity is None
    assert view._transcript_host.pos() == rest
    assert [row.content_text() for row in view._rows] == ["旧会话"]


def test_history_transition_crossfades_between_conversations(qtbot: QtBot) -> None:
    """已有会话切到另一会话时，旧内容先保留渐隐，随后换成新内容渐显。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)
    view.load_history([("user", "会话 A", "", "a1")])
    loaded: list[str] = []

    view.transition_history(
        [("assistant", "会话 B", "", "b1")],
        on_loaded=lambda: loaded.append("b"),
    )

    assert [row.content_text() for row in view._rows] == ["会话 A"]
    assert view._history_opacity is not None
    qtbot.waitUntil(lambda: loaded == ["b"])
    assert [row.content_text() for row in view._rows] == ["会话 B"]
    qtbot.waitUntil(lambda: view._history_transition is None)
    assert view._history_opacity is None


def test_sent_attachments_show_above_user_bubble(qtbot: QtBot) -> None:
    from PySide6.QtGui import QPixmap

    from limbowave.ui.sent_attachments import SentAttachment, SentAttachmentStrip

    view = ChatView()
    qtbot.addWidget(view)
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.red)
    items = {
        "m1": [SentAttachment("a.png", "PNG", pixmap), SentAttachment("b.md", "3 行")],
    }
    view.set_attachment_resolver(lambda message_id: items.get(message_id, []))

    view.add_user_message("带附件", "m1")
    view.add_user_message("没附件", "m2")

    with_items, without = view._rows
    assert len(with_items.findChildren(SentAttachmentStrip)) == 1
    assert without.findChildren(SentAttachmentStrip) == []
    # 历史重渲染同样带上
    view.load_history([("user", "带附件", "", "m1")])
    assert len(view._rows[0].findChildren(SentAttachmentStrip)) == 1


def test_clear_transcript_keeps_layout_valid(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("a")
    view.add_user_message("b")

    view.clear_transcript()

    assert view._transcript.count() == 1  # 只剩 stretch
    assert view._stream_row is None


def test_assistant_bubble_renders_markdown_on_finalize(qtbot: QtBot) -> None:
    """助手气泡定稿时是 Markdown 富文本：代码块应产生 HTML 标签。"""
    view = ChatView()
    qtbot.addWidget(view)

    view.begin_assistant()
    view.append_assistant_delta("看代码：\n\n```python\nprint(1)\n```")
    # 流式期间是纯文本（不含 HTML 标签）
    assert "<pre" not in view._stream_row._raw_text

    view.end_assistant("看代码：\n\n```python\nprint(1)\n```")
    row = view._rows[-1]
    html = row.label.toHtml()
    assert "print" in html
    assert "<span" in html or "<pre" in html  # 高亮或代码块结构


def test_assistant_reply_has_background_panel(qtbot: QtBot) -> None:
    """模型回复的正文和动作栏位于同一块主题背景板中。"""
    view = ChatView()
    qtbot.addWidget(view)

    view.begin_assistant()
    view.append_assistant_delta("回答")
    row = view._stream_row

    assert row is not None
    assert row._assistant_panel.objectName() == "assistantMessagePanel"
    assert "background:" in row._assistant_panel.styleSheet()
    assert "border-radius:" in row._assistant_panel.styleSheet()
    # 正文经分段容器落在背景板里（一轮 run 一卡，分段是卡的内部结构）
    assert row.label is not None
    assert row._assistant_panel.isAncestorOf(row.label)


def test_user_bubble_stays_plain_text(qtbot: QtBot) -> None:
    """用户消息不渲染 Markdown——用户输入不是排版语言。"""
    from PySide6.QtWidgets import QLabel

    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("**这不是加粗**")
    row = view._rows[-1]
    assert isinstance(row.label, QLabel)
    assert row.label.text() == "**这不是加粗**"


def test_thinking_block_collapsed_by_default(qtbot: QtBot) -> None:
    """思考块默认折叠（§13.2），点击展开。"""
    view = ChatView()
    qtbot.addWidget(view)

    view.begin_assistant()
    view.append_thinking_delta("在想…")
    view.end_assistant("回答")

    row = view._rows[-1]
    assert row._thinking is not None
    assert row._thinking._content.isHidden()  # 默认折叠（isHidden 不依赖窗口可见性）

    row._thinking._on_toggle()
    assert not row._thinking._content.isHidden()
    assert "在想" in row._thinking._content.text()


def test_thinking_only_on_assistant(qtbot: QtBot) -> None:
    """思考块只挂在助手消息上，用户消息忽略。"""
    view = ChatView()
    qtbot.addWidget(view)
    row = view._add_bubble("hi", role="user")
    row.append_thinking("不该出现")
    assert row._thinking is None


def test_load_history_restores_thinking(qtbot: QtBot) -> None:
    """历史里的 thinking 落回折叠块。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history([("assistant", "回答", "当时的思考", "m9")])
    row = view._rows[-1]
    assert row._thinking is not None
    assert row._thinking._content.text() == "当时的思考"
    assert row._thinking._content.isHidden()


# ---------- 分支 UI（Task 3.2） ----------


def test_user_edit_button_shows_on_hover_without_moving_bubble(qtbot: QtBot) -> None:
    """用户消息悬停出「编辑」按钮（位置常驻，气泡不挪动），点击外发 message_id。"""
    from PySide6.QtWidgets import QFrame

    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.raise_()
    view.activateWindow()
    qtbot.waitExposed(view)
    view.add_user_message("改我", "m1")
    row = view._rows[-1]
    bubble = row.findChild(QFrame, "messageBubble")
    assert row._edit_btn is not None and row._edit_btn.isHidden()
    qtbot.waitUntil(lambda: bubble.width() > 0)
    before = bubble.geometry()

    qtbot.mouseMove(view, QPoint(1, 1))
    qtbot.mouseMove(bubble)
    qtbot.waitUntil(row._edit_btn.isVisible)
    assert bubble.geometry() == before

    with qtbot.waitSignal(view.edit_message_requested, timeout=1000) as blocker:
        qtbot.mouseClick(row._edit_btn, Qt.MouseButton.LeftButton)
    assert blocker.args == ["m1"]


def test_edit_in_composer_submits_edit_and_cancel_clears(qtbot: QtBot) -> None:
    """编辑态借用输入框：发送走 edit_submitted（附件栏保留给上层装配），取消清空。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.attachments.add_attachment("doc-1", "a.md", "3 行")
    view.begin_edit("m1", "原文")
    assert view.editing_message_id == "m1"
    assert view._input.toPlainText() == "原文"

    with qtbot.waitSignal(view.edit_submitted, timeout=1000) as blocker:
        view._on_send()
    assert blocker.args == ["m1", "原文"]
    assert view.editing_message_id is None
    assert view.attachments.attachment_ids() == ["doc-1"]

    view.begin_edit("m1", "原文")
    view.cancel_edit()
    assert view.editing_message_id is None
    assert view._input.toPlainText() == ""
    assert view.attachments.attachment_ids() == []

def test_assistant_fork_button_emits_regenerate(qtbot: QtBot) -> None:
    """助手回复下方的 Fork：外发 message_id，应用层在新分支上重新生成这条回复。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.load_history([("assistant", "回答", "", "m2")])
    row = view._rows[-1]
    assert row._fork_btn is not None and row._fork_btn.isVisible()

    with qtbot.waitSignal(view.regenerate_requested, timeout=1000) as blocker:
        qtbot.mouseClick(row._fork_btn, Qt.MouseButton.LeftButton)
    assert blocker.args == ["m2"]


def test_streaming_row_gets_actions_after_finalize(qtbot: QtBot) -> None:
    """流式中的回复不露动作栏；定稿补上 message_id 后，复制与 Fork 出现在正文下方。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    view.begin_assistant()
    view.append_assistant_delta("半截")
    row = view._stream_row
    assert row is not None and row._actions is not None
    assert row._message_id is None
    assert row._actions.isHidden()

    view.end_assistant("定稿", "m-final")
    assert row._message_id == "m-final"
    assert row._copy_btn is not None and row._copy_btn.isVisible()
    assert row._fork_btn is not None and row._fork_btn.isVisible()
    actions = row._actions
    qtbot.waitUntil(lambda: actions.y() > row.label.geometry().bottom())


def test_assistant_message_end_is_not_presented_as_run_end(qtbot: QtBot) -> None:
    """正文结束后若整轮仍 busy，要明确提示仍在处理，并暂不露最终动作。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()

    view.set_busy(True)
    view.begin_assistant()
    view.append_assistant_delta("我先看一下。")
    row = view._stream_row
    assert row is not None

    view.end_assistant("我先看一下。", "m-live")

    assert row._run_state is not None
    assert row._run_state.isVisible()
    assert "仍在处理" in row._run_state.text()
    assert row._actions is not None and row._actions.isHidden()

    view.set_busy(False)
    assert row._run_state.isHidden()
    assert row._actions.isVisible()


def test_busy_opens_card_with_composing_hint(qtbot: QtBot) -> None:
    """一轮开始即预立卡片：正文位置顶部出现「正在构思……N秒」，落字即收。

    assistant_start 沿用预开的首段（不另开段、计时不清零）；思考流也算构思，
    首个正文 delta 才结束构思。
    """
    view = ChatView()
    qtbot.addWidget(view)
    view.show()

    view.add_user_message("你好")
    view.set_busy(True)
    card = view._rows[-1]
    assert card._role == "assistant"
    assert len(card._segments) == 1
    hint = card._segments[0].hint
    assert hint is not None and hint.isVisible()
    assert "正在构思" in hint.text()

    hint._tick()
    assert "1秒" in hint.text()

    # assistant_start 到来：沿用预开段，计时继续
    view.begin_assistant()
    assert len(card._segments) == 1
    assert hint.isVisible()

    # 思考流也算构思：提示不收
    view.append_thinking_delta("想一想……")
    assert hint.isVisible()

    # 首个正文 delta：构思结束
    view.append_assistant_delta("正文来了")
    assert hint.isHidden()
    view.end_assistant("正文来了", "m1")
    view.set_busy(False)


def test_composing_hint_reappears_for_next_segment(qtbot: QtBot) -> None:
    """工具之后再写正文：新段顶部重新出现构思计时，落字即收。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    card = view._rows[-1]

    view.begin_assistant()  # 沿用预开的首段
    view.append_assistant_delta("第一段")
    view.end_assistant("第一段", "m1")
    view.note_tool_step("read_file", "call-1", is_error=False, phase="start")
    view.note_tool_step("read_file", "call-1", is_error=False, phase="end")

    view.begin_assistant()  # 第二条模型消息：新段、新计时
    assert len(card._segments) == 2
    hint = card._segments[1].hint
    assert hint is not None and hint.isVisible()
    assert "正在构思" in hint.text()

    view.append_assistant_delta("第二段")
    assert hint.isHidden()
    view.set_busy(False)


def test_tool_start_ends_composing_hint(qtbot: QtBot) -> None:
    """模型不落字直接调工具：工具开始执行即结束构思。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    card = view._rows[-1]
    hint = card._segments[0].hint
    assert hint is not None and not hint.isHidden()

    view.note_tool_step("run_cmd", "call-1", is_error=False, phase="start")
    assert hint.isHidden()
    view.set_busy(False)


def test_settle_drops_card_that_never_got_content(qtbot: QtBot) -> None:
    """发起即失败：没等到任何内容的预立卡在整轮收敛时撤掉，不留空面板。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("你好")
    view.set_busy(True)
    assert [row._role for row in view._rows] == ["user", "assistant"]

    view.add_error("网络错误")
    view.set_busy(False)

    assert [row._role for row in view._rows] == ["user", "error"]
    assert view._stream_row is None
    assert view._run_rows == []


def test_history_render_has_no_visible_composing_hint(qtbot: QtBot) -> None:
    """历史重载不走构思计时：定稿段落不残留可见提示。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(
        [
            ("user", "第一轮", "", "m1"),
            ("assistant", "回复一", "", "m2"),
        ]
    )
    for row in view._rows:
        for segment in getattr(row, "_segments", []):
            assert segment.hint is None or segment.hint.isHidden()


def test_multi_message_run_merges_into_one_card(qtbot: QtBot) -> None:
    """同一轮 run 的多条模型消息并进**同一张卡**：段间细分隔线，收敛前不露动作。

    长任务（正文 → 工具 → 正文 → …）此前每条消息一张卡，读起来碎；合并后
    整轮是一张卡，中间过程正文完整显示，状态行在卡底提示进度。
    """
    from PySide6.QtWidgets import QFrame

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)

    view.begin_assistant()
    view.append_assistant_delta("我先查一下。")
    view.end_assistant("我先查一下。", "m-first")
    card = view._rows[-1]
    assert len(view._rows) == 1

    # 工具调用发生在两条消息之间：chip 跟在触发它的那段后面
    view.note_tool_step("read_file", "call-1", is_error=False, phase="start")
    view.note_tool_step("read_file", "call-1", is_error=False, phase="end")

    # 第二条模型消息：并入同一张卡，不再另开新卡
    view.begin_assistant()
    view.append_assistant_delta("查完了，这是结论。")
    view.end_assistant("查完了，这是结论。", "m-final")

    assert len(view._rows) == 1  # 一轮 run 一张卡
    assert len(card._segments) == 2
    assert card.content_text() == "我先查一下。\n\n查完了，这是结论。"
    assert card._run_state is not None and card._run_state.isVisible()  # 整轮仍在进行
    assert card._actions is not None and card._actions.isHidden()
    # 段间细分隔线（首段之前不画）
    dividers = [w for w in card._assistant_panel.findChildren(QFrame) if w.maximumHeight() == 1]
    assert len(dividers) == 1
    # 工具步骤归属第一段；第二段没有
    assert card._segments[0].tool_steps_view is not None
    assert card._segments[1].tool_steps_view is None
    # Fork 点随最新一段前移（一轮 run 的分叉落在它最后一条消息上）
    assert card._message_id == "m-final"

    view.set_busy(False)
    assert card._run_state.isHidden()
    assert card._actions.isVisible()


def test_separate_runs_get_separate_cards(qtbot: QtBot) -> None:
    """两轮独立 run 各一张卡：set_busy 的起落清掉上一轮的卡片状态。"""
    view = ChatView()
    qtbot.addWidget(view)

    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("第一轮", "m1")
    view.set_busy(False)

    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("第二轮", "m2")
    view.set_busy(False)

    assert len(view._rows) == 2
    assert [row.content_text() for row in view._rows] == ["第一轮", "第二轮"]


def test_copy_puts_markdown_source_on_clipboard(qtbot: QtBot) -> None:
    """复制的是 Markdown 原文；按钮短暂换成对勾，随后换回。"""
    from PySide6.QtGui import QGuiApplication

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    source = "看代码：\n\n```python\nprint(1)\n```"
    view.load_history([("assistant", source, "", "m1")])
    button = view._rows[-1]._copy_btn
    assert button is not None

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    assert QGuiApplication.clipboard().text() == source
    assert button.icon_name == "check"
    qtbot.waitUntil(lambda: button.icon_name == "copy", timeout=3000)


def test_user_and_assistant_messages_use_distinct_panels(qtbot: QtBot) -> None:
    """用户消息用收窄气泡；助手回复用铺满消息栏的背景板。"""
    from PySide6.QtWidgets import QFrame

    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1200, 800)
    view.show()
    view.load_history([("user", "问", "", "m1"), ("assistant", "答" * 10, "", "m2")])
    user_row, assistant_row = view._rows

    assert user_row.findChild(QFrame, "messageBubble") is not None
    assert assistant_row.findChild(QFrame, "messageBubble") is None
    panel = assistant_row.findChild(QFrame, "assistantMessagePanel")
    assert panel is not None
    qtbot.waitUntil(lambda: panel.width() == view._column_width)
    assert assistant_row.label.width() < panel.width()


def test_user_bubble_width_follows_text(qtbot: QtBot) -> None:
    """用户气泡随文字收窄、靠右；长消息封顶在栏宽的一定比例处才折行。"""
    from PySide6.QtWidgets import QFrame

    from limbowave.ui.chat_view import _USER_BUBBLE_RATIO

    view = ChatView()
    qtbot.addWidget(view)
    view.resize(1200, 800)
    view.show()
    view.add_user_message("短", "m1")
    view.add_user_message("很长的一句话，" * 40, "m2")
    short_row, long_row = view._rows
    short = short_row.findChild(QFrame, "messageBubble")
    long = long_row.findChild(QFrame, "messageBubble")

    qtbot.waitUntil(lambda: long.width() == int(view._column_width * _USER_BUBBLE_RATIO))
    assert short.width() < long.width() / 4
    assert short.geometry().right() == short_row.width() - 1  # 靠右贴着消息栏右缘


def test_message_column_is_centered_and_capped(qtbot: QtBot) -> None:
    """消息排在居中的一栏：宽对话区里栏宽与输入框一致，窄时两侧仍留白。"""
    from limbowave.ui.chat_view import _COMPOSER_MAX_WIDTH, _MESSAGE_SIDE_MIN

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.resize(1400, 800)
    qtbot.waitUntil(lambda: view._column_width == _COMPOSER_MAX_WIDTH)
    margins = view._transcript.contentsMargins()
    assert margins.left() == margins.right() > _MESSAGE_SIDE_MIN
    viewport = view._scroll.viewport().width()
    assert margins.left() + view._column_width + margins.right() == viewport

    view.resize(600, 800)
    qtbot.waitUntil(lambda: view._transcript.contentsMargins().left() == _MESSAGE_SIDE_MIN)
    viewport = view._scroll.viewport().width()
    assert view._column_width == viewport - 2 * _MESSAGE_SIDE_MIN


def test_end_assistant_without_stream_renders_text(qtbot: QtBot) -> None:
    """没经过流式（没有 begin_assistant）直接定稿的回复，也要显示正文与动作栏。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.end_assistant("**直接**定稿", "m1")
    row = view._rows[-1]
    assert row.content_text() == "直接定稿"
    assert row._actions is not None and not row._actions.isHidden()


def test_user_text_that_looks_like_html_stays_literal(qtbot: QtBot) -> None:
    """用户输入按原样显示：像 HTML 的内容也不当富文本渲染。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.add_user_message("<b>不加粗</b>", "m1")
    label = view._rows[-1].label
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == "<b>不加粗</b>"


def test_no_separate_branch_bar(qtbot: QtBot) -> None:
    """分支切换已移到左栏（双击会话行展开），消息区不再有单独的分支状态栏。"""
    view = ChatView()
    qtbot.addWidget(view)
    assert not hasattr(view, "_branch_bar")
    assert not hasattr(view, "branch_switch_requested")


# ---------- 长会话滞性化（Task 0.2：2,000 条消息可接受地滚动） ----------


def _history(n: int) -> list[tuple[str, str, str, str | None]]:
    return [("user" if i % 2 == 0 else "assistant", f"消息 {i}", "", f"m{i}") for i in range(n)]


def test_long_history_materializes_only_recent_page(qtbot: QtBot) -> None:
    """2000 条历史只物化最近一页——全量物化富文本组件会卡死。"""
    from limbowave.ui.chat_view import HISTORY_PAGE

    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(2000))

    rows = list(view._rows)
    assert len(rows) == HISTORY_PAGE  # 只物化最近一页
    assert view._history_offset == 2000 - HISTORY_PAGE


def test_shows_load_earlier_entry_for_hidden(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QPushButton

    from limbowave.ui.chat_view import HISTORY_PAGE

    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(200))
    buttons = [b for b in view.findChildren(QPushButton) if "加载更早" in b.text()]
    assert len(buttons) == 1
    assert f"{200 - HISTORY_PAGE}" in buttons[0].text()  # 告知还有多少条


def test_load_earlier_expands_one_page(qtbot: QtBot) -> None:
    from limbowave.ui.chat_view import HISTORY_PAGE

    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(200))
    before = view._history_offset

    view._load_earlier()
    assert view._history_offset == max(0, before - HISTORY_PAGE)
    assert len(view._rows) == min(200, 2 * HISTORY_PAGE)


def test_load_earlier_until_exhausted(qtbot: QtBot) -> None:
    """一页一页加载到顶后，入口消失且全部消息都在。"""
    from limbowave.ui.chat_view import HISTORY_PAGE

    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(HISTORY_PAGE + 5))
    while view._history_offset > 0:
        view._load_earlier()
    assert view._history_offset == 0
    assert len(view._rows) == HISTORY_PAGE + 5
    from PySide6.QtWidgets import QPushButton

    assert not [b for b in view.findChildren(QPushButton) if "加载更早" in b.text()]


def test_short_history_materializes_all(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(10))
    assert len(view._rows) == 10
    assert view._history_offset == 0


def test_switching_conversation_resets_history(qtbot: QtBot) -> None:
    """切会话后旧历史不得残留（否则会串台）。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(200))
    view.load_history(_history(3))
    assert view._full_history == [HistoryEntry(*m) for m in _history(3)]
    assert view._history_offset == 0


# ---------- 一轮 run 一卡（历史重载按 run_id 分组） ----------


def test_history_groups_one_run_into_one_card(qtbot: QtBot) -> None:
    """历史重载：同 run_id 的助手消息渲染进同一张卡；run_id=None 退回一卡一条。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(
        [
            HistoryEntry("user", "问题", "", "m1"),
            HistoryEntry("assistant", "先查工具", "", "m2", "run-1"),
            HistoryEntry("assistant", "查完了，结论是……", "", "m3", "run-1"),
            HistoryEntry("user", "追问", "", "m4"),
            HistoryEntry("assistant", "单独一轮的回复", "", "m5"),  # 旧数据没有 run_id
        ]
    )

    # 用户消息 + run 卡（两条并成一张）+ 用户消息 + 单独回复
    assert len(view._rows) == 4
    run_card = view._rows[1]
    assert len(run_card._segments) == 2
    assert run_card.content_text() == "先查工具\n\n查完了，结论是……"
    assert run_card._message_id == "m3"  # Fork 点 = run 的最后一条消息
    assert view._rows[3]._message_id == "m5"
    assert len(view._rows[3]._segments) == 1


def test_history_tool_steps_follow_segments_by_message_id(qtbot: QtBot) -> None:
    """历史里的工具步骤按 message_id 归段：哪段调的工具，chip 就在哪段下面。"""
    from limbowave.domain.tool_step import ToolStep

    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(
        [
            HistoryEntry(
                "assistant",
                "先查工具",
                "",
                "m2",
                "run-1",
                (ToolStep(tool_call_id="c1", name="read_file"),),
            ),
            HistoryEntry("assistant", "结论", "", "m3", "run-1"),
        ]
    )
    card = view._rows[-1]
    assert card._segments[0].tool_steps_view is not None
    assert card._segments[1].tool_steps_view is None


def test_history_restores_thinking_per_segment(qtbot: QtBot) -> None:
    """历史里的 thinking 落回各自分段的折叠块。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(
        [
            HistoryEntry("assistant", "段一", "思考一", "m2", "run-1"),
            HistoryEntry("assistant", "段二", "思考二", "m3", "run-1"),
        ]
    )
    card = view._rows[-1]
    assert card._segments[0].thinking is not None
    assert card._segments[0].thinking._content.text() == "思考一"
    assert card._segments[1].thinking is not None
    assert card._segments[1].thinking._content.text() == "思考二"
    # 两个思考块都默认折叠
    assert card._segments[0].thinking._content.isHidden()
    assert card._segments[1].thinking._content.isHidden()


def test_copy_on_run_card_joins_segment_sources(qtbot: QtBot) -> None:
    """合并卡的复制：整卡 Markdown 原文，各段按空行连接。"""
    from PySide6.QtGui import QGuiApplication

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view.set_busy(True)
    view.begin_assistant()
    view.end_assistant("第一段", "m1")
    view.begin_assistant()
    view.end_assistant("第二段", "m2")
    view.set_busy(False)

    button = view._rows[-1]._copy_btn
    assert button is not None
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "第一段\n\n第二段"


def test_load_earlier_does_not_split_a_run(qtbot: QtBot) -> None:
    """翻页边界落在某轮 run 中间时，窗口起点回退到该轮首条——整轮完整可见。"""
    entries = [HistoryEntry("user", f"前置 {i}", "", f"p{i}") for i in range(4)]
    entries += [HistoryEntry("assistant", f"段 {i}", "", f"r{i}", "run-9") for i in range(3)]
    entries += [
        HistoryEntry("user" if i % 2 == 0 else "assistant", f"后续 {i}", "", f"q{i}")
        for i in range(HISTORY_PAGE - 2)
    ]
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(entries)

    raw_start = len(entries) - HISTORY_PAGE  # 不做对齐时窗口会从 run 中间开始
    assert raw_start == 5
    assert entries[raw_start].run_id == "run-9"
    assert view._history_offset == 4  # 回退到该轮首条，整轮并入窗口
    assert len(view._rows[0]._segments) == 3


# ---------- 键盘操作（Task 8.4 / §16.3 要求覆盖「键盘操作」） ----------


def test_ctrl_enter_sends(qtbot: QtBot) -> None:
    """Ctrl+Enter 发送——占位文本一直这么承诺，此前从未实现（真缺陷）。"""
    from PySide6.QtCore import Qt

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    qtbot.addWidget(view)
    view._input.setPlainText("快捷键发送")
    view._input.setFocus()

    with qtbot.waitSignal(view.message_submitted, timeout=1000) as blocker:
        qtbot.keyClick(view._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)

    assert blocker.args == ["快捷键发送"]
    assert view._input.toPlainText() == ""  # 发送后清空


def test_plain_enter_does_not_send(qtbot: QtBot) -> None:
    """裸 Enter 是换行，不是发送（否则多行输入没法写）。"""
    from PySide6.QtCore import Qt

    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    fired: list[str] = []
    view.message_submitted.connect(fired.append)
    view._input.setPlainText("第一行")
    view._input.setFocus()

    qtbot.keyClick(view._input, Qt.Key.Key_Return)
    qtbot.wait(50)

    assert fired == []
    assert "第一行" in view._input.toPlainText()  # 内容还在


def test_tab_moves_focus_not_insert_tab(qtbot: QtBot) -> None:
    """Tab 用于键盘导航（可访问性），不该在输入框里插入制表符。"""
    assert ChatView()._input.tabChangesFocus() is True


def _paste(view: ChatView, mime: QMimeData) -> None:
    """模拟 Ctrl+V：写入系统剪贴板后对输入框按下粘贴键。"""
    from PySide6.QtGui import QGuiApplication, QKeySequence

    QGuiApplication.clipboard().setMimeData(mime)
    view._input.setFocus()
    view._input.keyPressEvent(
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_V,
            Qt.KeyboardModifier.ControlModifier,
            QKeySequence(QKeySequence.StandardKey.Paste).toString(),
        )
    )


def test_ctrl_v_local_files_become_attachments(qtbot: QtBot, tmp_path: Path) -> None:
    """资源管理器里复制的文件，Ctrl+V 进附件而不是把路径插成文本。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    target = tmp_path / "notes.md"
    target.write_text("# hi", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])

    with qtbot.waitSignal(view.files_dropped, timeout=1000) as blocker:
        _paste(view, mime)

    assert [Path(p) for p in blocker.args[0]] == [target]
    assert view._input.toPlainText() == ""


def test_ctrl_v_image_emits_png_bytes(qtbot: QtBot) -> None:
    """截图等纯图片，Ctrl+V 转成 PNG 字节外发。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    image = QImage(4, 3, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    mime = QMimeData()
    mime.setImageData(image)

    with qtbot.waitSignal(view.image_pasted, timeout=1000) as blocker:
        _paste(view, mime)

    raw = blocker.args[0]
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert QImage.fromData(raw).size() == image.size()
    assert view._input.toPlainText() == ""


def test_ctrl_v_text_with_image_pastes_text(qtbot: QtBot) -> None:
    """Word / Excel 复制的内容同时带文本和位图：粘贴文本，不当成图片附件。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    image = QImage(4, 3, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    mime = QMimeData()
    mime.setText("表格内容")
    mime.setImageData(image)
    fired: list[bytes] = []
    view.image_pasted.connect(fired.append)

    _paste(view, mime)

    assert fired == []
    assert view._input.toPlainText() == "表格内容"


def test_retry_notice_visible_in_transcript(qtbot: QtBot) -> None:
    """重试必须显示在消息流里（§八.3：不得偷偷重发）。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.add_retry_notice("第 1 次尝试失败：连接失败", attempt=2, delay_ms=1000, max_attempts=3)

    from PySide6.QtWidgets import QLabel

    texts = [w.text() for w in view.findChildren(QLabel)]
    joined = "\n".join(texts)
    assert "第 2/3 次尝试" in joined
    assert "连接失败" in joined
    assert "1.0s 后重试" in joined



def test_composer_is_single_compact_container(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    assert view._composer.acceptDrops()
    assert view._attachment_bar.parent() is not None
    assert view._usage_bar.parent() is not None
    assert view._send_btn.text() == "↑"
    assert view._attach_btn.accessibleName() == "添加附件"
    assert view._permission_btn.accessibleName() == "会话权限"


def test_composer_tool_buttons_highlight_on_hover(qtbot: QtBot) -> None:
    """附件 + 按钮与权限按钮：悬停要有可见高亮——底色走 QSS、加号走自绘，两路都锁住。

    悬浮事件本身在无头/合成鼠标环境里不可靠（同仓库已有用例因此失败），
    这里锁的是两段高亮机制：自身样式表里的 :hover 半透明底色，以及自绘
    加号对 ``WA_UnderMouse`` 的响应。像素采样按 devicePixelRatio 换算，
    高 DPI 屏上 grab() 的图像是物理像素。"""
    view = ChatView()
    qtbot.addWidget(view)

    for button in (view._attach_btn, view._permission_btn):
        hover_block = button.styleSheet().split("QPushButton:hover", 1)[1].split("}", 1)[0]
        assert "rgba(" in hover_block, button.accessibleName()  # 悬浮底色不是 transparent

    # 自绘加号跟随悬停变色：比较整幅不透明笔画的颜色集合
    def stroke_colors(image: QImage) -> set[str]:
        return {
            image.pixelColor(x, y).name()
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alphaF() > 0.5
        }

    button = view._attach_btn
    rest = stroke_colors(button.grab().toImage())
    assert rest, "加号未绘制"
    button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    hover = stroke_colors(button.grab().toImage())
    button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    left = stroke_colors(button.grab().toImage())

    assert hover != rest
    assert left == rest


def test_permission_menu_has_presets_and_custom(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    labels = [
        action.text()
        for action in view._permission_menu.actions()
        if not action.isSeparator()
    ]

    assert [label.split()[0] for label in labels] == ["仅聊天", "自由读取", "完全访问", "自定义"]
    assert view.permission_preset is PermissionPreset.READ_ONLY

    with qtbot.waitSignal(view.permission_preset_changed, timeout=1000) as blocker:
        view._permission_menu._actions[PermissionPreset.CHAT_ONLY].trigger()
    assert blocker.args == [PermissionPreset.CHAT_ONLY.value]

    view.set_permission_preset(PermissionPreset.READ_ONLY)
    assert view.permission_preset is PermissionPreset.READ_ONLY
    assert view._permission_btn.text() == "自由读取"


def test_custom_permission_action_requests_panel(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    with qtbot.waitSignal(view.permission_custom_requested, timeout=1000):
        view._permission_menu._actions[PermissionPreset.CUSTOM].trigger()


def test_logical_model_is_shown_without_endpoint(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.set_logical_model("deepseek-v4.1-flash")

    assert view._logical_model.currentText() == "deepseek-v4.1-flash"
    assert "实际站点" not in view._logical_model.toolTip()


def test_logical_model_selector_shows_display_name(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_logical_models([("model-a", "模型 A"), ("model-b", "模型 B")], "model-a")

    assert view._logical_model.currentText() == "模型 A"

    with qtbot.waitSignal(view.logical_model_changed, timeout=1000) as blocker:
        view._logical_model.setCurrentIndex(1)

    assert view._logical_model.currentText() == "模型 B"
    assert blocker.args == ["model-b"]


def test_context_usage_uses_ring(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)

    view.set_context_usage(72.5, "72.5% · 116k / 160k")

    assert view._usage_bar.percent == 72.5
    assert "116k / 160k" in view._usage_bar.toolTip()


def test_busy_swaps_send_for_stop(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.show()

    view.set_busy(True)
    assert view._stop_btn.isVisible()
    assert not view._send_btn.isVisible()

    view.set_busy(False)
    assert not view._stop_btn.isVisible()
    assert view._send_btn.isVisible()


def test_status_row_is_gone(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.set_status("正在生成压缩摘要…")
    assert view.status_text == "正在生成压缩摘要…"
    assert not hasattr(view, "_status")
    assert not hasattr(view, "_mode_badge")


def test_compression_masks_composer_and_drains_progress(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view._input.setPlainText("待发送")

    view.begin_compression(40.0)

    overlay = view._composer.compression_overlay
    assert view.compressing
    assert overlay.isVisible()
    assert overlay.geometry() == view._composer.rect()
    assert not view._input.isEnabled()
    assert not view._send_btn.isEnabled()
    assert view.compression_progress == 40.0
    # 每秒千分之三：按 100ms 一步走，每步 0.03 个百分点
    view._drain_compression()
    assert abs(view.compression_progress - (40.0 - COMPRESSION_DRAIN_PER_SEC / 10)) < 1e-9


def test_compression_finish_settles_to_after_percent_then_unmasks(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.show()
    view._input.setPlainText("待发送")
    view.begin_compression(40.0)

    view.finish_compression(12.5)

    qtbot.waitUntil(lambda: not view.compressing, timeout=3000)
    assert view.compression_progress == 12.5
    assert not view._composer.compression_overlay.isVisible()
    assert view._input.isEnabled()
    assert view._send_btn.isEnabled()


def test_compression_failure_returns_to_start_percent(qtbot: QtBot) -> None:
    view = ChatView()
    qtbot.addWidget(view)
    view.begin_compression(40.0)
    for _ in range(5):
        view._drain_compression()

    view.finish_compression(None)

    qtbot.waitUntil(lambda: not view.compressing, timeout=3000)
    assert view.compression_progress == 40.0
