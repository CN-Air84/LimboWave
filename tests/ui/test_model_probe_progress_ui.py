"""Row progress painting, endpoint isolation and GUI-thread delivery."""
from __future__ import annotations

import ast
import asyncio
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractAnimation
from PySide6.QtWidgets import QWidget

from limbowave.application.services import model_probe
from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.model_probe import ModelProbeProgress
from limbowave.application.services.settings_service import SettingsService
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.ui import theme
from limbowave.ui.model_probe_page import ModelProbeTask
from limbowave.ui.settings_panel import SettingsPage
from tests.ui.test_model_probe_page import _endpoint, _page, _result, _row


def test_progress_preserves_controls_and_resets_for_retry(qtbot):
    page = _page(qtbot, "m", "other")
    row = _row(page)
    row.selected.setChecked(True)
    values = [c.checkState() for c in row.checkboxes.values()]
    page.mark_probe_started("m")
    assert row._probe_progress == ModelProbeProgress(0, 10)
    page.apply_probe_progress("relay", "m", ModelProbeProgress(3, 10))
    assert row._probe_progress == ModelProbeProgress(3, 10)
    assert "3/10" in row.toolTip()
    assert "3/10" in row.accessibleDescription()
    assert [c.checkState() for c in row.checkboxes.values()] == values
    assert row.selected.isChecked()
    assert not row.detect.isEnabled()
    assert _row(page, "other")._probe_progress is None
    page.apply_probe_progress("another", "m", ModelProbeProgress(8, 10))
    assert row._probe_progress == ModelProbeProgress(3, 10)
    page.apply_probe_result("relay", _result())
    assert row._probe_progress.completed == row._probe_progress.total
    assert row.detect.isEnabled()
    page.apply_probe_progress("relay", "m", ModelProbeProgress(4, 10))
    assert row._probe_progress.completed == row._probe_progress.total
    page.mark_probe_started("m")
    assert row._probe_progress == ModelProbeProgress(0, 10)
    page.apply_probe_progress("relay", "m", ModelProbeProgress(1, 10))
    page.show_probe_error("relay", "m", "timeout")
    assert row._probe_progress == ModelProbeProgress(1, 10)
    assert row.detect.isEnabled()
    assert "检测失败" in row.status_text


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_background_fills_only_completed_fraction_and_preserves_border(qtbot, palette):
    try:
        theme.set_palette(palette)
        page = _page(qtbot, "m")
        page.setStyleSheet(theme.app_stylesheet())
        page.resize(1000, 480)
        page.show()
        qtbot.waitExposed(page)
        row = _row(page)
        page.mark_probe_started("m")
        before = row.grab().toImage()
        page.apply_probe_progress("relay", "m", ModelProbeProgress(5, 10))
        qtbot.waitUntil(
            lambda: row._probe_progress_animation.state() == QAbstractAnimation.State.Stopped,
        )
        after = row.grab().toImage()
        y = before.height() - 4
        assert after.pixelColor(before.width() // 4, y) != before.pixelColor(before.width() // 4, y)
        assert after.pixelColor(before.width() * 3 // 4, y) == before.pixelColor(
            before.width() * 3 // 4, y,
        )
        assert after.pixelColor(0, 0) == before.pixelColor(0, 0)
        assert row.detect.parentWidget() is row
        assert row.selected.parentWidget() is row
    finally:
        theme.set_palette("dark")


def test_active_progress_restored_after_endpoint_switch_and_late_events_ignored(
    qtbot, tmp_path, vault_key,
):
    settings = SettingsService(ConfigurationService(JsonConfigRepository(tmp_path / "config.json")))
    credentials = CredentialService(SecretStore(vault_key, tmp_path / "vault" / "secrets.json"))
    endpoint, other = _endpoint(), _endpoint("other")
    settings.upsert_endpoint(endpoint)
    settings.upsert_endpoint(other)
    page = SettingsPage(None, settings, credentials)
    qtbot.addWidget(page)
    page._on_endpoint_saved(endpoint)
    page._request_model_probe(ModelProbeTask(endpoint, "m", "M"))
    page.apply_model_probe_progress("relay", "m", ModelProbeProgress(2, 10))
    page._on_endpoint_saved(other)
    page._request_model_probe(ModelProbeTask(other, "m", "M"))
    page.apply_model_probe_progress("other", "m", ModelProbeProgress(1, 10))
    page.apply_model_probe_progress("relay", "m", ModelProbeProgress(4, 10))
    assert _row(page.actual_models)._probe_progress == ModelProbeProgress(1, 10)
    page._on_endpoint_saved(endpoint)
    assert _row(page.actual_models)._probe_progress == ModelProbeProgress(4, 10)
    page.show_model_probe_error("relay", "m", "timeout")
    assert ("relay", "m") not in page._probe_progress
    page.apply_model_probe_progress("relay", "m", ModelProbeProgress(5, 10))
    assert ("relay", "m") not in page._probe_progress
    assert _row(page.actual_models)._probe_progress == ModelProbeProgress(4, 10)
    page._request_model_probe(ModelProbeTask(endpoint, "m", "M"))
    page.apply_model_probe_progress("relay", "m", ModelProbeProgress(9, 9))
    page.apply_model_probe("relay", _result())
    assert ("relay", "m") not in page._probe_progress
    assert _row(page.actual_models)._probe_progress == ModelProbeProgress(9, 9)


@pytest.mark.parametrize("deleted", [False, True])
async def test_worker_progress_is_delivered_on_gui_loop(qtbot, monkeypatch, deleted):
    from shiboken6 import delete

    gui_thread = threading.get_ident()
    delivered, workers = [], []
    page = QWidget()
    if not deleted:
        qtbot.addWidget(page)
    page.apply_model_probe_progress = lambda *args: delivered.append((threading.get_ident(), args))
    page.show_model_probe_error = lambda *args: None
    task = ModelProbeTask(_endpoint(), "m", "M")

    def probe(*args, on_progress, **kwargs):
        workers.append(threading.get_ident())
        on_progress(ModelProbeProgress(1, 10))
        return SimpleNamespace(alive=False, stream=SimpleNamespace(detail="failure"))

    async def run_probe(function, *args):
        if deleted:
            delete(page)
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr(model_probe, "probe_model_capabilities", probe)
    source = Path(__file__).parents[2] / "src/limbowave/app.py"
    function = next(
        node for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_probe_actual_model"
    )
    namespace = {
        "asyncio": asyncio, "Any": object, "partial": partial, "page": page,
        "credentials": SimpleNamespace(resolve=lambda ref: None), "_run_probe": run_probe,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    await namespace["_probe_actual_model"](task)
    await asyncio.sleep(0)
    assert workers and workers[0] != gui_thread
    if deleted:
        assert not delivered
    else:
        assert delivered == [(gui_thread, ("relay", "m", ModelProbeProgress(1, 10)))]


def _visible_progress_page(qtbot):
    page = _page(qtbot, "m")
    page.setStyleSheet(theme.app_stylesheet())
    page.resize(1000, 480)
    page.show()
    qtbot.waitExposed(page)
    page.mark_probe_started("m")
    return page, _row(page)


def test_request_completion_animates_through_intermediate_frames(qtbot):
    page, row = _visible_progress_page(qtbot)
    before = row.grab().toImage()
    page.apply_probe_progress("relay", "m", ModelProbeProgress(5, 10))
    animation = row._probe_progress_animation
    assert animation.state() == QAbstractAnimation.State.Running
    assert row._displayed_probe_progress == 0.0
    assert row._probe_progress == ModelProbeProgress(5, 10)
    assert row.grab().toImage() == before
    # Drive a deterministic intermediate frame and verify the actual painted boundary.
    animation.setCurrentTime(animation.duration() // 2)
    assert 0.0 < row._displayed_probe_progress < 0.5
    middle = row.grab().toImage()
    y, width = middle.height() - 4, middle.width()
    assert middle.pixelColor(width // 4, y) != before.pixelColor(width // 4, y)
    assert middle.pixelColor(int(width * .47), y) == before.pixelColor(int(width * .47), y)
    animation.setCurrentTime(animation.duration())
    assert row._displayed_probe_progress == pytest.approx(0.5)
    assert row.grab().toImage().pixelColor(int(width * .47), y) != before.pixelColor(
        int(width * .47), y,
    )


def test_progress_animation_runs_on_event_loop_and_retargets_without_jump(qtbot):
    page, row = _visible_progress_page(qtbot)
    page.apply_probe_progress("relay", "m", ModelProbeProgress(2, 10))
    animation = row._probe_progress_animation
    qtbot.waitUntil(lambda: row._displayed_probe_progress > 0, timeout=1000)
    animation.setCurrentTime(animation.duration() // 3)
    previous = row._displayed_probe_progress
    page.apply_probe_progress("relay", "m", ModelProbeProgress(4, 10))
    assert row._displayed_probe_progress == previous
    assert animation.startValue() == previous
    assert animation.endValue() == 0.4
    animation.setCurrentTime(animation.duration() // 3)
    current_time = animation.currentTime()
    page.apply_probe_progress("relay", "m", ModelProbeProgress(4, 10))
    assert animation.currentTime() == current_time
    page.apply_probe_result("relay", _result())
    assert animation.endValue() == 1.0
    qtbot.waitUntil(lambda: animation.state() == QAbstractAnimation.State.Stopped, timeout=1500)
    assert row._displayed_probe_progress == 1.0
    page.mark_probe_started("m")
    assert row._displayed_probe_progress == 0.0
    assert animation.state() == QAbstractAnimation.State.Stopped


def test_hidden_rows_settle_progress_without_replaying_old_animation(qtbot):
    page, row = _visible_progress_page(qtbot)
    page.apply_probe_progress("relay", "m", ModelProbeProgress(5, 10))
    animation = row._probe_progress_animation
    animation.setCurrentTime(animation.duration() // 3)
    page.hide()
    assert animation.state() == QAbstractAnimation.State.Stopped
    assert row._displayed_probe_progress == 0.5
    page.apply_probe_progress("relay", "m", ModelProbeProgress(7, 10))
    assert row._displayed_probe_progress == 0.7
    page.show()
    assert animation.state() == QAbstractAnimation.State.Stopped
    assert row._displayed_probe_progress == 0.7
    page.apply_probe_progress("relay", "m", ModelProbeProgress(8, 10))
    assert animation.state() == QAbstractAnimation.State.Running
    assert animation.startValue() == 0.7
    page.show_probe_error("relay", "m", "timeout")
    page.mark_probe_started("m")
    assert row._displayed_probe_progress == 0.0
    assert animation.state() == QAbstractAnimation.State.Stopped
