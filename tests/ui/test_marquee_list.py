"""名称列表的行内滚动、裁剪及设置页接线回归。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QLineEdit, QTabWidget, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.settings_service import SettingsService
from limbowave.domain.models import LogicalModel
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.ui import theme
from limbowave.ui.marquee_list import MarqueeListWidget, _scroll_offset
from limbowave.ui.settings_dialog import SettingsDialog
from limbowave.ui.settings_panel import SettingsPage


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(0, 0), (899, 0), (1400, 14), (1900, 28), (2799, 28), (3300, 14), (3800, 0)],
)
def test_scroll_pauses_at_both_ends_and_returns(elapsed: int, expected: float) -> None:
    assert _scroll_offset(elapsed, 28) == pytest.approx(expected)
    assert _scroll_offset(elapsed, 0) == 0


@pytest.mark.parametrize("styled", [False, True])
def test_only_long_row_moves_without_horizontal_overflow(qtbot: QtBot, styled: bool) -> None:
    view = MarqueeListWidget()
    qtbot.addWidget(view)
    if styled:
        view.setStyleSheet(theme.app_stylesheet())
    view.resize(210, 180)
    hint = view.sizeHint()
    view.addItems(["Long display name / 超长名称 / " * 8, "Short"])
    view.setCurrentRow(0)
    view.show()
    qtbot.waitUntil(view._timer.isActive)
    assert view.sizeHint() == hint
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert view.horizontalScrollBar().maximum() == 0
    assert not view.horizontalScrollBar().isVisible()
    assert view.visualItemRect(view.item(0)).width() <= view.viewport().width()
    assert len(view._scrolling) == 1
    initial = view.viewport().grab().toImage()
    index = view.indexFromItem(view.item(0))
    overflow = next(iter(view._scrolling.values()))[0]
    qtbot.waitUntil(lambda: view._offset_for(index, overflow) > 10, timeout=2500)
    shifted = view.viewport().grab().toImage()
    assert shifted != initial
    short_rect = view.visualItemRect(view.item(1))
    assert shifted.copy(short_rect) == initial.copy(short_rect)
    if styled:
        # 文字不能侵入列表项的左右留白，选中底色也不随文字移动。
        assert shifted.copy(0, 0, 5, shifted.height()) == initial.copy(0, 0, 5, initial.height())
        right = shifted.width() - 5
        assert shifted.copy(right, 0, 5, shifted.height()) == initial.copy(
            right, 0, 5, initial.height()
        )


def test_resize_rename_hide_and_clear_reset_animation(qtbot: QtBot) -> None:
    view = MarqueeListWidget()
    qtbot.addWidget(view)
    view.resize(180, 140)
    name = "A long display name that needs room " * 2
    view.addItem(name)
    view.show()
    qtbot.waitUntil(view._timer.isActive)

    view.resize(1200, 140)
    qtbot.waitUntil(lambda: not view._timer.isActive())
    assert not view._scrolling
    view.resize(180, 140)
    qtbot.waitUntil(view._timer.isActive)
    view.hide()
    assert not view._timer.isActive()
    assert not view._scrolling
    view.show()
    qtbot.waitUntil(view._timer.isActive)
    view.item(0).setText("Short")
    qtbot.waitUntil(lambda: not view._timer.isActive())
    assert not view._scrolling
    view.item(0).setText(name)
    qtbot.waitUntil(view._timer.isActive)
    view.clear()
    assert not view._timer.isActive()
    assert not view._scrolling


def test_font_change_recalculates_overflow(qtbot: QtBot) -> None:
    view = MarqueeListWidget()
    qtbot.addWidget(view)
    view.resize(300, 140)
    view.addItem("Medium display name")
    view.show()
    view.viewport().grab()
    assert not view._timer.isActive()
    font = view.font()
    font.setPixelSize(40)
    view.setFont(font)
    qtbot.waitUntil(view._timer.isActive)
    font.setPixelSize(12)
    view.setFont(font)
    qtbot.waitUntil(lambda: not view._timer.isActive())
    assert not view._scrolling


def test_offscreen_rows_do_not_keep_timer_running(qtbot: QtBot) -> None:
    view = MarqueeListWidget()
    qtbot.addWidget(view)
    view.resize(180, 140)
    view.addItems(["Overflowing name " * 10, *[f"Short {i}" for i in range(50)]])
    view.show()
    qtbot.waitUntil(view._timer.isActive)
    view.scrollToBottom()
    qtbot.waitUntil(lambda: not view._timer.isActive())
    assert not view._scrolling
    view.scrollToTop()
    qtbot.waitUntil(view._timer.isActive)


@pytest.mark.parametrize("full_page", [False, True])
@pytest.mark.parametrize("endpoints", [False, True])
def test_settings_name_lists_keep_ids_internal_and_scroll(
    qtbot: QtBot, tmp_path: Path, vault_key: VaultKey, full_page: bool, endpoints: bool
) -> None:
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    name = "同名显示名称 Display name " * 15
    for suffix in ("a", "b"):
        settings.upsert_endpoint(EndpointConfig(
            id=f"endpoint-{suffix}", name=name, base_url="https://example.com/v1",
            api=ProviderProtocol.OPENAI_COMPLETIONS,
        ))
        settings.create_model(LogicalModel(id=f"model-{suffix}", name=name))
    if full_page:
        host = SettingsPage(None, settings, credentials)
        host.select_tab("站点端点" if endpoints else "逻辑模型", animated=False)
        tab = host.endpoints_tab if endpoints else host.models_tab
    else:
        host = SettingsDialog(settings, credentials)
        host.findChild(QTabWidget).setCurrentIndex(1 if endpoints else 0)
        tab = host._endpoints_tab if endpoints else host._models_tab
    qtbot.addWidget(host)
    host.setStyleSheet(theme.app_stylesheet())
    host.resize(1100, 800)
    host.show()
    view = tab._list
    assert isinstance(view, MarqueeListWidget)
    assert [view.item(i).text() for i in range(2)] == [name, name]
    assert view.item(1).data(Qt.ItemDataRole.UserRole) == (
        "endpoint-b" if endpoints else "model-b"
    )
    qtbot.waitUntil(view._timer.isActive)
    assert view.width() < 350
    assert not view.horizontalScrollBar().isVisible()
    assert view.horizontalScrollBar().maximum() == 0

    view.setCurrentRow(1)
    assert tab._selected_id() == ("endpoint-b" if endpoints else "model-b")
    assert tab._name.text() == name
    tab._name.setText("Renamed")
    tab._on_save()
    assert tab._selected_id() == ("endpoint-b" if endpoints else "model-b")
    assert view.currentItem().text() == "Renamed"
    assert view.item(0).text() == name
    if not endpoints:
        tab._on_default_model()
        assert view.currentItem().text() == "Renamed"
        assert "★" in view.currentItem().toolTip()
        assert "未绑定" in view.currentItem().toolTip()


@pytest.mark.parametrize("palette", ["dark", "light"])
@pytest.mark.parametrize("long_name", [False, True])
def test_current_indicator_survives_focus_and_tracks_current_not_selection(
    qtbot: QtBot, palette: str, long_name: bool
) -> None:
    previous = theme.current_palette().name
    try:
        theme.set_palette(palette)
        host = QWidget()
        qtbot.addWidget(host)
        layout = QVBoxLayout(host)
        view = MarqueeListWidget()
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        editor = QLineEdit()
        layout.addWidget(view)
        layout.addWidget(editor)
        host.setStyleSheet(theme.app_stylesheet())
        host.resize(240, 200)
        view.addItems(["Long name " * 20 if long_name else "First", "Second"])
        view.setCurrentRow(0)
        host.show()
        editor.setFocus()
        qtbot.waitUntil(editor.hasFocus)

        def has_indicator(row: int) -> bool:
            image = view.viewport().grab().toImage()
            rect = view.visualItemRect(view.item(row))
            ratio = image.devicePixelRatio()
            return image.pixelColor(
                int((rect.left() + 3) * ratio), int(rect.center().y() * ratio)
            ) == QColor(theme.ACCENT)

        assert has_indicator(0)
        assert not has_indicator(1)
        if long_name:
            index = view.indexFromItem(view.item(0))
            overflow = next(iter(view._scrolling.values()))[0]
            qtbot.waitUntil(lambda: view._offset_for(index, overflow) > 10, timeout=2500)
            assert has_indicator(0)
        view.setCurrentRow(1)
        view.item(0).setSelected(True)
        assert len(view.selectedItems()) == 2
        qtbot.waitUntil(
            lambda: view._indicator_animation.state() == QAbstractAnimation.State.Stopped
        )
        assert not has_indicator(0)
        assert has_indicator(1)
        view.setCurrentRow(-1)
        assert not has_indicator(0)
        assert not has_indicator(1)
    finally:
        theme.set_palette(previous)


def test_indicator_animates_and_retargets_without_list_focus_border(qtbot: QtBot) -> None:
    view = MarqueeListWidget()
    qtbot.addWidget(view)
    view.setStyleSheet(theme.app_stylesheet())
    view.resize(240, 240)
    view.addItems([f"Item {i}" for i in range(30)])
    view.setCurrentRow(0)
    view.show()
    view.setFocus()
    qtbot.waitUntil(view.hasFocus)
    # 无焦点描边，视口贴合整个列表；保留键盘导航能力。
    assert view.frameWidth() == 0
    assert view.viewport().geometry().topLeft() == view.rect().topLeft()
    assert view.viewport().height() == view.height()
    start = view._current_indicator.geometry()
    view.setCurrentRow(3)
    animation = view._indicator_animation
    assert animation.state() == QAbstractAnimation.State.Running
    assert view._current_indicator.geometry() == start
    animation.setCurrentTime(animation.duration() // 2)
    middle = view._current_indicator.geometry()
    assert start.y() < middle.y() < animation.endValue().y()
    assert middle.height() == start.height() + 48
    assert middle.width() == start.width()
    qtbot.keyClick(view, Qt.Key.Key_Up)
    assert view.currentRow() == 2
    assert animation.startValue() == middle
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped)
    assert view._current_indicator.geometry() == animation.endValue()
    view.scrollToBottom()
    assert not view._current_indicator.isVisible()
    view.scrollToTop()
    assert view._current_indicator.isVisible()
    view.clear()
    assert not view._current_indicator.isVisible()
    assert animation.state() == QAbstractAnimation.State.Stopped
