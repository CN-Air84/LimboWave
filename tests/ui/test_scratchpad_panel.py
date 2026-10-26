"""Transparency, explicit clear, and conflict-safe editing for short-term notes."""
from PySide6.QtCore import Qt

from limbowave.domain.memory import MemoryRunContext
from limbowave.ui.scratchpad_panel import ArchivedMemoryPanel, ScratchpadPanel
from limbowave.ui.session_memory_panel import MemoryPanel
from tests.unit.test_memory_service import memory_stack as memory_stack


def test_scratchpad_save_and_conflict_preserves_draft(qtbot, memory_stack):
    service, _ = memory_stack
    panel = ScratchpadPanel(service, "c", "b")
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: not panel._busy)
    panel.editor.setPlainText("近期计划")
    qtbot.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not panel._busy)
    assert service.read_scratchpad("c", "b").content == "近期计划"
    service.write_scratchpad("c", "b", "AI changed", 1)
    panel.editor.setPlainText("my draft")
    qtbot.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not panel._busy)
    assert "变化" in panel.status.text()
    assert panel.editor.toPlainText() == "my draft"
    assert service.read_scratchpad("c", "b").content == "AI changed"
    panel.editor.setPlainText("x" * 4001)
    assert not panel.save_button.isEnabled()


def test_scratchpad_clear_requires_confirmation(qtbot, memory_stack, monkeypatch):
    service, _ = memory_stack
    service.write_scratchpad("c", "b", "note", 0)
    panel = ScratchpadPanel(service, "c", "b")
    qtbot.addWidget(panel)
    qtbot.waitUntil(lambda: not panel._busy)
    monkeypatch.setattr("limbowave.ui.scratchpad_panel.ask_confirm",
                        lambda _parent, _title, _detail, callback: callback(False))
    panel.clear_button.click()
    assert service.read_scratchpad("c", "b").content == "note"
    monkeypatch.setattr("limbowave.ui.scratchpad_panel.ask_confirm",
                        lambda _parent, _title, _detail, callback: callback(True))
    panel.clear_button.click()
    qtbot.waitUntil(lambda: not panel._busy)
    assert service.read_scratchpad("c", "b").content == ""


def test_archived_memory_can_be_inspected_and_restored(qtbot, memory_stack):
    service, _ = memory_stack
    item = service.save("finished\nfull details", "c", "b")
    context = MemoryRunContext("r", "c", "b", "msg")
    change = service.prepare_mutation(context, "delete_session_memory", item.id)
    service.approve_mutation(context, "call", change)
    service.mutate_from_model(context, "call", change, lambda _: True)
    panel = ArchivedMemoryPanel(service, "c", "b")
    qtbot.addWidget(panel)
    qtbot.waitUntil(panel.isEnabled)
    panel.items.setCurrentRow(0)
    assert panel.preview.toPlainText() == item.content
    panel.restore_button.click()
    qtbot.waitUntil(panel.isEnabled)
    assert service.list("c", "b") == [item]
    assert panel.items.count() == 0


def test_only_session_memory_panel_has_scratchpad(qtbot, memory_stack):
    service, _ = memory_stack
    session = MemoryPanel(service, conversation_id="c", branch_id="b")
    global_panel = MemoryPanel(service)
    for panel in (session, global_panel):
        qtbot.addWidget(panel)
        qtbot.waitUntil(panel.isEnabled)
    assert isinstance(session.scratchpad, ScratchpadPanel)
    assert not hasattr(global_panel, "scratchpad")
    assert "透明便签" in [session._subtabs.tabText(i) for i in range(session._subtabs.count())]


def test_clear_confirmation_belongs_to_enclosing_memory_popup(qtbot, memory_stack, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from limbowave.ui.floating import FloatingPanel

    service, _ = memory_stack
    service.write_scratchpad("c", "b", "note", 0)
    host = QWidget()
    host.resize(1000, 800)
    qtbot.addWidget(host)
    outer = FloatingPanel(host, "会话记忆", width=660)
    panel = MemoryPanel(service, outer, conversation_id="c", branch_id="b")
    outer.content_layout.addWidget(panel)
    qtbot.waitUntil(lambda: panel.isEnabled() and not panel.scratchpad._busy)
    parents = []

    def confirm(parent, _title, _detail, callback):
        parents.append(parent)
        callback(False)

    monkeypatch.setattr("limbowave.ui.scratchpad_panel.ask_confirm", confirm)
    panel.scratchpad.clear_button.click()
    assert parents == [outer]
    assert service.read_scratchpad("c", "b").content == "note"


def test_archive_restore_refreshes_formal_memory_list(qtbot, memory_stack):
    service, _ = memory_stack
    item = service.save("finished", "c", "b")
    context = MemoryRunContext("r", "c", "b", "msg")
    change = service.prepare_mutation(context, "delete_session_memory", item.id)
    service.approve_mutation(context, "call", change)
    service.mutate_from_model(context, "call", change, lambda _: True)
    panel = MemoryPanel(service, conversation_id="c", branch_id="b")
    qtbot.addWidget(panel)
    qtbot.waitUntil(lambda: panel.isEnabled() and panel.archive.isEnabled())
    assert panel.items.count() == 0
    panel.archive.items.setCurrentRow(0)
    panel.archive.restore_button.click()
    qtbot.waitUntil(lambda: panel.isEnabled() and panel.items.count() == 1)
    assert panel.items.item(0).data(Qt.ItemDataRole.UserRole).id == item.id
