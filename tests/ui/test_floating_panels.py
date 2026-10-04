"""悬浮窗组件（ui/floating.py）与导出悬浮窗的行为测试。

这批测试盯的是「替代系统弹窗」后必须仍然成立的行为契约：

- FloatingPanel：弹出即显示、Esc 关闭、点外部关闭、关闭只发一次 ``closed``；
- ask_confirm：确定答 True、✕/Esc/点外部答 False，**只回调一次**（去重）；
- ask_choice：点哪个答哪个键、关闭答 None、只回调一次、正文按纯文本显示；
- ask_prompt：空值不回调、回车提交、密码不回显；
- ExportPanel：时间筛掉的老会话行被隐藏、全选/全不选作用于可见行、
  格式与目录选择进入导出回调、没有勾选时不触发回调。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import (
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QWidget,
)
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from limbowave.ui.export_panel import ExportPanel
from limbowave.ui.floating import FloatingPanel, ask_alert, ask_choice, ask_confirm, ask_prompt


@pytest.fixture(autouse=True)
def _modal_stub(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录选择器打桩：测试不能弹系统目录框。"""
    monkeypatch.setattr(
        "limbowave.ui.export_panel.QFileDialog.getExistingDirectory",
        staticmethod(lambda *a, **k: "/tmp/limbowave-test-export"),
    )


def _hold(parent_widget: object) -> object:
    """给面板一个可依附的父窗口（parentWidget 非空才有定位逻辑）。"""
    return parent_widget


def _host(qtbot: QtBot) -> QWidget:
    """可依附的父窗口：必须 show 出来，子面板的 isVisible 才是真的。"""
    host = QWidget()
    host.resize(1000, 800)
    qtbot.addWidget(host)
    host.show()
    return host


# ---------- FloatingPanel 基础行为 ----------


def test_panel_popup_shows_and_close_emits_once(qtbot: QtBot) -> None:

    host = _host(qtbot)
    panel = FloatingPanel(host, "标题")
    panel.resize(300, 200)
    with qtbot.waitSignal(panel.closed, timeout=1000):
        panel.popup()
        assert panel.isVisible()
        panel.close_panel()
    qtbot.waitUntil(lambda: not isValid(panel) or not panel.isVisible(), timeout=1000)


def test_panel_fades_and_slides_in_and_out(qtbot: QtBot) -> None:
    host = _host(qtbot)
    panel = FloatingPanel(host, "动效")
    closed: list[bool] = []
    panel.closed.connect(lambda: closed.append(True))

    panel.popup()
    entering_y = panel.pos().y()
    assert panel._opacity.isEnabled()
    assert panel._motion is not None
    qtbot.waitUntil(lambda: panel._motion is None, timeout=1000)
    assert panel._opacity.opacity() == 1.0
    assert not panel._opacity.isEnabled()
    settled = panel.pos()
    assert entering_y == settled.y() + 10

    panel.close_panel()
    panel.close_panel()
    assert closed == [True]  # 结果不等动画完成，也不会重复送达
    assert panel.isVisible()  # 淡出期间保留卡片
    assert panel._motion is not None
    # Capture geometry before deleteLater; polling may resume after C++ destruction.
    final_y: list[int] = []
    panel._motion.finished.connect(lambda: final_y.append(panel.pos().y()))
    qtbot.waitUntil(lambda: bool(final_y), timeout=1000)
    assert final_y == [settled.y() + 8]


def test_panel_close_during_entrance_reverses_without_duplicate_signal(qtbot: QtBot) -> None:
    host = _host(qtbot)
    panel = FloatingPanel(host, "快速关闭")
    closed: list[bool] = []
    panel.closed.connect(lambda: closed.append(True))
    panel.popup()
    panel.close_panel()
    panel.close_panel()
    assert closed == [True]
    assert panel._motion is not None
    qtbot.waitUntil(lambda: not isValid(panel) or not panel.isVisible(), timeout=1000)


def test_panel_close_is_idempotent(qtbot: QtBot) -> None:

    host = QWidget()
    qtbot.addWidget(host)
    panel = FloatingPanel(host, "标题")
    panel.popup()
    panel.close_panel()
    panel.close_panel()  # 第二次不应崩、不应再发信号


def test_panel_esc_closes(qtbot: QtBot) -> None:
    from PySide6.QtCore import Qt

    host = QWidget()
    qtbot.addWidget(host)
    panel = FloatingPanel(host, "标题")
    panel.popup()
    qtbot.keyClick(panel, Qt.Key.Key_Escape)
    assert not panel.isVisible()


