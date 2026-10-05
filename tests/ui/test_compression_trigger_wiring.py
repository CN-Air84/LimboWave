"""Exercise the real app callbacks, including asynchronous trigger/refresh races."""
from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from limbowave.application.kernel import ContextUsage
from limbowave.application.services.compression_service import ThresholdAction, UsageReport
from limbowave.application.services.compression_trigger import CompressionTrigger


class Signal:
    def connect(self, callback):
        pass


class Preview:
    def __init__(self, *args, **kwargs):
        self.apply_requested = Signal()
        self.rollback_requested = Signal()
        self.retry_requested = Signal()
        self.finished = Signal()

    def setWindowFlags(self, flags):
        pass


class Panel:
    def __init__(self, *args, **kwargs):
        self.content_layout = SimpleNamespace(addWidget=lambda *args: None)

    def close_panel(self):
        pass

    def popup(self):
        pass


class Coordinator:
    def __init__(self):
        self.kernel = object()
        self.branch_id = "b1"
        self.conversation_id = "c1"
        self.busy = False

    async def wait_idle(self):
        pass

    @contextmanager
    def runtime_transition(self):
        self.busy = True
        try:
            yield
        finally:
            self.busy = False


@pytest.fixture
def callbacks(monkeypatch):
    from limbowave.ui import compression_widgets

    monkeypatch.setattr(compression_widgets, "CompressionPreviewDialog", Preview)
    coordinator = Coordinator()
    trigger = CompressionTrigger()
    generated, usage_updates, status_updates, usage_actions = [], [], [], []
    active = [None]

    async def estimate(*args, **kwargs):
        await asyncio.sleep(0)
        return UsageReport(ContextUsage(85, 100, 85.0), ThresholdAction.PREVIEW, "85%")

    async def run_blocking(func, *args, **kwargs):
        await asyncio.sleep(0)
        return func(*args, **kwargs)

    async def generate(version_id, kernel):
        generated.append(version_id)
        await asyncio.sleep(0)
        return True

    compression = SimpleNamespace(
        estimate=estimate,
        get_active=lambda branch_id: active[0],
        get=lambda version_id: SimpleNamespace(id=version_id, branch_id="b1"),
        create_version=lambda *args, **kwargs: SimpleNamespace(id="version", branch_id="b1"),
    )
    namespace = {
        "asyncio": asyncio,
        "controller": SimpleNamespace(
            conversation_id="c1", branch_id="b1", coordinator=lambda: coordinator,
        ),
        "preview_scope": None,
        "usage_refresh_generation": {"value": 0},
        "compression_trigger": trigger,
        "compression": compression,
        "_compression_settings": lambda: SimpleNamespace(auto_preview=True),
        "_run_compression": generate,
        "run_blocking": run_blocking,
        "chat": SimpleNamespace(
            set_status=status_updates.append,
            set_context_usage=lambda *args: usage_updates.append(args),
            set_usage_action=usage_actions.append,
        ),
        "setup": None,
        "compression_run": SimpleNamespace(task=None, abortable=False),
        "window": object(),
        "FloatingPanel": Panel,
        "Qt": SimpleNamespace(WindowType=SimpleNamespace(Widget=1)),
    }
    source = Path(__file__).parents[2] / "src/limbowave/app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_do_compress", "_refresh_usage", "_retry_compress", "_abort_compression"}
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in names
    ]
    assert len(functions) == len(names)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return SimpleNamespace(
        namespace=namespace, trigger=trigger, generated=generated, active=active,
        coordinator=coordinator, usage_updates=usage_updates, compression=compression,
        usage_actions=usage_actions,
    )


async def test_manual_compression_then_reply_refresh_does_not_generate_again(callbacks):
    ns = callbacks.namespace
    await ns["_do_compress"]()
    for _ in range(3):
        await ns["_refresh_usage"]()
    assert callbacks.generated == ["version"]
    assert not callbacks.trigger.busy
    assert not callbacks.coordinator.busy


async def test_simultaneous_usage_refreshes_generate_one_preview(callbacks):
    await asyncio.gather(*(callbacks.namespace["_refresh_usage"]() for _ in range(10)))
    assert callbacks.generated == ["version"]
    assert not callbacks.trigger.busy


async def test_restored_accepted_version_does_not_auto_generate(callbacks):
    callbacks.active[0] = SimpleNamespace(id="accepted")
    await callbacks.namespace["_refresh_usage"]()
    assert callbacks.generated == []
    assert callbacks.usage_updates  # Real occupancy is still displayed.
    await callbacks.namespace["_do_compress"]()
    assert callbacks.generated == ["version"]  # Explicit compression remains possible.


