"""启动登录页的布局、逐字动画与同步解锁入口。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QLineEdit
from pytestqt.qtbot import QtBot

from limbowave.app import _ask_password
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow


def test_login_page_scales_headlines_with_window(qtbot: QtBot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(1280, 800)
    page.show()
    assert page._hello.font().pixelSize() == round(page._name.font().pixelSize() * 0.8)
    large = page._name.font().pixelSize()
    page.resize(640, 480)
    assert page._name.font().pixelSize() < large
    assert page._content.width() <= page.width() - 48


def test_login_page_types_then_reveals_password(qtbot: QtBot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    assert not page._form.isEnabled()
    assert page._form._get_reveal_progress() == 0
    assert page._input.reveal_progress == 0
    assert page._name._get_reveal_progress() == 0
    hello_origin = page._hello.pos()
    name_slot_origin = page._name_slot.pos()
    assert page._hello.fontScale == 1.0
    assert page._hello_shrink.duration() == page._name_reveal.duration()
    qtbot.waitUntil(lambda: page._name_intro.state().name == "Running", timeout=2000)
    qtbot.waitUntil(lambda: 0 < page._name._get_reveal_progress() < 1, timeout=1000)
    assert page._name.pos().y() == 0
    assert 0.65 / 0.8 < page._hello.fontScale < 1.0
    assert page._hello.pos() == hello_origin
    assert page._name_slot.pos() == name_slot_origin
    assert not page._form.isEnabled()
    qtbot.waitUntil(lambda: page._ready, timeout=3000)
    assert page._hello.text() == "Hello There"
    assert page._name.text() == "LimboWave"
    assert page._name._get_reveal_progress() == 1
    assert page._name.pos().y() == 0
    assert abs(page._hello.fontScale - 0.65 / 0.8) < 0.001
    assert page._hello.pos() == hello_origin
    assert page._name_slot.pos() == name_slot_origin
    qtbot.waitUntil(lambda: 0 < page._form._get_reveal_progress() < 1, timeout=1500)
    assert page._input.y() > 0  # 仍在从下方升起
    assert 0 < page._input.reveal_progress < 1
    qtbot.waitUntil(lambda: page._form._get_reveal_progress() == 1, timeout=1500)
    assert page._input.y() == 0
    assert page._input.reveal_progress == 1
    assert page._form.isEnabled()
    page.set_prompt("确认主密码", "再输入一次：")
    assert page._input.placeholderText() == "确认主密码"
    assert page._ready
    enter = page._enter
    assert enter.text() == "➜"
    assert enter.parentWidget() is page._input
    assert enter.width() == enter.height() == 36
    assert enter.geometry().right() < page._input.width()
    assert enter.x() > page._input.width() // 2
    assert enter.y() + enter.height() // 2 == page._input.height() // 2
    assert enter.font().family() == "Segoe UI Symbol"
    content = page._input._content_rect()
    assert content.right() < enter.x()


def test_form_reveal_resumes_after_event_loop_stall(qtbot: QtBot) -> None:
    import time

    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    page._typing.stop()
    page._reveal_form()
    qtbot.waitUntil(lambda: page._form._get_reveal_progress() > 0, timeout=500)
    before = page._form._get_reveal_progress()
    time.sleep(0.4)  # 模拟启动期 GUI 线程被主题/背景安装占住
    qtbot.waitUntil(lambda: page._form._get_reveal_progress() > before, timeout=500)
    # 停顿后从原处继续播放，而不是按墙钟时间直接跳到终点。
    assert page._form._get_reveal_progress() < 0.8
    qtbot.waitUntil(lambda: page._form._get_reveal_progress() == 1, timeout=1500)
    assert page._input.y() == 0
    qtbot.waitUntil(page._input.hasFocus, timeout=500)



def test_password_dots_scale_around_fixed_centers(qtbot: QtBot) -> None:
    from PySide6.QtCore import QEasingCurve

    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    page._form.setEnabled(True)
    edit = page._input
    edit.setFocus()

    qtbot.keyClicks(edit, "a")
    assert len(edit._dots) == 1
    dot = edit._dots[0]
    center = edit._dot_centers()[0]
    assert dot._animation.easingCurve().type() == QEasingCurve.Type.OutBack
    qtbot.waitUntil(lambda: 0.05 < dot._get_scale() < 1.2, timeout=500)
    assert edit._dot_centers()[0] == center
    qtbot.waitUntil(lambda: abs(dot._get_scale() - 1.0) < 0.001, timeout=800)
    assert edit._dot_centers()[0] == center

    qtbot.keyClick(edit, Qt.Key.Key_Backspace)
    assert edit._dots == []
    assert len(edit._exiting) == 1
    leaving, exit_center = edit._exiting[0]
    assert leaving is dot
    assert exit_center == center
    assert leaving._animation.easingCurve().type() == QEasingCurve.Type.InCubic
    qtbot.waitUntil(lambda: 0.0 < leaving._get_scale() < 1.0, timeout=500)
    assert edit._exiting[0][1] == center
    qtbot.waitUntil(lambda: edit._exiting == [], timeout=800)


def test_password_dot_centers_do_not_depend_on_scale(qtbot: QtBot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    page._form.setEnabled(True)
    edit = page._input
    edit.setText("abc")
    centers = edit._dot_centers()

    for dot, scale in zip(edit._dots, (0.2, 0.7, 1.15), strict=True):
        dot._set_scale(scale)

    assert edit._dot_centers() == centers
    assert [center.y() for center in centers] == [centers[0].y()] * 3



def test_placeholder_waits_for_password_dots_to_exit(qtbot: QtBot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    page._form.setEnabled(True)
    edit = page._input
    edit.setPlaceholderText("解锁资料库")
    edit.setText("secret")
    qtbot.waitUntil(lambda: all(dot._get_scale() > 0.9 for dot in edit._dots), timeout=700)

    edit.clear()
    assert edit._exiting
    assert not edit._should_draw_placeholder()
    qtbot.waitUntil(lambda: edit._exiting == [], timeout=700)
    assert edit._should_draw_placeholder()

def test_password_caret_is_centered_between_dots(qtbot: QtBot) -> None:
    from PySide6.QtGui import QPalette

    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    page._form.setEnabled(True)
    edit = page._input
    edit.setText("abcdefghij")
    centers = edit._dot_centers()

    assert edit.palette().color(QPalette.ColorRole.Text).alpha() == 0
    for cursor in range(1, len(centers)):
        edit.setCursorPosition(cursor)
        expected = (centers[cursor - 1].x() + centers[cursor].x()) / 2
        assert abs(edit._target_caret_x() - expected) < 0.001
    edit.setCursorPosition(len(centers))
    end_x = centers[-1].x() + edit._DOT_STEP / 2
    assert abs(edit._target_caret_x() - end_x) < 0.001
    qtbot.waitUntil(lambda: abs(edit._get_caret_x() - end_x) < 0.001, timeout=500)

    edit.setCursorPosition(2)
    target_x = (centers[1].x() + centers[2].x()) / 2
    assert edit._caret_move.state().name == "Running"
    qtbot.waitUntil(
        lambda: min(target_x, end_x) < edit._get_caret_x() < max(target_x, end_x),
        timeout=300,
    )
    qtbot.waitUntil(lambda: abs(edit._get_caret_x() - target_x) < 0.001, timeout=500)


def test_wrong_password_shakes_input_and_marks_inner_border(qtbot: QtBot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    page._form.setEnabled(True)
    edit = page._input
    origin = edit.pos()

    page.show_password_error()
    assert edit._shake.state().name == "Running"
    qtbot.waitUntil(lambda: edit.pos() != origin, timeout=500)
    qtbot.waitUntil(lambda: edit._get_error_strength() > 0.5, timeout=500)
    qtbot.waitUntil(lambda: edit._shake.state().name == "Stopped", timeout=1000)
    assert edit.pos() == origin
    assert abs(edit._get_error_strength() - 1.0) < 0.001

    edit.setFocus()
    qtbot.keyClicks(edit, "x")
    qtbot.waitUntil(lambda: edit._get_error_strength() < 0.5, timeout=500)
    qtbot.waitUntil(lambda: edit._get_error_strength() == 0.0, timeout=700)

def test_startup_password_uses_login_page(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()

    def _submit() -> None:
        if not page._ready:
            QTimer.singleShot(25, _submit)
            return
        assert page.isVisibleTo(window)
        assert not window.chat.isVisibleTo(window)
        page._input.setText("安全密码")
        page._enter.click()

    QTimer.singleShot(25, _submit)
    assert _ask_password(window, "解锁资料库", "主密码：") == "安全密码"
    assert page._input.text() == ""
    assert page._input.echoMode() == QLineEdit.EchoMode.Password


def test_startup_login_escape_cancels(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()
    QTimer.singleShot(1800, lambda: qtbot.keyClick(page._input, Qt.Key.Key_Escape))
    assert _ask_password(window, "解锁资料库", "主密码：") is None


def test_login_exit_waits_for_logo_instead_of_sliding(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()
    qtbot.waitExposed(window)

    window.show_workspace(scroll_down=True)
    assert window.showing_full_page
    assert window.transition_active
    assert window._transition is None
    assert window._transition_animation is None
    assert page._logo._phase == "drawing"
    qtbot.waitUntil(lambda: page._logo._phase == "erasing", timeout=3000)
    assert window.showing_full_page
    assert not window.sidebar.isVisibleTo(window)
    qtbot.waitUntil(lambda: not window.showing_full_page, timeout=1500)
    assert window.transition_active
    assert window._login_fade is not None
    qtbot.waitUntil(lambda: not window.transition_active, timeout=1000)
    assert window._login_fade is None
    assert window._transition is None
    assert window.sidebar.isVisibleTo(window)
    assert window._workspace.pos() == QPoint(0, 0)


def test_login_scroll_direction_and_fades(qtbot: QtBot) -> None:
    from PySide6.QtGui import QColor, QPalette, QPixmap
    from PySide6.QtWidgets import QWidget

    from limbowave.ui.main_window import _PageTransition

    outgoing = QPixmap(100, 100)
    outgoing.fill(QColor("#ff0000"))
    parent = QWidget()
    parent.resize(100, 100)
    qtbot.addWidget(parent)
    incoming = QWidget(parent)
    incoming.resize(100, 100)
    incoming.setAutoFillBackground(True)
    palette = incoming.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#00ff00"))
    incoming.setPalette(palette)
    incoming.show()
    transition = _PageTransition(parent, outgoing, incoming, -1, scroll_down=True)
    transition.resize(100, 100)
    transition.show()
    transition._set_progress(0.5)
    frame = parent.grab().toImage()
    left = frame.pixelColor(4, 50)
    right = frame.pixelColor(95, 50)
    assert left.green() > left.red()  # 登录层右移后，左侧露出静止主页
    assert abs(right.red() - right.green()) <= 2  # 固定主页在旧页下方透出
    assert incoming.pos() == QPoint(0, 0)


def test_login_elements_move_right_together_during_exit(qtbot: QtBot) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap
    from PySide6.QtWidgets import QWidget

    from limbowave.ui.main_window import _PageTransition

    outgoing = QPixmap(100, 100)
    outgoing.fill(QColor("#000000"))
    painter = QPainter(outgoing)
    painter.fillRect(QRect(30, 60, 40, 6), QColor("#ff0000"))
    painter.end()
    parent = QWidget()
    parent.resize(100, 100)
    qtbot.addWidget(parent)
    incoming = QWidget(parent)
    incoming.resize(100, 100)
    incoming.setAutoFillBackground(True)
    palette = incoming.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#00ff00"))
    incoming.setPalette(palette)
    incoming.show()
    transition = _PageTransition(parent, outgoing, incoming, -1, scroll_down=True)
    transition.resize(100, 100)
    transition.show()
    transition._set_progress(0.5)

    frame = parent.grab().toImage()
    ratio = frame.devicePixelRatio()
    # 42 px travel * 50% progress: the marker moves from x=30..70 to about x=51..91.
    moved = frame.pixelColor(round(85 * ratio), round(62 * ratio))
    old_position = frame.pixelColor(round(35 * ratio), round(62 * ratio))
    assert moved.red() > old_position.red() + 40
    assert old_position.green() > old_position.red()


def test_login_snapshot_keeps_second_heading_at_live_coordinates(qtbot: QtBot) -> None:
    from PySide6.QtCore import QPoint

    from limbowave.ui.main_window import _page_snapshot

    page = LoginPage()
    qtbot.addWidget(page)
    page.resize(900, 650)
    page.show()
    page._typing.stop()
    page._name.move(0, 0)
    page._name.setStyleSheet("background: #ff0000; color: #ff0000;")
    live_origin = page._name.mapTo(page, QPoint(0, 0))

    page._input.setText("secret")
    assert page._input._dots
    page.prepare_exit_snapshot()
    assert page._input._dots == []
    assert page._input._exiting == []
    frame = _page_snapshot(page)
    image = frame.toImage()
    ratio = frame.devicePixelRatio()
    captured = image.pixelColor(
        round((live_origin.x() + 4) * ratio),
        round((live_origin.y() + 4) * ratio),
    )
    bottom_middle = image.pixelColor(image.width() // 2, image.height() - 12)
    assert captured.red() > 200 and captured.green() < 80
    assert bottom_middle.red() < 200



def test_wrong_vault_password_uses_inline_error_not_floating_alert(
    qtbot: QtBot, tmp_path, monkeypatch
) -> None:
    from limbowave import app as app_module
    from limbowave.bootstrap import AppPaths
    from limbowave.infrastructure.crypto.vault import KdfParams, Vault

    data_root = tmp_path / "data"
    data_root.mkdir()
    Vault(
        data_root / "vault.json",
        params=KdfParams(time_cost=1, memory_cost=8, parallelism=1),
    ).create("correct password")
    paths = AppPaths(data_root=data_root, log_root=tmp_path / "logs")
    window = MainWindow()
    qtbot.addWidget(window)
    page = LoginPage()
    window.show_full_page(page)
    window.show()

    answers = iter(("wrong password", "correct password"))
    inline_errors: list[bool] = []
    monkeypatch.setattr(app_module, "_ask_password", lambda *_args: next(answers))
    monkeypatch.setattr(page, "show_password_error", lambda: inline_errors.append(True))

    def _unexpected_alert(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("错误密码不应弹出悬浮提示")

    monkeypatch.setattr(app_module, "_show_startup_alert", _unexpected_alert)
    assert app_module._unlock_vault(paths, window) is not None
    assert inline_errors == [True]