# ---------- ask_confirm ----------


def test_confirm_ok_answers_true_once(qtbot: QtBot) -> None:

    host = _host(qtbot)
    answers: list[bool] = []
    panel = ask_confirm(host, "确认", "内容？", answers.append)
    # 点「确定」
    buttons = [b for b in panel.findChildren(type(panel)) if b.text() == "确定"]
    assert not buttons  # 按钮不是 FloatingPanel 类型——用文本找 QWidget 兄弟
    from PySide6.QtWidgets import QPushButton

    oks = [b for b in panel.findChildren(QPushButton) if b.text() == "确定"]
    assert len(oks) == 1
    oks[0].click()
    assert answers == [True]


def test_confirm_dismiss_answers_false_once(qtbot: QtBot) -> None:

    host = _host(qtbot)
    answers: list[bool] = []
    panel = ask_confirm(host, "确认", "内容？", answers.append)
    # 直接关闭（✕ 按钮）
    closes = [b for b in panel.findChildren(QPushButton) if b.text() == "✕"]
    assert len(closes) == 1
    closes[0].click()
    assert answers == [False]
    panel.close_panel()
    assert answers == [False], "关闭后不得再回调"


def test_confirm_cancel_answers_false(qtbot: QtBot) -> None:

    host = _host(qtbot)
    answers: list[bool] = []
    panel = ask_confirm(host, "确认", "内容？", answers.append, cancel_text="不")
    cancels = [b for b in panel.findChildren(QPushButton) if b.text() == "不"]
    assert len(cancels) == 1
    cancels[0].click()
    assert answers == [False]


# ---------- ask_choice ----------


def _choice_panel(host: QWidget, answers: list[str | None]) -> FloatingPanel:
    return ask_choice(
        host,
        "选择",
        "内容？",
        [("a", "甲"), ("b", "乙"), ("c", "丙")],
        answers.append,
        accent="b",
    )


def test_choice_click_answers_its_key_once(qtbot: QtBot) -> None:
    host = _host(qtbot)
    answers: list[str | None] = []
    panel = _choice_panel(host, answers)
    buttons = {b.text(): b for b in panel.findChildren(QPushButton) if b.text() != "✕"}
    assert list(buttons) == ["甲", "乙", "丙"]  # 按给定顺序从左到右
    assert buttons["乙"].property("accent") is True
    assert not buttons["甲"].property("accent")
    buttons["丙"].click()
    assert answers == ["c"]
    panel.close_panel()
    assert answers == ["c"], "关闭后不得再回调"


def test_choice_dismiss_answers_none(qtbot: QtBot) -> None:
    from PySide6.QtCore import Qt

    host = _host(qtbot)
    answers: list[str | None] = []
    panel = _choice_panel(host, answers)
    qtbot.keyClick(panel, Qt.Key.Key_Escape)
    assert answers == [None]


