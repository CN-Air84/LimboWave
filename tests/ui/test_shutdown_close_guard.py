from limbowave.ui.main_window import MainWindow


def test_close_guard_defers_close_until_confirmed(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.set_close_guard(lambda: False)
    assert not window.close()
    assert window.isVisible()
    window.set_close_guard(lambda: True)
    assert window.close()
    assert not window.isVisible()
