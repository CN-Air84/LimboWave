"""正式首次引导：复用设置中的实际模型编辑器，后台检测，显式选择默认模型。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from threading import Event

from PySide6.QtCore import QEvent, QEventLoop, QObject, Signal
from PySide6.QtWidgets import QApplication

from limbowave.application.services.configuration_service import ConfigurationService
from limbowave.application.services.credential_service import CredentialService
from limbowave.application.services.endpoint_rate_limiter import EndpointRateLimiter
from limbowave.application.services.model_probe import (
    DiscoveryResult,
    ModelProbeProgress,
    ModelProbeResult,
    discover_models,
    probe_model_capabilities,
)
from limbowave.application.services.onboarding import complete_onboarding, save_onboarding_endpoint
from limbowave.application.services.settings_service import SettingsService
from limbowave.bootstrap import AppPaths
from limbowave.domain.providers import EndpointConfig
from limbowave.infrastructure.configuration.json_config_repository import JsonConfigRepository
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import VaultKey
from limbowave.ui.main_window import MainWindow
from limbowave.ui.model_probe_page import ModelCapabilityChange, ModelProbeTask
from limbowave.ui.oobe_demo import OobeDemo

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Request:
    sequence: int
    generation: int
    endpoint: EndpointConfig
    task: ModelProbeTask | None = None


def run_onboarding(window: MainWindow, paths: AppPaths, key: VaultKey) -> None:
    """配好默认模型或跳过后返回，调用方再装配内核；关闭窗口会取消后台检测。"""
    page = OobeDemo(preview=False)
    gate = _Gate(window, page, paths, key)
    window.show_full_page(page)
    gate.wait()


class _Gate(QObject):
    _work_finished = Signal(object, object)
    _probe_progress = Signal(object, object)

    def __init__(self, window: MainWindow, page: OobeDemo, paths: AppPaths, key: VaultKey) -> None:
        super().__init__(page)
        self._window = window
        self._page = page
        self._settings = SettingsService(
            ConfigurationService(JsonConfigRepository(paths.data_root / "config.json"))
        )
        self._credentials = CredentialService(
            SecretStore(key, paths.data_root / "vault" / "secrets.json")
        )
        self._loop = QEventLoop()
        self._running = False
        self._closed = False
        self._generation = 0
        self._sequence = 0
        self._discovery_sequence = 0
        self._cancelled = Event()
        self._limiter = EndpointRateLimiter()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="oobe-model-probe")
        self._work_finished.connect(self._apply_result)
        self._probe_progress.connect(self._apply_progress)
        page.check_requested.connect(self._start_check)
        page.model_selected.connect(self._complete)
        page.screen_changed.connect(self._screen_changed)
        page.finished.connect(self._stop)
        page.skipped.connect(self._stop)
        page.destroyed.connect(self._stop)
        page.actual_models.discovery_requested.connect(self._discover)
        page.actual_models.probe_requested.connect(self._probe)
        page.actual_models.manual_save_requested.connect(self._save_model)
        page.actual_models.capability_save_requested.connect(self._save_capability)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def wait(self) -> None:
        if self._closed:
            return
        self._running = True
        try:
            self._loop.exec()
        finally:
            self._running = False
            self._stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._running and watched is self._window and event.type() == QEvent.Type.Close:
            self._stop()
        return False

    def _cancel_pending(self) -> None:
        self._cancelled.set()
        self._generation += 1
        self._cancelled = Event()

    def _screen_changed(self, screen: str) -> None:
        if screen != "models":
            self._cancel_pending()

    def _stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancelled.set()
        self._limiter.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self._running:
            self._loop.quit()

    def _start_check(self, _token: int, url: str, secret: str) -> None:
        if self._closed:
            return
        provider = self._page.provider()
        try:
            endpoint = save_onboarding_endpoint(
                settings=self._settings,
                credentials=self._credentials,
                provider_id=provider.id,
                provider_name=provider.name,
                custom=provider.custom,
                url=url,
                secret=secret,
                display_name=self._page.display_name(),
            )
        except (ValueError, OSError):
            _LOG.exception("onboarding.endpoint_save_failed")
            self._page.show_screen("key_custom" if provider.custom else "key")
            self._page._hint.setText("端点保存失败，请检查配置和资料库后重试。")
            self._page._hint_wrap.show()
            return
        self._limiter.configure(self._settings.load().endpoints)
        models = tuple(self._settings.load().actual_models)
        self._page.actual_models.activate_endpoint(endpoint, actual_models=models, discover=False)
        self._page.update_default_models(models)
        self._page.show_screen("models")
        self._discover(endpoint)

    def _current_endpoint(self, endpoint: EndpointConfig) -> bool:
        return (
            not self._closed
            and self._page.screen_id == "models"
            and self._page.actual_models._endpoint == endpoint
        )

    def _require_unchanged(self, endpoint: EndpointConfig) -> None:
        current = next((e for e in self._settings.load().endpoints if e.id == endpoint.id), None)
        if current != endpoint:
            raise ValueError("站点配置已变化，请返回修改端点后重试")

    def _request(self, endpoint: EndpointConfig, task: ModelProbeTask | None = None) -> _Request:
        self._sequence += 1
        return _Request(self._sequence, self._generation, endpoint, task)

    def _submit(self, request: _Request, work: Callable[[], object]) -> None:
        cancelled = self._cancelled

        def run() -> None:
            if cancelled.is_set():
                return
            result: object
            try:
                result = work()
            except Exception:
                _LOG.exception("onboarding.model_request_failed")
                result = "检测异常，请重试或手动配置模型"
            if not cancelled.is_set():
                # 引导可能已销毁；不访问 UI 或写入配置。
                with suppress(RuntimeError):
                    self._work_finished.emit(request, result)

        self._executor.submit(run)

    def _discover(self, endpoint: EndpointConfig) -> None:
        if not self._current_endpoint(endpoint):
            return
        request = self._request(endpoint)
        self._discovery_sequence = request.sequence
        cancelled = self._cancelled
        self._page.actual_models._status.setText("正在拉取模型清单…")

        def work() -> DiscoveryResult:
            secret = self._credentials.resolve(endpoint.credential_ref)
            return discover_models(endpoint, secret, limiter=self._limiter, cancelled=cancelled)

        self._submit(request, work)

    def _probe(self, task: ModelProbeTask) -> None:
        if not self._current_endpoint(task.endpoint):
            return
        request = self._request(task.endpoint, task)
        cancelled = self._cancelled
        self._page._refresh_actions()

        def progress(value: ModelProbeProgress) -> None:
            if not cancelled.is_set():
                with suppress(RuntimeError):
                    self._probe_progress.emit(request, value)

        def work() -> ModelProbeResult:
            secret = self._credentials.resolve(task.endpoint.credential_ref)
            return probe_model_capabilities(
                task.endpoint,
                task.model_id,
                secret,
                limiter=self._limiter,
                cancelled=cancelled,
                on_progress=progress,
            )

        self._submit(request, work)

    def _accept(self, request: _Request) -> bool:
        return request.generation == self._generation and self._current_endpoint(request.endpoint)

    def _apply_progress(self, request: _Request, progress: ModelProbeProgress) -> None:
        if self._accept(request) and request.task is not None:
            self._page.actual_models.apply_probe_progress(
                request.endpoint.id,
                request.task.model_id,
                progress,
            )

    def _apply_result(self, request: _Request, result: object) -> None:
        if not self._accept(request):
            return
        actual_page = self._page.actual_models
        if request.task is None:
            if request.sequence == self._discovery_sequence:
                discovery = (
                    result
                    if isinstance(result, DiscoveryResult)
                    else DiscoveryResult((), str(result), "network")
                )
                actual_page.apply_discovery(request.endpoint.id, discovery)
            return
        task = request.task
        try:
            self._require_unchanged(task.endpoint)
            if not isinstance(result, ModelProbeResult):
                raise ValueError(str(result))
            if not result.alive:
                raise ValueError(result.stream.detail)
            config, _ = self._settings.record_probed_model(
                endpoint_id=task.endpoint.id,
                model_id=task.model_id,
                display_name=task.display_name,
                default_thinking_level=result.default_thinking_level,
                thinking_level_locked=result.thinking_level_locked,
                available_thinking_levels=result.thinking.levels,
                supports_thinking=(result.thinking.supported or None)
                if result.thinking.inconclusive
                else result.thinking.supported,
                supports_tools=result.supports_tools,
                supports_streaming=result.stream.alive,
            )
            actual_page.apply_probe_result(
                task.endpoint.id, result, config.actual_model(task.endpoint.id, task.model_id)
            )
        except (ValueError, OSError) as exc:
            actual_page.show_probe_error(task.endpoint.id, task.model_id, str(exc))
        self._refresh_saved_models()

    def _refresh_saved_models(self) -> None:
        models = tuple(self._settings.load().actual_models)
        self._page.actual_models.apply_saved_models(models)
        self._page.update_default_models(models)

    def _save_model(self, task: ModelProbeTask) -> None:
        if not self._current_endpoint(task.endpoint):
            return
        try:
            self._require_unchanged(task.endpoint)
            self._settings.record_unverified_model(
                endpoint_id=task.endpoint.id,
                model_id=task.model_id,
                display_name=task.display_name,
            )
        except (ValueError, OSError) as exc:
            self._page.actual_models.show_probe_error(task.endpoint.id, task.model_id, str(exc))
            return
        self._page.actual_models.apply_manual_save(task.endpoint.id, task.model_id)
        self._refresh_saved_models()

    def _save_capability(self, change: ModelCapabilityChange) -> None:
        if not self._current_endpoint(change.endpoint):
            return
        try:
            self._require_unchanged(change.endpoint)
            config, _ = self._settings.set_model_capability(
                endpoint_id=change.endpoint.id,
                model_id=change.model_id,
                display_name=change.display_name,
                capability=change.capability,
                supported=change.supported,
            )
        except (ValueError, OSError) as exc:
            self._page.actual_models.show_capability_save_error(change, str(exc))
            return
        actual = config.actual_model(change.endpoint.id, change.model_id)
        if actual is not None:
            self._page.actual_models.apply_capability_save(change, actual)
        self._refresh_saved_models()

    def _complete(self, model_id: str) -> None:
        endpoint = self._page.actual_models._endpoint
        if (
            endpoint is None
            or not self._current_endpoint(endpoint)
            or not self._page._can_continue()
        ):
            return
        try:
            self._require_unchanged(endpoint)
            complete_onboarding(self._settings, endpoint.id, model_id)
        except (ValueError, OSError) as exc:
            self._page.actual_models._status.setText(str(exc))
            return
        self._page.show_screen("success")
