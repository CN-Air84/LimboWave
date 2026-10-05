from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from limbowave.ui.session_toolbar import THINKING_LEVELS, SessionToolbar
from limbowave.ui.thinking_combo import HOLD_MS


def popup(qtbot):
    bar = SessionToolbar()
    qtbot.addWidget(bar)
    bar.set_session_available(True)
    bar.set_thinking_capability(False, runtime_levels=("off",))
    bar.show()
    combo = bar._thinking
    combo.showPopup()
    qtbot.wait(180)
    index = combo.model().index(THINKING_LEVELS.index("high"), 0)
    pos = combo.view().visualRect(index).center()
    return bar, combo, pos


def test_three_second_hold_requests_trial_without_selecting_or_saving(qtbot):
    bar, combo, pos = popup(qtbot)
    fired, selected = [], []
    bar.thinking_force_requested.connect(fired.append)
    bar.thinking_level_changed.connect(selected.append)
    QTest.mousePress(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.wait(HOLD_MS - 350)
    assert not fired
    qtbot.waitUntil(lambda: fired == ["high"], timeout=1000)
    assert selected == []
    assert combo.currentText() == "off"  # app must first apply the runtime override
    assert not combo.model().item(4).isEnabled()
    assert not combo.view().isVisible()


def test_short_click_does_not_unlock(qtbot):
    bar, combo, pos = popup(qtbot)
    fired = []
    bar.thinking_force_requested.connect(fired.append)
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert not combo._hold_timer.isActive()
    assert fired == []
    assert combo.currentText() == "off"
    combo.hidePopup()


def test_moving_off_row_and_model_refresh_cancel_hold(qtbot):
    bar, combo, pos = popup(qtbot)
    QTest.mousePress(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert combo._hold_timer.isActive()
    other = combo.view().visualRect(combo.model().index(1, 0)).center()
    QTest.mouseMove(combo.view().viewport(), other)
    assert not combo._hold_timer.isActive()
    QTest.mouseRelease(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=other)
    QTest.mousePress(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
    bar.set_thinking_capability(True, runtime_levels=("off", "low"))
    assert not combo._hold_timer.isActive()
    combo.hidePopup()


def test_close_and_disable_cancel_hold(qtbot):
    bar, combo, pos = popup(qtbot)
    QTest.mousePress(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
    combo.hidePopup()
    assert not combo._hold_timer.isActive()
    combo.showPopup()
    qtbot.wait(180)
    QTest.mousePress(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
    bar.set_session_available(False)
    assert not combo._hold_timer.isActive()
