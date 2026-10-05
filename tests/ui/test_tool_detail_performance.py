"""Single tool-detail animation has bounded text and layout work."""

from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import QApplication

from limbowave.application.tool_step_payload import prepare_tool_step
from limbowave.domain.tool_step import ToolStep
from limbowave.ui.tool_steps import ToolStepsView
from tests.ui.test_tool_steps_ui import animated_steps as animated_steps


def test_detail_reuses_measurements_across_layout_probe_widths(animated_steps, monkeypatch):
    panel = animated_steps._details["c1"]
    label = panel._detail
    calls = []
    original = label.heightForWidth

    def measure(width):
        calls.append(width)
        return original(width)

    monkeypatch.setattr(label, "heightForWidth", measure)
    for _ in range(8):
        panel._natural_height(420)
        panel._natural_height(560)
    assert len(calls) <= 2


def test_detail_frames_do_not_restart_body_layout_loop(animated_steps, qtbot, monkeypatch):
    view = animated_steps
    panel = view._details["c1"]
    view._chips["c1"].click()
    panel._animation.pause()
    panel._animation.setCurrentTime(panel._animation.duration() // 2)
    qtbot.wait(40)
    calls = []
    layout = view._body.layout()
    original = layout.totalHeightForWidth

    def measure(width):
        calls.append(width)
        return original(width)

    monkeypatch.setattr(layout, "totalHeightForWidth", measure)
    for time in (120, 135, 150, 165, 180):
        panel._animation.setCurrentTime(time)
        QApplication.processEvents()
    assert len(calls) <= 15  # At most a direct update + queued layout/width correction per frame.
    calls.clear()
    for _ in range(5):
        QApplication.processEvents()
    assert len(calls) <= 2  # A stationary paused panel must settle, not invalidate forever.


def test_details_retain_selectable_plain_text_layout(qtbot):
    step = prepare_tool_step(ToolStep("c", "read", args={"path": "<not-html>"}))
    view = ToolStepsView((step,))
    qtbot.addWidget(view)
    label = view._details["c"]._detail
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert "<not-html>" in label.text()


def test_live_text_and_font_changes_refresh_cached_height(animated_steps, qtbot):
    view = animated_steps
    panel = view._details["c1"]
    view._chips["c1"].click()
    panel.settle()
    qtbot.wait(30)
    old_height = panel.height()
    panel._detail.setText("Long wrapped tool arguments and output. " * 120)
    qtbot.waitUntil(lambda: panel.height() > old_height)
    old_height = panel.height()
    panel._detail.setStyleSheet("font-size: 25px; padding: 8px;")
    qtbot.waitUntil(lambda: panel.height() > old_height)
    assert panel._animation.state() == QAbstractAnimation.State.Stopped
    assert panel.height() == panel._detail.heightForWidth(panel.width())


def test_collapsed_detail_does_not_measure_unseen_text(animated_steps, monkeypatch):
    panel = animated_steps._details["c1"]

    def forbidden(*args):
        raise AssertionError("hidden detail performed text measurement")

    monkeypatch.setattr(panel._detail, "heightForWidth", forbidden)
    monkeypatch.setattr(panel._detail, "sizeHint", forbidden)
    assert panel.sizeHint().height() == 0
    assert panel.heightForWidth(300) == 0
    assert panel.heightForWidth(600) == 0


def test_changed_tool_result_invalidates_metrics_during_animation(animated_steps, qtbot):
    from limbowave.domain.tool_step import finish

    view = animated_steps
    chip = view._chips["c1"]
    panel = view._details["c1"]
    chip.click()
    panel._animation.pause()
    panel._animation.setCurrentTime(panel._animation.duration() // 2)
    qtbot.wait(30)
    previous = panel.height()
    changed = prepare_tool_step(finish(
        ToolStep("c1", "read", args={"path": "long arguments " * 300}),
        result="latest result", duration_ms=15,
    ))
    view.update_steps((changed, view._chips["c2"]._step))
    qtbot.waitUntil(lambda: panel.height() > previous)
    assert panel._progress == 0.5
    assert chip._expanded
    assert "latest result" in panel._detail.text()
    assert panel.height() == round(panel._detail.heightForWidth(panel.width()) / 2)
    assert panel._animation.state() == QAbstractAnimation.State.Paused
