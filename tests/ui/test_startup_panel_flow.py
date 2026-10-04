"""启动同步路径的悬浮窗交互（「未响应」回归测试）。

背景：启动解锁发生在主 Qt 循环**之前**。上一版在 ``_ask_password`` 里开了
asyncio 局部循环等回调——asyncio 不处理 Qt 事件，悬浮窗永远收不到输入，
窗口「未响应」，用户一关就「无报错退出」。这里的测试证明现在的实现
（嵌套 Qt 事件循环等回调）在无主循环的语境下**真的可交互**。

技巧：pytest 主线程在 ``_ask_password`` 里阻塞时没法再去点按钮，
所以用 ``QTimer.singleShot`` 把交互动作**排进嵌套循环里**执行——
它若真的能在嵌套循环里跑起来并驱动面板，就证明修对了。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLineEdit, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from limbowave.app import (
    _ask_password,
    _ask_startup_confirm,
    _offer_system_recovery,
    _show_startup_alert,
)
from limbowave.ui.floating import FloatingPanel
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow


def _visible_panel(host: QWidget) -> FloatingPanel:
    panels = [p for p in host.findChildren(FloatingPanel) if p.isVisible() and not p._closing]
    assert panels, "应有一个可见的悬浮面板"
    return panels[0]


def _click(panel: FloatingPanel, text: str) -> None:
    buttons = [b for b in panel.findChildren(QPushButton) if b.text() == text]
    assert buttons, f"应存在按钮 {text!r}"
    buttons[0].click()


def test_ask_password_submit_roundtrip(qtbot: QtBot) -> None:
    host = QWidget()
    host.resize(800, 600)
    qtbot.addWidget(host)
    host.show()

    def _interact() -> None:
        panel = _visible_panel(host)
        edit = panel.findChildren(QLineEdit)[0]
        edit.setText("主密码-123")
        _click(panel, "确定")

    QTimer.singleShot(80, _interact)
    assert _ask_password(host, "解锁资料库", "主密码：") == "主密码-123"


def test_ask_password_cancel_returns_none(qtbot: QtBot) -> None:
    host = QWidget()
    host.resize(800, 600)
    qtbot.addWidget(host)
    host.show()

    def _cancel() -> None:
        _click(_visible_panel(host), "取消")

    QTimer.singleShot(80, _cancel)
    assert _ask_password(host, "解锁资料库", "主密码：") is None


def test_ask_password_dismiss_returns_none(qtbot: QtBot) -> None:
    host = QWidget()
    host.resize(800, 600)
    qtbot.addWidget(host)
    host.show()

    def _dismiss() -> None:
        _visible_panel(host).close_panel()

    QTimer.singleShot(80, _dismiss)
    assert _ask_password(host, "解锁资料库", "主密码：") is None


def test_startup_confirm_yes_and_no(qtbot: QtBot) -> None:
    host = QWidget()
    host.resize(800, 600)
    qtbot.addWidget(host)
    host.show()

    def _yes() -> None:
        _click(_visible_panel(host), "重置")

    QTimer.singleShot(80, _yes)
    assert _ask_startup_confirm(host, "忘记主密码？", "现在重置？", confirm_text="重置") is True

    def _no() -> None:
        _click(_visible_panel(host), "取消")

    QTimer.singleShot(80, _no)
    assert _ask_startup_confirm(host, "忘记主密码？", "现在重置？", confirm_text="重置") is False


def test_startup_alert_waits_for_dismiss(qtbot: QtBot) -> None:
    """提示框必须**等用户看完**才继续——启动路径的提示一闪而过等于没提示。"""
    host = QWidget()
    host.resize(800, 600)
    qtbot.addWidget(host)
    host.show()

    seen: list[bool] = []

    def _dismiss() -> None:
        panel = _visible_panel(host)
        seen.append(panel.isVisible())
        panel.close_panel()

    QTimer.singleShot(80, _dismiss)
    _show_startup_alert(host, "已重置", "主密码已重置，资料库内容未变。")
    assert seen == [True], "关闭前面板应可见（即确实等待过用户）"


def test_system_recovery_hands_focus_back_and_keeps_ui_responsive(qtbot: QtBot) -> None:
    """退场面板不再拦截密码输入；Hello 等待期间 Qt 事件仍继续处理。"""

    expected_key = object()

    class _SlowRecoveryVault:
        def recovery_enabled(self) -> bool:
            return True

        def unlock_with_recovery(self, password: str) -> object:
            assert password == "new-password"
            time.sleep(0.25)
            return expected_key

    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()
    page._typing.stop()
    page._name_intro.stop()
    page._ready = True
    page._form.setEnabled(True)
    page._form.settle()

    stage = {"reset": False, "new": False, "confirm": False, "dismissed": False}
    focus_handoff: list[bool] = []
    responsive_ticks: list[bool] = []
    saw_busy: list[bool] = []

    driver = QTimer(window)
    driver.setInterval(10)

    def _drive() -> None:
        panels = [
            panel
            for panel in window.findChildren(FloatingPanel)
            if panel.isVisible() and not panel._closing
        ]
        if panels and not stage["reset"]:
            buttons = [
                button
                for button in panels[0].findChildren(QPushButton)
                if button.text() == "重置"
            ]
            if buttons:
                stage["reset"] = True
                buttons[0].click()
                return

        prompt = page._input.placeholderText()
        input_ready = not page.loading and page._input.isEnabled()
        if prompt == "设置新主密码" and not stage["new"] and input_ready:
            focus_handoff.append(
                not any(
                    panel.isVisible() and not panel._closing
                    for panel in window.findChildren(FloatingPanel)
                )
            )
            stage["new"] = True
            page._input.setText("new-password")
            page._input.returnPressed.emit()
            return
        if prompt == "确认新主密码" and not stage["confirm"] and input_ready:
            stage["confirm"] = True
            page._input.setText("new-password")
            page._input.returnPressed.emit()
            return
        if prompt == "正在验证系统身份…":
            saw_busy.append(not page._input.isEnabled())
            responsive_ticks.append(True)

        if panels and stage["confirm"] and not stage["dismissed"]:
            ok = [
                button
                for button in panels[0].findChildren(QPushButton)
                if button.text() == "好"
            ]
            if ok:
                stage["dismissed"] = True
                ok[0].click()

    driver.timeout.connect(_drive)
    driver.start()
    # A broken handoff should fail assertions rather than hang the entire UI suite.
    watchdog = QTimer(window)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(window.close)
    watchdog.start(8000)
    try:
        result = _offer_system_recovery(_SlowRecoveryVault(), window)  # type: ignore[arg-type]
    finally:
        driver.stop()
        watchdog.stop()

    assert result is expected_key
    assert focus_handoff == [True]
    assert saw_busy and all(saw_busy)
    assert len(responsive_ticks) >= 3
