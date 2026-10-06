"""Desktop transcript isolation when a paired device changes the runtime target."""

from limbowave.application.services.run_origin import remote_origin
from tests.ui.test_session_switch_during_generation import wired as wired


async def test_remote_target_does_not_mix_into_desktop_transcript(wired):
    window, controller, kernel, history_id, history_branch, _ = wired
    kernel.say("desktop result")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    before = [row.content_text() for row in window.chat._rows]
    window.chat._input.setPlainText("desktop unsent draft")
    with remote_origin():
        controller.coordinator().notify_remote_target_changing(history_id, history_branch)
        assert await controller.switch_conversation(history_id, history_branch)
        await controller.send("phone-only question")
    kernel.say("phone-only answer")
    kernel.emit("run.settled", {})
    await controller.wait_idle()
    assert [row.content_text() for row in window.chat._rows] == before
    assert window.chat._input.toPlainText() == "desktop unsent draft"
    assert not window.chat._busy
    # Selecting the remote target must load it, not reuse the detached old widgets.
    from tests.ui.test_session_switch_during_generation import browse_history

    await browse_history(window, history_id)
    assert [row.content_text() for row in window.chat._rows] == [
        "history question",
        "history answer",
        "phone-only question",
        "phone-only answer",
    ]


def test_restore_draft_does_not_destroy_new_input(qtbot):
    from limbowave.ui.chat_view import ChatView

    chat = ChatView()
    qtbot.addWidget(chat)
    chat.restore_draft("rejected text")
    assert chat._input.toPlainText() == "rejected text"
    chat._input.setPlainText("typed meanwhile")
    chat.restore_draft("older rejected text")
    assert chat._input.toPlainText() == "typed meanwhile\nolder rejected text"
