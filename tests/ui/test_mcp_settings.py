from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.mcp_service import McpService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.mcp import McpConfiguration, McpServer
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.ui.mcp_settings import McpSettingsPage
from limbowave.ui.settings_panel import SettingsPage


@pytest.fixture
def page(qtbot, tmp_path):
    operation = AsyncMock(return_value={"tools": [{"name": "echo"}]})
    service = McpService(tmp_path / "mcp.json", operation=operation)
    page = McpSettingsPage(service)
    qtbot.addWidget(page)
    page.resize(850, 850)
    page.show()
    return page, service, operation


def populate(page):
    page.name.setText("Local server")
    page.command.setText("python")
    page.args.setPlainText('["-m", "test_server"]')


def test_save_disabled_server_without_starting_process(page):
    widget, service, operation = page
    populate(widget)
    assert not widget.resolve_pending_changes()
    widget.save()
    config = service.configuration()
    assert len(config.servers) == 1 and not config.servers[0].enabled
    assert config.servers[0].args == ["-m", "test_server"]
    assert widget.resolve_pending_changes()
    operation.assert_not_called()


def test_invalid_json_keeps_saved_configuration_and_does_not_leak(page):
    widget, service, _ = page
    populate(widget)
    widget.save()
    widget.args.setPlainText("my-secret-not-json")
    widget.save()
    assert "my-secret" not in widget.feedback.text()
    assert service.configuration().servers[0].args == ["-m", "test_server"]
    assert not widget.resolve_pending_changes()


def test_http_fields_and_environment_references(page):
    widget, service, operation = page
    widget.name.setText("Remote")
    widget.transport.setCurrentIndex(1)
    assert not widget.command.isEnabled() and widget.url.isEnabled()
    widget.url.setText("https://example.org/mcp")
    widget.header_refs.setPlainText('{"Authorization": "MCP_AUTH"}')
    widget.save()
    server = service.configuration().servers[0]
    assert server.transport == "streamable_http" and server.command == ""
    assert server.header_refs == {"Authorization": "MCP_AUTH"}
    operation.assert_not_called()


def test_discovery_requires_enablement_and_confirmation(page, monkeypatch, qtbot):
    widget, _, operation = page
    populate(widget)
    widget.save()
    widget.test_connection()
    assert "停用" in widget.feedback.text()
    operation.assert_not_called()
    widget.enabled.setChecked(True)
    widget.save()
    confirmations = []
    monkeypatch.setattr(
        "limbowave.ui.mcp_settings.ask_confirm",
        lambda parent, title, detail, callback, **kw: confirmations.append(callback),
    )
    widget.test_connection()
    operation.assert_not_called()
    confirmations[0](False)
    operation.assert_not_called()
    widget.test_connection()
    confirmations[1](True)
    qtbot.waitUntil(lambda: not widget._testing, timeout=5000)
    assert "发现 1 个工具" in widget.feedback.text()
    assert widget.results.toPlainText() == "echo"
    operation.assert_awaited_once()


def test_delete_and_unsaved_switch_protection(page, monkeypatch):
    widget, service, _ = page
    populate(widget)
    widget.save()
    first_id = widget.server_id.text()
    widget.new_server()
    populate(widget)
    widget.name.setText("Second")
    widget.save()
    widget.name.setText("Unsaved")
    widget.selector.setCurrentIndex(0)
    assert widget.server_id.text() != first_id
    assert "请先保存" in widget.feedback.text()
    widget.reload()
    monkeypatch.setattr(
        "limbowave.ui.mcp_settings.ask_confirm",
        lambda parent, title, detail, callback, **kw: callback(True),
    )
    widget.delete_server()
    assert len(service.configuration().servers) == 1


def test_corrupt_config_disables_overwrite(qtbot, tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("corrupted", encoding="utf8")
    widget = McpSettingsPage(McpService(path))
    qtbot.addWidget(widget)
    assert not widget.save_button.isEnabled()
    assert path.read_text() == "corrupted"


def test_settings_tab_is_lazy_and_dirty_close_is_guarded(qtbot, tmp_path):
    service = McpService(tmp_path / "mcp.json")
    service.save(McpConfiguration(servers=[McpServer(id="s", name="Saved", command="python")]))
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    page = SettingsPage(None, settings, None, mcp_service=service)
    qtbot.addWidget(page)
    assert page._mcp_tab is None
    assert page._rail.index_of("MCP 服务器") >= 0
    page.select_tab("MCP 服务器")
    assert isinstance(page._mcp_tab, McpSettingsPage)
    page._mcp_tab.name.setText("Unsaved")
    assert not page.resolve_pending_changes()
    page._mcp_tab.reload()
    assert page.resolve_pending_changes()
