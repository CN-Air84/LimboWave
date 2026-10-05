"""运行状态条的水波绘制和可见性生命周期。"""

from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from limbowave.ui.run_state_label import RunStateLabel


def test_wave_only_runs_while_visible(qtbot: QtBot, monkeypatch) -> None:
    monkeypatch.setattr(QApplication, "isEffectEnabled", staticmethod(lambda _: True))
    parent = QWidget()
    qtbot.addWidget(parent)
    label = RunStateLabel(parent)
    label.setText("● 正在生成内容…")
    assert not label._timer.isActive()
    parent.show()
    qtbot.waitUntil(label._timer.isActive)
    phase = label._phase
    qtbot.waitUntil(lambda: label._phase != phase)
    parent.hide()
    assert not label._timer.isActive()
    parent.show()
    qtbot.waitUntil(label._timer.isActive)
    label.hide()
    assert not label._timer.isActive()


def test_wave_changes_background_without_changing_layout(qtbot: QtBot) -> None:
    label = RunStateLabel()
    qtbot.addWidget(label)
    label.setText("● 正在生成内容…")
    label.resize(760, 36)
    label.show()
    label._timer.stop()
    hint = label.sizeHint()
    label._phase = 0.0
    first = label.grab().toImage()
    label._phase = 1.5
    second = label.grab().toImage()
    assert first != second
    assert label.sizeHint() == hint
    assert label.text() == "● 正在生成内容…"
    label.setText("↻ 正在重试，请稍候；" * 12)
    label.resize(240, 100)
    assert label.wordWrap()
    assert not label.grab().isNull()
