from limbowave.ui.privacy_panel import PrivacyPanel
from tests.unit.test_memory_service import memory_stack as memory_stack


def test_privacy_panel_add_and_idle_guard(qtbot, memory_stack):
    service, _factory = memory_stack
    panel = PrivacyPanel(service.privacy, "c")
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: "普通会话" in panel.mode.text())
    panel.topic.setText("旧项目")
    panel.aliases.setPlainText("ALPHA\n阿尔法")
    panel.add.click()
    qtbot.waitUntil(lambda: panel.rules.count() == 1)
    assert service.privacy.policy().blocks("alpha")
    service.privacy.is_busy = lambda: True
    panel.topic.setText("另一个话题")
    panel.add.click()
    assert "停止" in panel.status.text()
    assert len(service.privacy.policy().rules) == 1
