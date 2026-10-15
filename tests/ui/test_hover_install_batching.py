from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from limbowave.domain.appearance import MaterialSettings
from limbowave.ui.theme_effects import install_hover_suspension


def test_child_added_batch_scans_once_and_installs_new_subtree(qtbot, monkeypatch):
    root = QWidget()
    qtbot.addWidget(root)
    old = [QPushButton(str(i), root) for i in range(30)]
    install_hover_suspension(root, MaterialSettings())
    controller = root._limbowave_hover_suspension
    scan = Mock(wraps=root.findChildren)
    monkeypatch.setattr(root, "findChildren", scan)
    helpers = [QObject(root) for _ in range(20)]
    container = QWidget(root)
    added = QPushButton("new", container)
    qtbot.waitUntil(lambda: bool(added.property("limbowaveHoverFilter")))
    assert scan.call_count == 1
    assert all(button.property("limbowaveHoverFilter") for button in old)
    assert len(helpers) == 20
    scan.reset_mock()
    later = QPushButton("later", root)
    qtbot.waitUntil(lambda: bool(later.property("limbowaveHoverFilter")))
    assert scan.call_count == 1
    assert controller is root._limbowave_hover_suspension


def test_explicit_install_consumes_pending_scan(qtbot, monkeypatch):
    root = QWidget()
    qtbot.addWidget(root)
    install_hover_suspension(root, MaterialSettings())
    scan = Mock(wraps=root.findChildren)
    monkeypatch.setattr(root, "findChildren", scan)
    button = QPushButton("new", root)
    install_hover_suspension(root, MaterialSettings(hover_suspend_enabled=False))
    assert button.property("limbowaveHoverFilter")
    QApplication.processEvents()
    assert scan.call_count == 1


def test_destroying_root_cancels_pending_install(qtbot, monkeypatch):
    root = QWidget()
    install_hover_suspension(root, MaterialSettings())
    scan = Mock(wraps=root.findChildren)
    monkeypatch.setattr(root, "findChildren", scan)
    helper = QObject(root)
    root.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    scan.assert_not_called()
    assert helper is not None
