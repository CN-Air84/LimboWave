from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel

from tools.demos.oobe_joint_demo import JointOobeDemo


@pytest.fixture
def demo(qtbot):
    window = JointOobeDemo()
    qtbot.addWidget(window)
    window.show_screen("network")
    window.show()
    return window


def test_shared_pages_coexist_with_approved_production_tour(demo):
    from limbowave.ui.oobe_copy import SCREENS

    assert {"intro", "voice", "network", "welcome"} <= set(demo.indices)
    assert "network" in SCREENS and "voice" in SCREENS
    assert not demo.network_tryout.configured.isChecked()


def test_real_defaults_and_separate_simulated_permissions(demo):
    trial = demo.network_tryout
    trial.request_search()
    assert trial.state == "unconfigured"
    trial.configured.setChecked(True)
    trial.request_search()
    assert trial.state == "search_permission" and trial.evidence.isHidden()
    trial.approve()
    assert trial.state == "snippets" and "搜索到" in trial.evidence.text()
    trial.request_page()
    assert trial.state == "page_permission" and trial.evidence.isHidden()
    trial.approve()
    assert trial.state == "page_content" and "虚构演示" in trial.evidence.text()


def test_rejection_and_page_departure_clear_approval(demo, qtbot):
    trial = demo.network_tryout
    trial.configured.setChecked(True)
    trial.request_search()
    qtbot.keyClick(demo, Qt.Key.Key_Escape)
    assert trial.state == "denied" and trial.evidence.isHidden()
    trial.request_search()
    trial.approve()
    demo.show_screen("voice")
    demo.show_screen("network")
    assert trial.state == "ready" and trial.evidence.isHidden()


def test_minimum_window_preserves_title_and_footer(demo, qtbot):
    demo.toggle_theme()
    demo.resize(1000, 760)
    trial = demo.network_tryout
    trial.configured.setChecked(True)
    trial.request_search()
    trial.approve()
    trial.request_page()
    trial.approve()
    qtbot.wait(80)
    page = demo.pages.widget(demo.indices["network"])
    title = page.findChild(QLabel, "oobeTitle")
    assert title.height() >= title.fontMetrics().lineSpacing() * 2
    assert (
        demo.network_continue_button.mapTo(demo, QPoint()).y()
        + demo.network_continue_button.height()
        < demo.height()
    )
    assert demo.network_scroll.widgetResizable()
