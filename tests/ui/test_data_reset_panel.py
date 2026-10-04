"""重置面板的等待、取消及应用接线；不弹真实系统验证窗，不接触用户数据。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from limbowave.app import _open_data_reset
from limbowave.application.services import data_reset_service as reset
from limbowave.application.services.data_reset_service import DataResetService, ResetScope
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.ui import data_reset_panel as reset_ui
from limbowave.ui.data_reset_panel import DataResetPanel
from tests.conftest import TEST_PASSWORD


@pytest.fixture(autouse=True)
def simulated_windows_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reset_ui, "supports_system_identity", lambda: True)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(reset_ui, "time", clock)
    monkeypatch.setattr(reset, "time", clock)
    return now


@pytest.fixture
def host(qtbot: QtBot) -> QWidget:
    widget = QWidget()
    widget.resize(1000, 800)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_scope_selection_and_password_are_locked_during_verification(
    host: QWidget, qtbot: QtBot,
) -> None:
    panel = DataResetPanel(host, "test-data")
    panel.popup()
    assert panel._scope.currentData() == ResetScope.DATABASE
    panel._scope.setCurrentIndex(1)
    assert "下次启动重新设置主密码" in panel._description.text()
    panel._password.setText("  master with spaces  ")
    with qtbot.waitSignal(panel.verification_requested) as signal:
        panel._verify.click()
    assert signal.args == ["  master with spaces  ", ResetScope.ALL]
    assert panel._password.text() == ""
    assert not panel._scope.isEnabled()
    assert not panel._verify.isEnabled()
    assert not panel._confirm.isEnabled()
    panel.verification_failed("主密码错误")
    assert panel._scope.isEnabled()
    assert panel._password.isEnabled()
    assert panel._verify.isEnabled()
    panel.close_panel()


def test_countdown_cannot_be_skipped_and_never_auto_confirms(
    host: QWidget, clock: list[float],
) -> None:
    panel = DataResetPanel(host, "test-data")
    panel.popup()
    confirmations: list[bool] = []
    panel.confirmation_requested.connect(lambda: confirmations.append(True))
    panel._request_confirmation()
    assert confirmations == []
    panel.verification_succeeded()
    assert panel._confirm.text() == "请等待 5 秒"
    clock[0] += 4.999
    panel._update_countdown()
    panel._request_confirmation()  # 直接调用处理器也不能绕过禁用按钮。
    assert not panel._confirm.isEnabled()
    assert confirmations == []
    clock[0] += 0.001
    panel._update_countdown()
    assert panel._confirm.isEnabled()
    assert confirmations == []
    panel._confirm.click()
    panel._request_confirmation()
    assert confirmations == [True]
    panel.close_panel()


@pytest.mark.parametrize("cancel", ["button", "escape", "outside"])
def test_cancelling_countdown_never_emits_confirmation(
    host: QWidget, qtbot: QtBot, clock: list[float], cancel: str,
) -> None:
    panel = DataResetPanel(host, "test-data")
    panel.popup()
    confirmations: list[bool] = []
    panel.confirmation_requested.connect(lambda: confirmations.append(True))
    panel.verification_succeeded()
    if cancel == "button":
        panel._cancel.click()
    elif cancel == "escape":
        qtbot.keyClick(panel, Qt.Key.Key_Escape)
    else:
        qtbot.mouseClick(host, Qt.MouseButton.LeftButton)
    clock[0] += 10
    panel._update_countdown()
    panel._request_confirmation()
    assert confirmations == []
    assert not panel._timer.isActive()


@pytest.mark.parametrize("scope", list(ResetScope))
async def test_wired_flow_requires_password_hello_wait_and_final_click(
    host: QWidget, tmp_path: Path, vault_key: VaultKey, clock: list[float],
    monkeypatch: pytest.MonkeyPatch, scope: ResetScope,
) -> None:
    gate = Mock()
    gate.name = "windows-hello"
    gate.available.return_value = True
    gate.verify.return_value = True
    monkeypatch.setattr(reset, "hello_gate", lambda: gate)
    tasks: list[asyncio.Task] = []
    confirmed: list[DataResetService] = []
    panel = _open_data_reset(
        host, tmp_path, lambda coro: tasks.append(asyncio.create_task(coro)), confirmed.append,
    )
    database = tmp_path / "limbowave.db"
    database.write_bytes(b"keep until final confirmation")
    panel._scope.setCurrentIndex(1 if scope is ResetScope.ALL else 0)
    panel._password.setText("wrong")
    panel._verify.click()
    await asyncio.gather(*tasks)
    assert "主密码错误" in panel._status.text()
    gate.verify.assert_not_called()
    panel._password.setText(TEST_PASSWORD)
    panel._verify.click()
    await asyncio.gather(*tasks)
    gate.verify.assert_called_once()
    assert not panel._scope.isEnabled()
    assert panel._deadline == clock[0] + 5
    assert database.exists()
    assert confirmed == []
    panel._request_confirmation()
    assert confirmed == []
    clock[0] += 5
    panel._update_countdown()
    assert confirmed == []
    panel._confirm.click()
    assert len(confirmed) == 1
    assert confirmed[0].scope is scope
    assert database.exists()  # 应用要先关闭资源，再消费授权。
    confirmed[0].execute()
    assert not database.exists()
    assert (tmp_path / "vault.json").exists() is (scope is ResetScope.DATABASE)


async def test_cancelled_panel_ignores_late_hello_success(
    host: QWidget, tmp_path: Path, vault_key: VaultKey, clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    gate = Mock()
    gate.name = "windows-hello"
    gate.available.return_value = True

    def verify(_reason: str) -> bool:
        entered.set()
        assert release.wait(5)
        return True

    gate.verify.side_effect = verify
    monkeypatch.setattr(reset, "hello_gate", lambda: gate)
    tasks: list[asyncio.Task] = []
    confirmed: list[DataResetService] = []
    panel = _open_data_reset(
        host, tmp_path, lambda coro: tasks.append(asyncio.create_task(coro)), confirmed.append,
    )
    panel._password.setText(TEST_PASSWORD)
    panel._verify.click()
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        panel.close_panel()
    finally:
        release.set()
    await asyncio.gather(*tasks)
    assert confirmed == []
    assert panel._deadline is None
    assert (tmp_path / "vault.json").exists()
