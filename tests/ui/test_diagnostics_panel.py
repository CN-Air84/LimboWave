"""独立面板验收；不创建 MainWindow、不连接业务内核。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from limbowave.infrastructure.diagnostics import LogConfig, LogManager
from limbowave.ui.diagnostics_panel import DiagnosticsPanel


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[LogManager]:
    with LogManager(LogConfig(tmp_path, level="debug", disk_bytes_per_second=0)) as value:
        yield value


def test_panel_filters_details_and_statistics(qtbot: QtBot, manager: LogManager) -> None:
    log = manager.get_logger("app.worker")
    log.debug("debug")
    log.error("网络错误", extra={"password": "must-not-show"})
    log.error("网络错误")
    manager.get_logger("other").warning("different")
    panel = DiagnosticsPanel(manager)
    qtbot.addWidget(panel)
    assert panel._model.rowCount() == 4
    panel._level.setCurrentText("ERROR")
    assert panel._model.rowCount() == 2
    assert "2 次" in panel._stats.toPlainText()
    panel._table.selectRow(0)
    assert "网络错误" in panel._details.toPlainText()
    assert "[REDACTED]" in panel._details.toPlainText()
    assert "must-not-show" not in panel._details.toPlainText()
    panel._search.setText("nothing")
    panel.refresh(force=True)
    assert panel._model.rowCount() == 0
    assert not panel._details.toPlainText()
    panel._search.clear()
    panel._level.setCurrentText("DEBUG")
    panel._source.setText("app")
    panel.refresh(force=True)
    assert panel._model.rowCount() == 3


def test_pause_freezes_window_and_hide_stops_polling(qtbot: QtBot, manager: LogManager) -> None:
    logger = manager.get_logger("app")
    logger.info("before")
    panel = DiagnosticsPanel(manager)
    qtbot.addWidget(panel)
    assert not panel._timer.isActive()
    panel.show()
    assert panel._timer.isActive()
    panel._pause.setChecked(True)
    assert not panel._timer.isActive()
    logger.info("after")
    panel.refresh()
    assert panel._model.rowCount() == 1
    panel._search.setText("after")
    panel.refresh(force=True)
    assert panel._model.rowCount() == 0  # 暂停后改筛选仍只查冻结的窗口
    panel._pause.setChecked(False)
    assert panel._model.rowCount() == 1
    assert panel._model.entries[0].message == "after"
    assert panel._timer.isActive()
    panel.hide()
    assert not panel._timer.isActive()
    assert not panel._debounce.isActive()
    assert manager.status().state == "running"


def test_bounded_display_and_refresh_preserves_selection(qtbot: QtBot, manager: LogManager) -> None:
    logger = manager.get_logger("app")
    for index in range(10):
        logger.info("%s", index)
    panel = DiagnosticsPanel(manager, display_limit=3)
    qtbot.addWidget(panel)
    assert [entry.message for entry in panel._model.entries] == ["7", "8", "9"]
    panel._table.selectRow(1)
    assert '"message": "8"' in panel._details.toPlainText()
    logger.info("10")
    panel.refresh()
    assert panel._model.rowCount() == 3
    assert panel._table.currentIndex().row() == 0
    assert '"message": "8"' in panel._details.toPlainText()
    assert "当前显示 3 条" in panel._stats.toPlainText()


def test_writer_failure_is_visible(qtbot: QtBot, manager: LogManager, monkeypatch) -> None:
    def fail(payloads) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(manager._sink, "write_batch", fail)
    manager.get_logger("app").error("retained in memory")
    assert manager.flush()
    panel = DiagnosticsPanel(manager)
    qtbot.addWidget(panel)
    assert panel._model.rowCount() == 1
    assert "文件故障 1" in panel._status.text()
    assert "写盘异常" in panel._status.text()


def test_unchanged_poll_does_not_reset_model(qtbot: QtBot, manager: LogManager) -> None:
    manager.get_logger("app").info("one")
    panel = DiagnosticsPanel(manager)
    qtbot.addWidget(panel)
    resets = []
    panel._model.modelReset.connect(lambda: resets.append(True))
    panel.refresh()
    panel.refresh()
    assert not resets
