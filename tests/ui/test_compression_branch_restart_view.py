"""Render the persisted branch marker, not a transient progress separator."""

from limbowave.application.history_payload import history_payload
from limbowave.application.services.history_service import HistoryService
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.ui.chat_view import ChatView
from limbowave.ui.compression_widgets import CompressionDivider
from tests.unit.test_branch_path import _turn
from tests.unit.test_compression_runtime_regression import setup_chat


async def test_forked_separator_survives_recreated_view_and_database(qtbot, tmp_path, vault_key):
    database = tmp_path / "visible-marker.db"
    factory, kernel, coord, _service, version = await setup_chat(
        sqlite_uow_factory(database, vault_key)
    )
    assert await coord.apply_compression(version.id)
    await _turn(kernel, coord, "after compression", "after answer")
    messages = HistoryService(factory).branch_messages(coord.branch_id)
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(history_payload(messages, uow_factory=factory, branch_id=coord.branch_id))
    assert len(view.findChildren(CompressionDivider)) == 1
    events = []
    coord.subscribe(events.append)
    assert await coord.regenerate(messages[-1].id)
    event = next(e for e in events if e.kind == "branched")
    assert view.apply_branch_history(event.data["history"], messages[-2].id)
    assert len(view.findChildren(CompressionDivider)) == 1
    kernel.say("regenerated answer")
    kernel.emit("run.settled", {})
    await coord.wait_idle()
    branch = coord.branch_id
    await coord.shutdown()
    factory.close()

    factory = sqlite_uow_factory(database, vault_key)
    reopened = ChatView()
    qtbot.addWidget(reopened)
    messages = HistoryService(factory).branch_messages(branch)
    await reopened.load_history_incrementally(
        history_payload(messages, uow_factory=factory, branch_id=branch)
    )
    dividers = reopened.findChildren(CompressionDivider)
    assert len(dividers) == 1
    assert "会话已压缩" in dividers[0]._label.text()
    factory.close()
