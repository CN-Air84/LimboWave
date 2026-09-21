from __future__ import annotations

from PySide6.QtCore import SignalInstance
from pytestqt.qtbot import QtBot

from limbowave.ui.main_window import MainWindow


def test_window_title_identifies_app(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert "LimboWave" in window.windowTitle()


def test_window_holds_no_public_state(qtbot: QtBot) -> None:
    """架构守卫：主窗口的公开属性只允许是信号，不得挂载业务数据。

    窗口只表达意图、展示状态；一旦有人往窗口上挂会话或消息集合，这里会立刻失败。
    """
    window = MainWindow()
    qtbot.addWidget(window)

    data_attributes = [
        name
        for name, value in vars(window).items()
        if not name.startswith("_") and not isinstance(value, SignalInstance)
    ]

    assert data_attributes == []


def test_window_has_central_widget(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.centralWidget() is not None


def test_window_exposes_command_channel(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    with qtbot.waitSignal(window.command_requested, timeout=1000) as blocker:
        window.command_requested.emit("ping")

    assert blocker.args == ["ping"]
