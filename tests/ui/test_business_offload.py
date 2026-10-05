"""Regression tests exercise the actual app callback bodies, not proxy implementations."""
import ast
import asyncio
import threading
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from limbowave.application.background import run_blocking
from limbowave.application.services.history_reader import HistoryReader
from limbowave.application.services.tool_gateway import ToolResult


def app_callback(name, namespace):
    source = Path(__file__).parents[2] / "src/limbowave/app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("tool", ["list_directory", "search_text", "read_document", "run_command"])
async def test_tool_dispatch_never_executes_business_on_gui(qapp, tool):
    gui = threading.get_ident()
    workers, ticks = [], []

    def invoke(*args, **kwargs):
        workers.append(threading.get_ident())
        time.sleep(.15)
        return ToolResult(ok=True, data={})

    dispatch = app_callback("_dispatch_tool", {
        "shutting_down": False, "tool_gateway": SimpleNamespace(invoke=invoke),
        "run_blocking": run_blocking,
    })
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    task = asyncio.create_task(dispatch(tool, {}, "conversation", True))
    while not task.done():
        qapp.processEvents()
        await asyncio.sleep(.005)
    timer.stop()
    assert (await task)["ok"]
    assert workers and workers[0] != gui
    assert len(ticks) >= 5
    assert max((b-a for a,b in pairwise(ticks)), default=0) < .1


async def test_late_history_search_does_not_replace_new_query():
    entered, release = threading.Event(), threading.Event()
    applied = []
    reader = HistoryReader()

    def search(text):
        entered.set()
        release.wait(3)
        return []

    namespace = {
        "search_request": ("old", 1, None), "history_list_generation": 1,
        "history_reader": reader, "history": SimpleNamespace(search=search),
        "shutting_down": False, "sidebar": SimpleNamespace(show_search_results=applied.append),
    }
    search_callback = app_callback("_search_history", namespace)
    task = asyncio.create_task(search_callback())
    try:
        while not entered.is_set():
            await asyncio.sleep(.001)
        namespace["history_list_generation"] = 2
        release.set()
        await task
        assert applied == []
    finally:
        release.set()
        await reader.close()


def test_background_import_is_off_thread_and_keeps_gui_alive(qtbot, tmp_path, monkeypatch):
    from limbowave.application.services.appearance_theme_service import AppearanceThemeService
    from limbowave.application.services.preferences_service import PreferencesService
    from limbowave.ui.appearance_editor import AppearanceEditor

    service = AppearanceThemeService(tmp_path / "themes")
    editor = AppearanceEditor(PreferencesService(tmp_path / "preferences.json"), service)
    qtbot.addWidget(editor)
    gui, workers, ticks = threading.get_ident(), [], []

    def import_image(path):
        workers.append(threading.get_ident())
        time.sleep(.2)
        return "assets/test.png"

    monkeypatch.setattr(service, "import_background", import_image)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_a: ("test.jpg", ""))
    timer = QTimer(editor)
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    editor._import_background()
    assert not editor.isEnabled()
    qtbot.waitUntil(editor.isEnabled)
    assert workers == [workers[0]] and workers[0] != gui
    assert editor.draft.background.asset == "assets/test.png"
    assert len(ticks) >= 5


@pytest.mark.parametrize("invalidate", [False, True])
async def test_attachment_build_is_background_and_validates_draft(invalidate):
    from contextlib import nullcontext

    from limbowave.application.services.attachment_service import AttachmentPayload

    started = threading.Event()
    release = threading.Event()
    threads, sends, cleared = [], [], []
    loading_calls = []
    strip = SimpleNamespace(generation=1, clear=lambda: cleared.append(True))
    gui = threading.get_ident()

    def build(ids):
        threads.append(threading.get_ident())
        started.set()
        release.wait(3)
        return AttachmentPayload(attachment_ids=ids)

    async def send(text, **kwargs):
        sends.append((text, kwargs))

    namespace = {
        "send_preparing": True, "attachment_tasks": set(), "asyncio": asyncio,
        "chat": SimpleNamespace(attachments=strip, set_history_loading=lambda *a, **kw: loading_calls.append((a, kw))),
        "_display_scope": lambda: (None, None), "run_blocking": run_blocking,
        "attachments": SimpleNamespace(build=build), "_send_with_catalog_sync": send,
        "controller": SimpleNamespace(busy=False,
            coordinator=lambda: SimpleNamespace(runtime_transition=nullcontext)),
    }
    source = Path(__file__).parents[2] / "src/limbowave/app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_prepare_attachment_send")
    # Supply the enclosing closure cells as module globals, retaining the body.
    class LiftClosure(ast.NodeTransformer):
        def visit_Nonlocal(self, node):
            return ast.copy_location(ast.Global(names=node.names), node)
    function = LiftClosure().visit(function)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    task = asyncio.create_task(namespace["_prepare_attachment_send"](
        "question", ["image"], (None, None), draft_generation=1,
    ))
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        if invalidate:
            strip.generation += 1
        release.set()
        await task
    finally:
        release.set()
    assert threads == [threads[0]] and threads[0] != gui
    assert bool(sends) is not invalidate
    assert bool(cleared) is not invalidate
    assert not namespace["send_preparing"]
    assert loading_calls[0] == ((True,), {"show_overlay": False})
    assert loading_calls[-1] == ((False,), {})


def test_pending_clipboard_image_blocks_send_and_drops_old_draft(qtbot, monkeypatch):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QImage

    from limbowave.ui.chat_view import ChatView

    pending = []

    class Jobs:
        def __init__(self, parent):
            pass
        def submit(self, work, success, error):
            pending.append(success)

    monkeypatch.setattr("limbowave.ui.background_tasks.BackgroundTasks", Jobs)
    view = ChatView()
    qtbot.addWidget(view)
    mime = QMimeData()
    mime.setImageData(QImage(2, 2, QImage.Format.Format_RGB32))
    pasted, sent = [], []
    view.image_pasted.connect(pasted.append)
    view.message_submitted.connect(sent.append)
    view._input.insertFromMimeData(mime)
    view._input.setPlainText("question")
    view._on_send()
    assert sent == []
    assert view._input.toPlainText() == "question"
    view.attachments.clear()
    pending[0](b"png")
    assert pasted == []
    assert view._pending_pastes == 0
