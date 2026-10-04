from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget

from limbowave.domain import platform_capabilities
from limbowave.ui import data_reset_panel
from limbowave.ui.settings_dialog import _SecurityTab


def test_linux_reset_cannot_be_enabled_by_callbacks(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(data_reset_panel, "supports_system_identity", lambda: False)
    host = QWidget()
    qtbot.addWidget(host)
    panel = data_reset_panel.DataResetPanel(host, "/tmp/limbowave")
    assert not panel._verify.isEnabled()
    assert not panel._password.isEnabled()
    assert "已禁用" in panel._status.text()
    sent = []
    panel.verification_requested.connect(lambda *args: sent.append(args))
    panel._password.setText("password")
    panel._request_verification()
    panel.verification_succeeded()
    panel.verification_failed("retry")
    panel._request_confirmation()
    assert not sent
    assert not panel._confirm.isEnabled()
    assert not panel._verify.isEnabled()


def test_linux_security_settings_do_not_offer_dpapi(qtbot, tmp_path, make_key, monkeypatch) -> None:
    from limbowave.infrastructure.crypto.vault import Vault
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: False)
    tab = _SecurityTab(Vault(tmp_path / "vault.json"))
    qtbot.addWidget(tab)
    assert tab._enable_btn.isHidden()
    assert "不支持" in tab._status.text()
    assert "加密备份" in tab._detail.text()


def test_linux_login_recovery_does_not_offer_windows_prompt(qtbot, monkeypatch, tmp_path) -> None:
    from limbowave import app
    from limbowave.infrastructure.crypto.vault import Vault
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: False)
    alerts = []
    monkeypatch.setattr(app, "_show_startup_alert", lambda *args: alerts.append(args))
    monkeypatch.setattr(
        app, "_ask_password", lambda *args: pytest.fail("unexpected password prompt"),
    )
    host = QWidget()
    qtbot.addWidget(host)
    assert app._offer_system_recovery(Vault(tmp_path / "vault.json"), host) is None
    assert alerts and "当前平台不支持" in alerts[0][2]
