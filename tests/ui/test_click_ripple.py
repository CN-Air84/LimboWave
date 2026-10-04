"""Click feedback stays local and never consumes the control's own events."""

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QListWidget, QPushButton, QWidget

from limbowave.ui.click_ripple import _RippleLayer, install_click_ripples


def test_dynamic_button_click_is_local_and_nonblocking(qtbot):
    root = QWidget()
    root.resize(260, 140)
    qtbot.addWidget(root)
    install_click_ripples(root)
    controller = root._limbowave_click_ripples
    install_click_ripples(root)
    assert root._limbowave_click_ripples is controller
    button = QPushButton("Click", root)
    button.setGeometry(20, 20, 100, 40)
    sibling = QPushButton("Other", root)
    sibling.move(150, 20)
    root.show()
    clicks = []
    button.clicked.connect(lambda: clicks.append(True))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(12, 15))
    layer = button.findChildren(_RippleLayer)[0]
    assert layer.parentWidget() is button
    assert layer.geometry() == button.rect()
    assert layer.origin == QPoint(12, 15)
    assert not sibling.findChildren(_RippleLayer)
    assert clicks == [True]
    layer.animation.setCurrentTime(400)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(70, 20))
    layers = button.findChildren(_RippleLayer)
    assert len(layers) == 2
    assert layers[0] is layer
    assert layer.animation.currentTime() >= 400
    assert layer.origin == QPoint(12, 15)
    assert layers[1].origin == QPoint(70, 20)
    assert layers[1].animation.currentTime() < 100
    assert all(ripple.isVisible() for ripple in layers)
    assert clicks == [True, True]
    layer.animation.setCurrentTime(1200)
    qtbot.waitUntil(lambda: len(button.findChildren(_RippleLayer)) == 1)
    assert layers[1].isVisible()
    qtbot.waitUntil(lambda: not button.findChildren(_RippleLayer), timeout=2000)


def test_list_ripple_is_clipped_to_clicked_item(qtbot):
    root = QListWidget()
    root.resize(260, 240)
    root.addItems(["First", "Second"])
    qtbot.addWidget(root)
    install_click_ripples(root)
    root.show()
    rect = root.visualItemRect(root.item(1))
    qtbot.mouseClick(root.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    layer = root.viewport().findChildren(_RippleLayer)[0]
    assert layer.geometry() == rect
    assert root.currentRow() == 1
    layer.animation.stop()
    layer.hide()
    qtbot.mouseClick(root.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(20, 180))
    assert not layer.isVisible()


def test_no_ripple_on_background_right_click_disabled_or_opt_out(qtbot):
    root = QWidget()
    button = QPushButton("Button", root)
    qtbot.addWidget(root)
    install_click_ripples(root)
    root.show()
    qtbot.mouseClick(root, Qt.MouseButton.LeftButton)
    assert not root.findChildren(_RippleLayer)
    qtbot.mouseClick(button, Qt.MouseButton.RightButton)
    button.setDisabled(True)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    button.setEnabled(True)
    button.setProperty("rippleDisabled", True)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert not button.findChildren(_RippleLayer)


def test_round_corners_remain_transparent(qtbot):
    button = QPushButton("Click")
    button.resize(100, 40)
    qtbot.addWidget(button)
    layer = _RippleLayer(button)
    layer.start(button.rect(), QPointF(0, 0))
    layer.animation.stop()
    layer.progress = 0.2
    image = layer.grab().toImage()
    assert image.pixelColor(0, 0).alpha() == 0
    assert any(image.pixelColor(x, 10).alpha() > 0 for x in range(10, 50))


def test_ripple_uses_silver_white_and_slow_duration(qtbot):
    button = QPushButton("Accent")
    button.setProperty("accent", True)
    qtbot.addWidget(button)
    layer = _RippleLayer(button)
    layer.start(button.rect(), QPointF(10, 10))
    assert layer.animation.duration() == 1200
    assert layer.color.name() == "#e8e8e8"
    layer.animation.stop()
    layer.progress = 1.0
    image = layer.grab().toImage()
    assert all(
        image.pixelColor(x, y).alpha() == 0
        for x in range(image.width())
        for y in range(image.height())
    )


def test_clicks_on_different_rows_keep_independent_bounds(qtbot):
    root = QListWidget()
    root.resize(260, 240)
    root.addItems(["First", "Second"])
    qtbot.addWidget(root)
    install_click_ripples(root)
    root.show()
    rects = [root.visualItemRect(root.item(i)) for i in range(2)]
    for rect in rects:
        qtbot.mouseClick(root.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    layers = root.viewport().findChildren(_RippleLayer)
    assert len(layers) == 2
    assert [layer.geometry() for layer in layers] == rects
    assert all(layer.isVisible() for layer in layers)
    qtbot.waitUntil(lambda: not root.viewport().findChildren(_RippleLayer), timeout=2000)


def test_double_click_triggers_another_ripple(qtbot):
    button = QPushButton("Click")
    qtbot.addWidget(button)
    install_click_ripples(button)
    button.show()
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.mouseDClick(button, Qt.MouseButton.LeftButton)
    assert len(button.findChildren(_RippleLayer)) == 2