def test_choice_detail_is_plain_text(qtbot: QtBot) -> None:
    """正文可能含外部内容（工具参数）：不能被当成富文本解析。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    host = _host(qtbot)
    panel = ask_choice(host, "选择", "<b>粗体</b>", [("a", "甲")], lambda _k: None)
    labels = [lb for lb in panel.findChildren(QLabel) if lb.text() == "<b>粗体</b>"]
    assert len(labels) == 1
    assert labels[0].textFormat() == Qt.TextFormat.PlainText


# ---------- ask_prompt ----------


def test_prompt_submit_via_return(qtbot: QtBot) -> None:

    host = _host(qtbot)
    got: list[str] = []
    panel = ask_prompt(host, "输入", "值：", got.append)
    edits = panel.findChildren(QLineEdit)
    assert len(edits) == 1
    qtbot.keyClicks(edits[0], "hello")
    qtbot.keyClick(edits[0], __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Key.Key_Return)
    assert got == ["hello"]


def test_prompt_empty_does_not_submit(qtbot: QtBot) -> None:

    host = _host(qtbot)
    got: list[str] = []
    panel = ask_prompt(host, "输入", "值：", got.append)
    edits = panel.findChildren(QLineEdit)
    edits[0].setText("   ")
    subs = [b for b in panel.findChildren(QPushButton) if b.text() == "确定"]
    subs[0].click()
    assert got == [], "空白输入不应提交"


def test_prompt_password_does_not_echo(qtbot: QtBot) -> None:

    host = _host(qtbot)
    panel = ask_prompt(host, "密码", "值：", lambda _v: None, password=True)
    edit = panel.findChildren(QLineEdit)[0]
    assert edit.echoMode() == QLineEdit.EchoMode.Password


def test_animated_password_prompt_reuses_login_dot_motion(qtbot: QtBot) -> None:
    from PySide6.QtCore import QEasingCurve, Qt

    from limbowave.ui.login_page import _AnimatedPasswordEdit

    host = _host(qtbot)
    submitted: list[str] = []
    panel = ask_prompt(
        host, "验证", "主密码：", submitted.append, password=True, animated_password=True,
    )
    edit = panel.findChild(_AnimatedPasswordEdit)
    assert edit is not None
    assert edit.echoMode() == QLineEdit.EchoMode.Password

    qtbot.keyClicks(edit, "abc")
    assert edit.text() == "abc"
    assert len(edit._dots) == 3
    assert edit._dots[0]._animation.easingCurve().type() == QEasingCurve.Type.OutBack
    qtbot.keyClick(edit, Qt.Key.Key_Backspace)
    assert len(edit._exiting) == 1
    assert edit._exiting[0][0]._animation.easingCurve().type() == QEasingCurve.Type.InCubic
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert submitted == ["ab"]


def test_prompt_multiline_uses_plain_edit(qtbot: QtBot) -> None:

    host = _host(qtbot)
    got: list[str] = []
    panel = ask_prompt(host, "编辑", "内容：", got.append, default_text="原文", multiline=True)
    edits = panel.findChildren(QPlainTextEdit)
    assert len(edits) == 1
    assert edits[0].toPlainText() == "原文"


def test_alert_shows_ok(qtbot: QtBot) -> None:

    host = _host(qtbot)
    panel = ask_alert(host, "提示", "内容")
    oks = [b for b in panel.findChildren(QPushButton) if b.text() == "好"]
    assert len(oks) == 1
    oks[0].click()
    qtbot.waitUntil(lambda: not isValid(panel) or not panel.isVisible(), timeout=1000)


# ---------- ExportPanel ----------


def _panel_rows() -> list[tuple[str, str, list[tuple[str, str]], datetime]]:
    now = datetime.now(UTC).astimezone()
    return [
        ("c1", "昨天会话", [("b1", "主线")], now - timedelta(days=1)),
        ("c2", "十 天 前", [("b2", "主线")], now - timedelta(days=10)),
        ("c3", "今天会话", [("b3", "主线"), ("b3f", "分叉")], now),
    ]


def test_export_panel_time_filter_hides_old_rows(qtbot: QtBot) -> None:
    host = _host(qtbot)
    panel = ExportPanel(host, _panel_rows(), on_export=lambda *_: None)
    # 「最近 7 天」：10 天前那行应被隐藏
    panel._time_filter.setCurrentIndex(2)
    titles_visible = [row.conversation_title for row in panel._rows if row.isVisible()]
    assert "十 天 前" not in titles_visible
    assert "今天会话" in titles_visible


def test_export_panel_select_none_blocks_export(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QPushButton

    host = _host(qtbot)
    exported: list[object] = []
    panel = ExportPanel(host, _panel_rows(), on_export=lambda *a: exported.append(a))
    panel._set_all(False)
    go = next(b for b in panel.findChildren(QPushButton) if b.text() == "导出")
    go.click()
    assert exported == [], "没有任何勾选时不应触发导出"


def test_export_panel_selection_and_format_reach_callback(qtbot: QtBot) -> None:

    host = _host(qtbot)
    captured: list[tuple[list[str], list[str], str, Path]] = []
    panel = ExportPanel(host, _panel_rows(), on_export=lambda *a: captured.append(a))
    panel._set_all(False)
    # 只勾「今天会话」的两条分支
    panel._rows[2].checkbox.setChecked(True)
    for _bid, check in panel._rows[2]._branch_checks:
        check.setChecked(True)
    jsons = [b for b in panel.findChildren(QRadioButton) if b.text() == "JSON"]
    assert jsons
    jsons[0].setChecked(True)
    go = next(b for b in panel.findChildren(QPushButton) if b.text() == "导出")
    go.click()
    assert len(captured) == 1
    branch_ids, labels, fmt, target = captured[0]
    assert sorted(branch_ids) == ["b3", "b3f"]
    assert set(labels) == {"主线", "分叉"}
    assert fmt == "json"
    assert target.name.startswith("limbowave-export-")


def _host(qtbot: QtBot) -> object:

    host = QWidget()
    host.resize(1000, 800)
    qtbot.addWidget(host)
    host.show()
    return host
