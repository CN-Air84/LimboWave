"""Real app callbacks synchronize markers only after a committed, in-scope change."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from limbowave.application.history_payload import HistoryEntry


def callback(name, namespace):
    source = Path(__file__).parents[2] / "src/limbowave/app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "branch", "preview"])
async def test_marker_refresh_is_scope_safe(change):
    payload = [HistoryEntry("user", "hello", "", "m1")]
    seen = []
    scope = SimpleNamespace(conversation_id="c1", branch_id="b1")

    async def read(operation):
        result = operation()
        await asyncio.sleep(0)
        if change == "branch":
            scope.branch_id = "other"
        elif change == "preview":
            namespace["preview_scope"] = ("other", "branch")
        return result

    def project(messages, *, uow_factory, branch_id):
        assert branch_id == "b1"
        return payload

    namespace = {
        "controller": scope, "preview_scope": None,
        "conversation_process": None, "HistoryEntry": HistoryEntry,
        "history_reader": SimpleNamespace(read=read),
        "history": SimpleNamespace(branch_messages=lambda bid: []),
        "_history_payload": project, "uow_factory": object(),
        "chat": SimpleNamespace(sync_compression_markers=seen.append),
    }
    callback("_load_branch_payload", namespace)
    refresh = callback("_refresh_compression_markers", namespace)
    await refresh("c1", "b1")
    assert seen == ([payload] if change is None else [])
    await refresh("another", "branch")
    assert len(seen) == (1 if change is None else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_apply_and_rollback_refresh_only_after_success(monkeypatch, success):
    from limbowave.ui import compression_widgets
    from tests.ui.test_compression_trigger_wiring import Coordinator, Panel

    previews, refreshed, finished = [], [], []

    class Signal:
        def connect(self, handler):
            self.handler = handler

    class Preview:
        def __init__(self, *args, **kwargs):
            self.apply_requested, self.rollback_requested = Signal(), Signal()
            self.retry_requested, self.finished = Signal(), Signal()
            previews.append(self)

        def setWindowFlags(self, flags):
            pass

        def finish_runtime_change(self, ok, **kwargs):
            finished.append((ok, kwargs))

    async def result(*args, **kwargs):
        return success

    async def refresh(cid, bid):
        refreshed.append((cid, bid))

    async def noop():
        pass

    async def blocking(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    coordinator = Coordinator()
    coordinator.apply_compression = result
    coordinator.rollback_compression = result
    monkeypatch.setattr(compression_widgets, "CompressionPreviewDialog", Preview)
    from limbowave.application.services.compression_trigger import CompressionTrigger

    namespace = {
        "asyncio": asyncio,
        "controller": SimpleNamespace(conversation_id="c1", coordinator=lambda: coordinator),
        "preview_scope": None, "compression_trigger": CompressionTrigger(),
        "compression": SimpleNamespace(
            estimate=result,
            create_version=lambda *args, **kwargs: SimpleNamespace(id="cmp"),
        ),
        "run_blocking": blocking, "_run_compression": result, "setup": None,
        "compression_run": SimpleNamespace(task=None, abortable=False),
        "chat": SimpleNamespace(set_status=lambda text: None),
        "FloatingPanel": Panel, "window": None,
        "Qt": SimpleNamespace(WindowType=SimpleNamespace(Widget=1)),
        "shiboken6": SimpleNamespace(isValid=lambda obj: True),
        "_refresh_usage": noop, "_refresh_compression_markers": refresh,
        "_spawn": lambda coroutine: coroutine,
    }

    async def estimate(kernel):
        return SimpleNamespace(usage=None)

    namespace["compression"].estimate = estimate
    compress = callback("_do_compress", namespace)
    await compress()
    assert not refreshed
    await previews[0].apply_requested.handler("cmp", "summary")
    await previews[0].rollback_requested.handler("b1")
    assert refreshed == ([("c1", "b1"), ("c1", "b1")] if success else [])
    assert finished == [(success, {}), (success, {"rollback": True})]


@pytest.mark.asyncio
async def test_branch_payload_uses_process_without_local_projection():
    payload = [HistoryEntry("assistant", "done", "", "m")]
    calls = []

    async def call(*args):
        calls.append(args)
        return payload

    namespace = {
        "conversation_process": SimpleNamespace(call=call), "HistoryEntry": HistoryEntry,
    }
    load = callback("_load_branch_payload", namespace)
    assert await load("branch") is payload
    assert calls == [("history_payload", (None, "branch"), "branch")]
