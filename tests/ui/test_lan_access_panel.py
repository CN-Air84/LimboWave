from limbowave.ui.lan_access_panel import LanAccessPanel


def test_default_closed_and_start_signal(qtbot):
    panel = LanAccessPanel()
    qtbot.addWidget(panel)
    assert panel.start_button.isEnabled()
    assert not panel.stop_button.isEnabled()
    assert not panel.pair_button.isEnabled()
    with qtbot.waitSignal(panel.start_requested) as result:
        panel.start_button.click()
    assert result.args[0]["port"] == 8765
    assert result.args[0]["host"] != "0.0.0.0"


def test_failed_start_rolls_back_and_stop_clears_secrets(qtbot):
    panel = LanAccessPanel()
    qtbot.addWidget(panel)
    panel.set_pending()
    assert not panel.start_button.isEnabled()
    panel.set_running(False, error="端口已被占用")
    assert panel.start_button.isEnabled()
    assert panel.status.text() == "端口已被占用"
    panel.set_running(True, "https://192.168.1.2:8765")
    panel.invitation.setText("temporary secret")
    panel.set_running(False)
    assert not panel.invitation.text()
    assert not panel.address.text()


def test_device_revoke_and_matching_phrase(qtbot):
    from PySide6.QtWidgets import QLabel, QPushButton

    panel = LanAccessPanel()
    qtbot.addWidget(panel)
    panel.set_devices(
        [{"device_id": "one", "name": "<b>phone</b>"}],
        [{"request_id": "two", "name": "phone", "phrase": "river-moon"}],
    )
    assert any("river-moon" in label.text() for label in panel.findChildren(QLabel))
    button = next(b for b in panel.findChildren(QPushButton) if b.text() == "撤销")
    with qtbot.waitSignal(panel.revoke_requested) as result:
        button.click()
    assert result.args == ["one"]


def test_http_requires_explicit_risk_acknowledgement(qtbot):
    panel = LanAccessPanel()
    qtbot.addWidget(panel)
    assert not panel.allow_http.isChecked()
    with qtbot.waitSignal(panel.start_requested) as default:
        panel.start_button.click()
    assert default.args[0]["allow_insecure_http"] is False
    panel.allow_http.setChecked(True)
    with qtbot.waitSignal(panel.start_requested) as enabled:
        panel.start_button.click()
    assert enabled.args[0]["allow_insecure_http"] is True
    panel.set_running(True, "http://192.168.1.2:8765")
    assert not panel.allow_http.isEnabled()


def test_unchanged_devices_keep_their_widgets_and_focus(qtbot):
    panel = LanAccessPanel()
    qtbot.addWidget(panel)
    devices = [{"device_id": "one", "name": "phone"}]
    requests = [{"request_id": "two", "name": "tablet", "phrase": "moon-river"}]
    panel.set_devices(devices, requests)
    rows = [panel.device_rows.itemAt(i).widget() for i in range(panel.device_rows.count())]
    panel.set_devices([dict(device) for device in devices], [dict(r) for r in requests])
    assert [panel.device_rows.itemAt(i).widget() for i in range(panel.device_rows.count())] == rows
    requests[0]["phrase"] = "different-phrase"
    panel.set_devices(devices, requests)
    assert panel.device_rows.itemAt(1).widget() is not rows[1]
    panel.set_devices([], [])
    assert panel.device_rows.count() == 0
