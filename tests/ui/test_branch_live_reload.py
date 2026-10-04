"""分支创建后，历史重载与实时气泡追加之间的回归测试。"""

from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import ChatView


def test_synchronous_branch_history_reload_preserves_immediate_live_messages(
    qtbot: QtBot,
) -> None:
    """重载继承前缀后，紧接着到来的新一轮气泡不得被延迟清掉。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)

    prefix = [
        ("user", "第一问", "", "m1"),
        ("assistant", "第一答", "", "m2"),
    ]
    # ``branched`` 同步重载前缀，随后同一调用链依次收到 user / assistant 事件。
    view.load_history(prefix)
    view.add_user_message("第二问（编辑）", "m3-new")
    view.begin_assistant()
    view.append_assistant_delta("第二答（新）")
    view.end_assistant("第二答（新）", "m4-new")

    # 等过历史转场时长，确认不存在残留动画回调覆盖新气泡。
    qtbot.wait(350)
    assert [row.content_text() for row in view._rows] == [
        "第一问",
        "第一答",
        "第二问（编辑）",
        "第二答（新）",
    ]
