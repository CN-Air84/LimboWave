"""Deterministic rendering and lifecycle checks for settings page motion."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from limbowave.ui import theme
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow, _page_snapshot, _PageTransition


def _solid_frame(color: str, width: int = 480, height: int = 320) -> QPixmap:
    frame = QPixmap(width, height)
    frame.fill(QColor(color))
    return frame


def _overlay(qtbot: QtBot, direction: int = 1) -> tuple[QWidget, _PageTransition]:
    host = QWidget()
    host.resize(480, 320)
    qtbot.addWidget(host)
    overlay = _PageTransition(
        host, _solid_frame("#ff0000"), _solid_frame("#0000ff"), direction,
        layered=True,
    )
    overlay.setGeometry(host.rect())
    host.show()
    overlay.show()
    return host, overlay


def _pixel(overlay: _PageTransition, progress: float) -> QColor:
    overlay._set_progress(progress)
    image = overlay.grab().toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("surface", ["#101018", "#f4f5f8"])
def test_layered_frames_have_exact_opaque_endpoints(qtbot, monkeypatch, direction, surface):
    monkeypatch.setattr(theme, "BG_APP", surface)
    _host, overlay = _overlay(qtbot, direction)
    assert _pixel(overlay, 0.0) == QColor("#ff0000")
    assert _pixel(overlay, 1.0) == QColor("#0000ff")
    for progress in (0.1, 0.4, 0.7):
        overlay._set_progress(progress)
        image = overlay.grab().toImage()
        for x in (0, image.width() // 2, image.width() - 1):
            assert image.pixelColor(x, image.height() // 2).alpha() == 255


def test_fades_are_staggered_instead_of_double_exposing_text(qtbot, monkeypatch):
    monkeypatch.setattr(theme, "BG_APP", "#000000")
    _host, overlay = _overlay(qtbot)
    early = _pixel(overlay, 0.15)
    middle = _pixel(overlay, 0.4)
    late = _pixel(overlay, 0.8)
    assert early.red() > 100 and early.blue() == 0
    assert middle.red() < 60 and middle.blue() < 60
    assert late.red() == 0 and late.blue() > 180


def _window(qtbot: QtBot) -> MainWindow:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    return window


def _pause_at(window: MainWindow, fraction: float) -> _PageTransition:
    animation = window._transition_animation
    assert animation is not None
    animation.pause()
    animation.setCurrentTime(round(animation.duration() * fraction))
    assert window._transition is not None
    return window._transition


def test_settings_captures_each_page_once_and_keeps_live_geometry(qtbot, monkeypatch):
    import limbowave.ui.main_window as module

    window = _window(qtbot)
    page = QWidget()
    page.setStyleSheet("background: #193452")
    captures = []
    original = module._page_snapshot

    def capture(widget):
        captures.append(widget)
        return original(widget)

    monkeypatch.setattr(module, "_page_snapshot", capture)
    window.show_full_page(page)
    overlay = _pause_at(window, 0.0)
    assert overlay._incoming_widget is page
    assert overlay._incoming_pixmap is not None
    assert captures == [window._workspace, page]
    for fraction in (0.15, 0.4, 0.8):
        _pause_at(window, fraction)
        overlay.grab()
        assert page.pos() == QPoint(0, 0)
    assert captures == [window._workspace, page]


def test_exit_is_brisk_and_finishes_before_deferred_sidebar_refresh(qtbot):
    window = _window(qtbot)
    window.show_full_page(QWidget())
    enter = window._transition_animation
    assert enter is not None
    entry_duration = enter.duration()
    assert 240 <= entry_duration <= 320
    enter.setCurrentTime(entry_duration)
    window.show_workspace(release_page=False)
    leave = window._transition_animation
    assert leave is not None
    assert 160 <= leave.duration() < min(230, entry_duration)


@pytest.mark.parametrize("size", [(1280, 800), (1537, 947)])
@pytest.mark.parametrize("fraction", [0.15, 0.45, 0.8])
def test_reversing_navigation_uses_current_composite(qtbot, fraction, size):
    window = _window(qtbot)
    window.resize(*size)
    page = QWidget()
    page.setStyleSheet("background: #193452")
    window.show_full_page(page)
    entry = _pause_at(window, fraction)
    before_close = _page_snapshot(entry).toImage()
    window.show_workspace(release_page=False)
    leaving = _pause_at(window, 0.0)
    assert leaving._outgoing.toImage() == before_close
    assert _page_snapshot(leaving).toImage() == before_close
    _pause_at(window, fraction)
    before_open = _page_snapshot(leaving).toImage()
    window.show_full_page(page)
    reopened = _pause_at(window, 0.0)
    assert reopened._outgoing.toImage() == before_open
    assert _page_snapshot(reopened).toImage() == before_open
    assert window._root_stack.count() == 2


def test_resize_settles_to_stationary_live_page(qtbot):
    window = _window(qtbot)
    page = QWidget()
    window.show_full_page(page)
    _pause_at(window, 0.4)
    window.resize(window.width() + 64, window.height() + 32)
    qtbot.waitUntil(lambda: not window.transition_active)
    assert window._root_stack.currentWidget() is page
    assert page.pos() == QPoint(0, 0)
    assert page.size() == window._root_stack.size()
    assert page.graphicsEffect() is None
    assert not page.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_login_entry_does_not_capture_or_freeze_live_logo(qtbot):
    window = _window(qtbot)
    login = LoginPage()
    window.show_full_page(login)
    overlay = _pause_at(window, 0.4)
    assert overlay._incoming_widget is login
    assert overlay._incoming_pixmap is None


@pytest.mark.parametrize("direction", [1, -1])
def test_motion_is_reciprocal_and_restrained(qtbot, monkeypatch, direction):
    monkeypatch.setattr(theme, "BG_APP", "#000000")
    _host, overlay = _overlay(qtbot, direction)
    for attribute, color in (("_outgoing", "#ff0000"), ("_incoming_pixmap", "#0000ff")):
        frame = _solid_frame("#000000")
        painter = QPainter(frame)
        painter.fillRect(240, 0, 1, 320, QColor(color))
        painter.end()
        setattr(overlay, attribute, frame)
    overlay._set_progress(0.4)
    image = overlay.grab().toImage()
    old_x = max(range(image.width()), key=lambda x: image.pixelColor(x, 160).red())
    new_x = max(range(image.width()), key=lambda x: image.pixelColor(x, 160).blue())
    dpr = image.devicePixelRatio()
    assert 0 < -direction * (old_x / dpr - 240) <= 24
    assert 0 < direction * (new_x / dpr - 240) <= 24


def test_both_page_captures_respect_high_dpi_budget(qtbot):
    from limbowave.ui.main_window import _MAX_TRANSITION_PIXELS

    class HighDpiPage(QWidget):
        def devicePixelRatioF(self):
            return 2.0

    window = _window(qtbot)
    window.resize(2000, 1200)
    page = HighDpiPage()
    page.setStyleSheet("background: #193452")
    window.show_full_page(page)
    overlay = _pause_at(window, 0.4)
    for frame in (overlay._outgoing, overlay._incoming_pixmap):
        assert frame is not None and not frame.isNull()
        # Rounding each dimension may add one row/column to the pixel budget.
        assert frame.width() * frame.height() <= _MAX_TRANSITION_PIXELS + 3000
        assert abs(frame.deviceIndependentSize().width() - page.width()) < 1
    incoming = overlay._incoming_pixmap.toImage()
    assert incoming.pixelColor(incoming.width() // 2, incoming.height() // 2) == QColor("#193452")


def test_layered_motion_does_not_drop_16ms_animation_ticks(qtbot, monkeypatch):
    import limbowave.ui.main_window as module

    _host, overlay = _overlay(qtbot)
    clock = [100.0]
    updates = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(overlay, "update", lambda: updates.append(overlay._progress))
    overlay._last_update = clock[0]
    for progress in (0.1, 0.2, 0.3, 0.4, 0.5):
        clock[0] += 0.016
        overlay._set_progress(progress)
    assert updates == [0.1, 0.2, 0.3, 0.4, 0.5]


def test_close_during_settings_tab_switch_reopens_cleanly(qtbot, tmp_path, vault_key):
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.credential_service import CredentialService
    from limbowave.application.services.settings_service import SettingsService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
    from limbowave.infrastructure.crypto.secret_store import SecretStore
    from limbowave.ui.settings_panel import SettingsPage

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    window = _window(qtbot)
    page = SettingsPage(window, settings, credentials)
    window.show_full_page(page)
    window._stop_transition()
    page.select_tab("站点端点", animated=True)
    tabs = page._pages
    tab_animation = tabs._animation
    assert tab_animation is not None
    tab_animation.pause()
    tab_animation.setCurrentTime(tab_animation.duration() // 2)
    window.show_workspace(release_page=False)
    _pause_at(window, 0.4)
    window.show_full_page(page)
    _pause_at(window, 0.4)
    assert tabs.current_index == page._rail.index_of("站点端点")
    assert not tabs._effect.isEnabled()
    assert tabs._stack.pos() == QPoint(0, 0)
    assert page.pos() == QPoint(0, 0)
    window._stop_transition()
    assert page.isVisibleTo(window)


def test_click_during_entry_reveals_live_primary_tab_animation(qtbot, tmp_path, vault_key):
    from PySide6.QtWidgets import QApplication

    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.credential_service import CredentialService
    from limbowave.application.services.settings_service import SettingsService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
    from limbowave.infrastructure.crypto.secret_store import SecretStore
    from limbowave.ui.settings_panel import SettingsPage

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    window = _window(qtbot)
    page = SettingsPage(window, settings, credentials)
    window.show_full_page(page)
    _pause_at(window, 0.35)
    target = page._rail.index_of("站点端点")
    button = page._rail._buttons[target]
    assert window._transition is not None
    assert window._transition.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    hit = QApplication.widgetAt(button.mapToGlobal(button.rect().center()))
    assert hit is not window._transition
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert window._transition is None
    assert page._pages._animation is not None
    assert page._rail._indicator_animation.state().name == "Running"
    page._pages._animation.setCurrentTime(160)
    assert page._pages.current_index == target
    assert page._pages._animation is not None
    page._pages._animation.setCurrentTime(95)
    assert 0 < page._pages._effect.opacity() < 1


def test_click_during_entry_reveals_live_secondary_tab_animation(qtbot, tmp_path, vault_key):
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.credential_service import CredentialService
    from limbowave.application.services.settings_service import SettingsService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
    from limbowave.infrastructure.crypto.secret_store import SecretStore
    from limbowave.ui.settings_panel import SettingsPage

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    window = _window(qtbot)
    page = SettingsPage(window, settings, credentials)
    page.select_tab("站点端点", animated=False)
    window.show_full_page(page)
    _pause_at(window, 0.35)
    tabs = page.endpoint_workspace._subtabs
    qtbot.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(1).center())
    assert window._transition is None
    assert page.endpoint_workspace._stack._animation is not None
    assert tabs._indicator_animation.state().name == "Running"


def test_entry_overlay_filter_is_removed_when_navigation_finishes(qtbot, tmp_path, vault_key):
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.credential_service import CredentialService
    from limbowave.application.services.settings_service import SettingsService
    from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
    from limbowave.infrastructure.crypto.secret_store import SecretStore
    from limbowave.ui.settings_panel import SettingsPage

    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "secrets.json"))
    window = _window(qtbot)
    page = SettingsPage(window, settings, credentials)
    window.show_full_page(page)
    overlay = _pause_at(window, 0.2)
    assert overlay._layered
    window.show_workspace(release_page=False)
    leaving = window._transition
    assert leaving is not None and leaving is not overlay
    assert not overlay.isVisible()
    window._stop_transition()
    page.select_tab("站点端点", animated=False)
    assert page._pages.current_index == page._rail.index_of("站点端点")
    window.show_full_page(page)
    window._stop_transition()
    assert page.isVisibleTo(window)


class _PatternPage(QWidget):
    """Asymmetric landmarks reveal scaling/cropping hidden by solid-color tests."""

    colors = ("#cf2020", "#20bf40", "#2040df", "#dfbf20")

    def paintEvent(self, event):
        painter = QPainter(self)
        width, height = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(self.colors[0]))
        painter.fillRect(QRectF(width / 3, 0, width * 2 / 3, height), QColor(self.colors[1]))
        painter.fillRect(QRectF(0, height / 3, width, height * 2 / 3), QColor(self.colors[2]))
        painter.fillRect(
            QRectF(width / 3, height / 3, width * 2 / 3, height * 2 / 3),
            QColor(self.colors[3]),
        )


def _assert_pattern(image, colors):
    for x, y, index in (
        (0.0, 0.0, 0), (1.0, 0.0, 1), (0.0, 1.0, 2), (1.0, 1.0, 3),
        (0.25, 0.25, 0), (0.4, 0.25, 1), (0.25, 0.4, 2), (0.4, 0.4, 3),
    ):
        pixel = image.pixelColor(round(x * (image.width() - 1)), round(y * (image.height() - 1)))
        assert pixel == QColor(colors[index]), (x, y, pixel.name(), colors[index])


@pytest.mark.parametrize("size", [(913, 617), (1280, 800), (1537, 947), (2003, 1201)])
@pytest.mark.parametrize("mode", ["layered", "fade", "scroll"])
def test_transition_maps_complete_snapshot_to_logical_page(qtbot, size, mode):
    page = _PatternPage()
    page.resize(*size)
    qtbot.addWidget(page)
    page.show()
    old_frame = _page_snapshot(page)
    _assert_pattern(old_frame.toImage(), page.colors)
    old_colors = page.colors
    page.colors = tuple(reversed(page.colors))
    new_frame = _page_snapshot(page)
    _assert_pattern(new_frame.toImage(), page.colors)
    overlay = _PageTransition(
        page, old_frame, new_frame, 1, layered=mode == "layered", scroll_down=mode == "scroll",
    )
    overlay.setGeometry(page.rect())
    overlay.show()
    for progress, colors in ((0.0, old_colors), (1.0, page.colors)):
        overlay._set_progress(progress)
        _assert_pattern(overlay.grab().toImage(), colors)


@pytest.mark.parametrize("size", [(1103, 743), (1717, 1013)])
def test_settings_snapshot_matches_page_after_window_resize(qtbot, size):
    window = _window(qtbot)
    window.resize(*size)
    page = _PatternPage()
    window.show_full_page(page)
    entering = _pause_at(window, 0.0)
    entering._set_progress(1.0)
    _assert_pattern(entering.grab().toImage(), page.colors)
    window._stop_transition()
    # Returning after resizing settings must capture the new, not the old size.
    window.resize(size[0] + 111, size[1] + 71)
    assert page.size() == window._root_stack.size()
    window.show_workspace(release_page=False)
    leaving = _pause_at(window, 0.0)
    _assert_pattern(leaving.grab().toImage(), page.colors)
