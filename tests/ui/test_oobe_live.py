"""OOBE reuses the actual-models editor and never silently chooses a remote model."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import get_ident
from typing import Any

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from limbowave.application.services.model_probe import (
    DiscoveredModel,
    DiscoveryResult,
    ModelProbeProgress,
    ModelProbeResult,
    StreamProbeResult,
    ThinkingProbeResult,
    ToolProbeResult,
)
from limbowave.bootstrap import AppPaths
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.ui import oobe_live
from limbowave.ui.main_window import MainWindow
from limbowave.ui.model_probe_page import ActualModelsPage, ModelProbeTask
from limbowave.ui.oobe_demo import OobeDemo


@pytest.fixture
def setup(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_key: VaultKey,
) -> Iterator[tuple[OobeDemo, oobe_live._Gate, list[Callable[[], None]]]]:
    pending: list[Callable[[], None]] = []

    class DeferredExecutor:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def submit(self, run: Callable[[], None]) -> None:
            pending.append(run)

        def shutdown(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(oobe_live, "ThreadPoolExecutor", DeferredExecutor)
    monkeypatch.setattr(
        oobe_live,
        "discover_models",
        lambda *args, **kwargs: DiscoveryResult(
            (DiscoveredModel("first", "First"), DiscoveredModel("chosen", "Chosen")),
            "ok",
        ),
    )
    window = MainWindow()
    qtbot.addWidget(window)
    page = OobeDemo(preview=False)
    window.show_full_page(page)
    gate = oobe_live._Gate(window, page, AppPaths(tmp_path, tmp_path / "logs"), vault_key)
    yield page, gate, pending
    gate._stop()


def _enter(page: OobeDemo, *, custom: bool = False) -> None:
    page.show_screen("key_custom" if custom else "key")
    page._url.setText("https://relay.example/v1")
    page._key.setText("sk-test")
    page._display_name.setText("  我的服务商  ")
    page._begin_check()


@pytest.mark.parametrize("custom", [False, True])
def test_endpoint_save_opens_shared_models_page_without_choosing_a_model(setup, custom):
    page, gate, pending = setup
    _enter(page, custom=custom)
    assert page.screen_id == "models"
    assert isinstance(page.actual_models, ActualModelsPage)
    config = gate._settings.load()
    assert config.endpoints[0].name == "我的服务商"
    assert not config.models and not config.actual_models
    assert not page._primary.isEnabled()
    page._display_name.setText("later edit")
    pending.pop(0)()
    assert page.screen_id == "models"
    assert list(page.actual_models._rows) == ["first", "chosen"]
    assert gate._settings.load().endpoints[0].name == "我的服务商"
    assert not gate._settings.load().models


def test_add_and_choose_explicit_default_model(setup, qtbot):
    page, gate, pending = setup
    _enter(page)
    pending.pop(0)()
    for row in page.actual_models._rows.values():
        qtbot.mouseClick(row.add, Qt.MouseButton.LeftButton)
    assert page._default_model.currentIndex() == -1
    assert not page._primary.isEnabled()
    page._default_model.setCurrentIndex(page._default_model.findData("chosen"))
    qtbot.mouseClick(page._primary, Qt.MouseButton.LeftButton)
    assert page.screen_id == "success"
    config = gate._settings.load()
    assert config.default_model_id == "chosen"
    assert config.models[0].bindings[0].model_id == "chosen"
    assert len(config.actual_models) == 2


def test_failed_discovery_keeps_manual_configuration_available(setup, monkeypatch, qtbot):
    page, gate, pending = setup
    monkeypatch.setattr(
        oobe_live, "discover_models", lambda *a, **kw: DiscoveryResult((), "denied", "auth")
    )
    _enter(page, custom=True)
    pending.pop(0)()
    assert page.screen_id == "models"
    assert "denied" in page.actual_models._status.text()
    page.actual_models._manual_id.setText("manual-model")
    qtbot.mouseClick(page.actual_models._manual_add, Qt.MouseButton.LeftButton)
    row = page.actual_models._rows["manual-model"]
    row.checkboxes["supports_tools"].click()
    config = gate._settings.load()
    assert config.actual_models[0].supports_tools
    assert not config.models
    page._default_model.setCurrentIndex(0)
    qtbot.mouseClick(page._primary, Qt.MouseButton.LeftButton)
    assert page.screen_id == "success"
    assert gate._settings.load().models[0].bindings[0].supports_tools


def test_leaving_models_cancels_and_ignores_old_discovery(setup):
    page, gate, pending = setup
    _enter(page)
    previous_cancel = gate._cancelled
    page._on_secondary()
    assert page.screen_id == "key"
    assert page._display_name.text() == "  我的服务商  "
    assert previous_cancel.is_set()
    _enter(page, custom=True)
    pending.pop(0)()
    assert not page.actual_models._rows
    pending.pop(0)()
    assert len(page.actual_models._rows) == 2
    assert page.actual_models.endpoint_id == "custom-relay-example"


def test_close_cancels_pending_requests_without_model_writes(setup):
    page, gate, pending = setup
    _enter(page)
    gate._stop()
    assert gate._cancelled.is_set()
    pending.pop(0)()
    assert not gate._settings.load().models
    assert not page.actual_models._rows


def _probe_result(model_id: str, *, alive: bool = True) -> ModelProbeResult:
    return ModelProbeResult(
        model_id,
        StreamProbeResult(alive, "stream ok" if alive else "stream failed"),
        ThinkingProbeResult(True, "high", "thinking ok", ("low", "high")),
        ToolProbeResult(True, True, "tools ok"),
    )


def test_probe_uses_shared_service_progress_and_persists_capabilities(setup, monkeypatch):
    page, gate, pending = setup
    _enter(page)
    pending.pop(0)()
    row = page.actual_models._rows["chosen"]
    row.add.click()
    page._default_model.setCurrentIndex(0)
    assert page._primary.isEnabled()

    def probe(endpoint, model_id, secret, *, limiter, cancelled, on_progress):
        assert secret == "sk-test"
        assert limiter is gate._limiter
        assert not cancelled.is_set()
        on_progress(ModelProbeProgress(2, 4))
        assert row._probe_progress == ModelProbeProgress(2, 4)
        return _probe_result(model_id)

    monkeypatch.setattr(oobe_live, "probe_model_capabilities", probe)
    row.detect.click()
    assert not page._primary.isEnabled()
    assert row.model_id in page.actual_models._probing
    pending.pop(0)()
    actual = gate._settings.load().actual_models[0]
    assert actual.supports_tools and actual.supports_streaming and actual.supports_thinking
    assert actual.available_thinking_levels == ("low", "high")
    assert actual.default_thinking_level == "high"
    assert row.probe_succeeded
    assert page._primary.isEnabled()
    page._primary.click()
    assert gate._settings.load().models[0].bindings[0].supports_tools


def test_probe_failure_does_not_overwrite_saved_capabilities(setup, monkeypatch):
    page, gate, pending = setup
    _enter(page)
    pending.pop(0)()
    row = page.actual_models._rows["chosen"]
    row.checkboxes["supports_tools"].click()
    before = gate._settings.load().actual_models[0]
    monkeypatch.setattr(
        oobe_live, "probe_model_capabilities", lambda *a, **kw: _probe_result("chosen", alive=False)
    )
    row.detect.click()
    pending.pop(0)()
    assert gate._settings.load().actual_models[0] == before
    assert not page.actual_models._probing
    assert "stream failed" in row.status_text


def test_probe_result_after_navigation_does_not_write_models(setup, monkeypatch):
    page, gate, pending = setup
    _enter(page)
    pending.pop(0)()
    monkeypatch.setattr(
        oobe_live, "probe_model_capabilities", lambda *a, **kw: _probe_result("chosen")
    )
    page.actual_models._rows["chosen"].detect.click()
    endpoint = page.actual_models._endpoint
    request = gate._request(endpoint, ModelProbeTask(endpoint, "chosen", "Chosen"))
    # Simulate a result already queued by a worker before the user navigates back.
    page._on_secondary()
    pending.pop(0)()
    gate._apply_result(request, _probe_result("chosen"))
    assert not gate._settings.load().actual_models
    assert not gate._settings.load().models


def test_changed_endpoint_rejects_probe_result(setup, monkeypatch):
    page, gate, pending = setup
    _enter(page)
    pending.pop(0)()
    monkeypatch.setattr(
        oobe_live, "probe_model_capabilities", lambda *a, **kw: _probe_result("chosen")
    )
    row = page.actual_models._rows["chosen"]
    row.detect.click()
    endpoint = gate._settings.load().endpoints[0]
    gate._settings.upsert_endpoint(
        endpoint.model_copy(update={"base_url": "https://changed.example"})
    )
    pending.pop(0)()
    assert not gate._settings.load().actual_models
    assert "站点配置已变化" in row.status_text


def test_older_refresh_result_does_not_replace_newer_discovery(setup):
    page, gate, pending = setup
    _enter(page)
    older = gate._discovery_sequence
    endpoint = page.actual_models._endpoint
    page.actual_models._refresh.click()
    pending.pop()()
    gate._apply_result(
        oobe_live._Request(older, gate._generation, endpoint), DiscoveryResult((), "stale")
    )
    assert list(page.actual_models._rows) == ["first", "chosen"]


def test_switching_endpoint_does_not_reuse_previous_default_selection(setup):
    page, _gate, pending = setup
    _enter(page)
    pending.pop(0)()
    page.actual_models._rows["chosen"].add.click()
    page._default_model.setCurrentIndex(0)
    page._on_secondary()
    _enter(page, custom=True)
    pending.pop(0)()
    page.actual_models._rows["chosen"].add.click()
    assert page._default_model.currentIndex() == -1
    assert not page._primary.isEnabled()


def test_real_worker_delivers_progress_and_saves_only_on_gui_thread(setup, monkeypatch, qtbot):
    page, gate, _pending = setup
    gate._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="oobe-test")
    gui_thread = get_ident()
    workers = []
    original_record = gate._settings.record_probed_model
    original_progress = page.actual_models.apply_probe_progress
    progress_threads = []

    def probe(endpoint, model_id, secret, **kwargs):
        workers.append(get_ident())
        kwargs["on_progress"](ModelProbeProgress(1, 2))
        return _probe_result(model_id)

    def record(**kwargs):
        assert get_ident() == gui_thread
        return original_record(**kwargs)

    def progress(*args):
        progress_threads.append(get_ident())
        return original_progress(*args)

    monkeypatch.setattr(oobe_live, "probe_model_capabilities", probe)
    monkeypatch.setattr(gate._settings, "record_probed_model", record)
    monkeypatch.setattr(page.actual_models, "apply_probe_progress", progress)
    _enter(page)
    qtbot.waitUntil(lambda: "chosen" in page.actual_models._rows)
    row = page.actual_models._rows["chosen"]
    row.detect.click()
    qtbot.waitUntil(lambda: row.probe_succeeded)
    assert workers and all(worker != gui_thread for worker in workers)
    assert progress_threads == [gui_thread]
    assert gate._settings.load().actual_models[0].supports_tools


def test_endpoint_save_failure_stays_on_form_and_starts_no_request(setup, monkeypatch):
    page, gate, pending = setup

    def fail(**kwargs):
        raise OSError("test disk failure")

    monkeypatch.setattr(oobe_live, "save_onboarding_endpoint", fail)
    _enter(page)
    assert page.screen_id == "key"
    assert "保存失败" in page._hint.text()
    assert page._display_name.text() == "  我的服务商  "
    assert not pending
    assert not gate._settings.load().endpoints
