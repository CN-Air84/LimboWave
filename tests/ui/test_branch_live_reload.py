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


async def test_app_fork_and_regenerate_buttons_are_wired_separately(
    qtbot, qapp, monkeypatch, tmp_path
):
    import asyncio

    from limbowave import app
    from limbowave.application.kernel import KernelSetup
    from limbowave.bootstrap import AppPaths
    from limbowave.infrastructure import shell
    from limbowave.ui.main_window import MainWindow
    from tests.unit.test_branch_path import TreeKernel

    old_stylesheet = qapp.styleSheet()
    kernel = TreeKernel()
    monkeypatch.setattr(
        app, "_build_kernel", lambda *_a, **_kw: KernelSetup(kernel, "fake", "fake", "test")
    )
    monkeypatch.setattr(shell, "probe_shell", lambda *_a: None)
    window = MainWindow()
    qtbot.addWidget(window)
    controller, _ipc, _warm, _startup, shutdown = app._wire(
        window, AppPaths(tmp_path, tmp_path / "logs"), None
    )
    try:
        await controller.send("问题")
        kernel.say("原回答")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        original_branch = controller.coordinator().branch_id
        window.chat._rows[-1]._fork_btn.click()
        for _ in range(100):
            if controller.coordinator().branch_id != original_branch:
                break
            await asyncio.sleep(0.01)
        fork_branch = controller.coordinator().branch_id
        assert fork_branch != original_branch
        assert kernel.sent == ["问题"]
        assert [row.content_text() for row in window.chat._rows] == ["问题", "原回答"]
        assert not window.chat._busy
        window.chat._rows[-1]._regenerate_btn.click()
        for _ in range(100):
            if len(kernel.sent) == 2:
                break
            await asyncio.sleep(0.01)
        assert kernel.sent == ["问题", "问题"]
        assert controller.coordinator().branch_id != fork_branch
        kernel.say("新回答")
        kernel.emit("run.settled", {})
        await controller.wait_idle()
        assert [row.content_text() for row in window.chat._rows] == ["问题", "新回答"]
    finally:
        await shutdown()
        qapp.setStyleSheet(old_stylesheet)


def test_regeneration_reuses_prefix_widgets_and_updates_paging_cache(qtbot, monkeypatch):
    from limbowave.ui.chat_view import HistoryEntry

    view = ChatView()
    qtbot.addWidget(view)
    payload = [HistoryEntry(role, text, "", mid) for role, text, mid in [
        ("user", "first", "u1"), ("assistant", "answer", "a1"),
        ("user", "second", "u2"), ("assistant", "old answer", "a2"),
    ]]
    view.load_history(payload)
    retained = view._rows[:2]

    def no_reload(*args, **kwargs):
        raise AssertionError("prefix was synchronously rendered again")

    monkeypatch.setattr(view, "load_history", no_reload)
    view.apply_branch_history(payload[:2], "u2")
    assert view._rows == retained
    assert view._full_history == payload[:2]
    view.add_user_message("second", "u2-new")
    view.begin_assistant()
    view.end_assistant("new answer", "a2-new")
    qtbot.wait(350)
    assert view._rows[:2] == retained
    assert [row.content_text() for row in view._rows] == ["first", "answer", "second", "new answer"]
