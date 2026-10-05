"""Runtime-managed preview cannot declare success before the coordinator commits."""

import pytest

from limbowave.application.services.compression_service import CompressionService
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.ui.compression_widgets import CompressionPreviewDialog
from tests.ui.test_compression_widgets import _make_preview_version
from tests.unit.test_compression_service import _seed


@pytest.fixture
def service():
    factory = in_memory_uow_factory(InMemoryStore())
    _seed(factory)
    return CompressionService(factory)


def test_managed_accept_only_emits_request_and_waits_for_runtime(qtbot, service):
    version_id = _make_preview_version(service)
    dialog = CompressionPreviewDialog(service, version_id, runtime_managed=True)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)
    requests, accepted = [], []
    dialog.apply_requested.connect(lambda vid, text: requests.append((vid, text)))
    dialog.accepted.connect(accepted.append)
    dialog._summary.setPlainText("user edited summary")
    dialog._on_accept()
    assert requests == [(version_id, "user edited summary")]
    assert not dialog.isEnabled() and not accepted
    assert service.get_active("b1") is None
    assert service.get(version_id).edited_summary is None
    dialog.finish_runtime_change(False)
    assert dialog.isEnabled() and not accepted
    assert service.get_active("b1") is None


def test_managed_rollback_cannot_clear_active_before_runtime(qtbot, service):
    version_id = _make_preview_version(service)
    service.accept(version_id)
    dialog = CompressionPreviewDialog(service, version_id, runtime_managed=True)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog._versions.count() > 0)
    requested, rolled = [], []
    dialog.rollback_requested.connect(requested.append)
    dialog.rolled_back.connect(rolled.append)
    dialog._on_rollback()
    assert requested == ["b1"] and not rolled
    assert service.get_active("b1").id == version_id
    dialog.finish_runtime_change(False, rollback=True)
    assert service.get_active("b1").id == version_id
    assert not rolled