async def test_failed_generation_releases_trigger_and_allows_manual_retry(callbacks):
    ns = callbacks.namespace
    original = ns["_run_compression"]

    async def fail(*args):
        raise RuntimeError("model failed")

    ns["_run_compression"] = fail
    with pytest.raises(RuntimeError, match="model failed"):
        await ns["_do_compress"]()
    assert not callbacks.trigger.busy and not callbacks.coordinator.busy
    await ns["_refresh_usage"]()
    assert callbacks.generated == []
    ns["_run_compression"] = original
    await ns["_retry_compress"]("version")
    assert callbacks.generated == ["version"]


async def test_manual_stop_cancels_running_compression_and_releases_trigger(callbacks):
    ns = callbacks.namespace
    entered = asyncio.Event()

    async def wait(*args):
        ns["compression_run"].abortable = True
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            ns["compression_run"].abortable = False

    ns["_run_compression"] = wait
    task = asyncio.create_task(ns["_do_compress"]())
    await asyncio.wait_for(entered.wait(), 2)
    assert ns["compression_run"].task is task
    assert ns["_abort_compression"]() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not callbacks.trigger.busy and not callbacks.coordinator.busy
    assert ns["compression_run"].task is None
    assert ns["_abort_compression"]() is False


async def test_cancelled_generation_releases_trigger_without_auto_retry(callbacks):
    ns = callbacks.namespace
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    ns["_run_compression"] = wait
    task = asyncio.create_task(ns["_do_compress"]())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not callbacks.trigger.busy and not callbacks.coordinator.busy
    await ns["_refresh_usage"]()
    assert callbacks.generated == []


async def test_late_duplicate_retry_read_cannot_start_second_job(callbacks):
    ns = callbacks.namespace
    entered, finish_first, finish_stale_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ns["run_blocking"]
    reads = 0

    async def run_blocking(func, *args, **kwargs):
        nonlocal reads
        if func is callbacks.compression.get:
            reads += 1
            if reads == 2:
                await finish_stale_read.wait()
        return await original(func, *args, **kwargs)

    async def generate(version_id, kernel):
        callbacks.generated.append(version_id)
        entered.set()
        if len(callbacks.generated) == 1:
            await finish_first.wait()
        return True

    ns["run_blocking"], ns["_run_compression"] = run_blocking, generate
    first = asyncio.create_task(ns["_retry_compress"]("version"))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(ns["_retry_compress"]("version"))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        finish_first.set()
        await asyncio.wait_for(first, 2)
        finish_stale_read.set()
        await asyncio.wait_for(second, 2)
        assert callbacks.generated == ["version"]
    finally:
        finish_first.set()
        finish_stale_read.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_usage_refresh_abandons_update_if_history_preview_opens_during_read(callbacks):
    ns = callbacks.namespace
    original = ns["run_blocking"]

    async def open_preview_during_read(func, *args, **kwargs):
        if func is callbacks.compression.get_active:
            ns["preview_scope"] = ("other-conversation", "other-branch")
        return await original(func, *args, **kwargs)

    ns["run_blocking"] = open_preview_during_read
    await ns["_refresh_usage"]()
    assert callbacks.usage_updates == []
    assert callbacks.generated == []


@pytest.mark.parametrize("late_phase", ["estimate", "active_read"])
async def test_old_block_usage_cannot_overwrite_post_apply_refresh(callbacks, late_phase):
    ns = callbacks.namespace
    old_read_started, release_old_read = asyncio.Event(), asyncio.Event()
    reads = 0
    active_reads = 0
    original_read = ns["run_blocking"]

    async def run_blocking(func, *args, **kwargs):
        nonlocal active_reads
        if func is callbacks.compression.get_active:
            active_reads += 1
            if active_reads == 1 and late_phase == "active_read":
                old_read_started.set()
                await release_old_read.wait()
        return await original_read(func, *args, **kwargs)

    async def estimate(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            if late_phase == "estimate":
                old_read_started.set()
                await release_old_read.wait()
            return UsageReport(ContextUsage(95, 100, 95.0), ThresholdAction.BLOCK, "95%")
        return UsageReport(None, ThresholdAction.NONE, "unknown after applying summary")

    callbacks.compression.estimate = estimate
    ns["run_blocking"] = run_blocking
    old = asyncio.create_task(ns["_refresh_usage"]())
    await asyncio.wait_for(old_read_started.wait(), 2)
    try:
        callbacks.active[0] = SimpleNamespace(id="newly-applied")
        await ns["_refresh_usage"]()
        release_old_read.set()
        await asyncio.wait_for(old, 2)
        assert callbacks.usage_updates[-1][0] is None
        assert callbacks.usage_actions[-1] == "none"
        assert callbacks.generated == []
    finally:
        release_old_read.set()
        await asyncio.gather(old, return_exceptions=True)
