"""进程接线：标准 logging、异常钩子、降级与清理。"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from limbowave.infrastructure.diagnostics import DiagnosticRuntime, LogConfig, LogManager, LogReader


def test_namespace_level_changes_and_restore(tmp_path: Path) -> None:
    namespace = logging.getLogger("limbowave")
    root = logging.getLogger()
    previous = (namespace.level, tuple(namespace.handlers), root.level, tuple(root.handlers))
    with DiagnosticRuntime(LogConfig(tmp_path, disk_bytes_per_second=0)) as runtime:
        assert runtime.start() is runtime
        logger = logging.getLogger("limbowave.testing.integration")
        logger.debug("disabled")
        logger.info("visible")
        runtime.set_level("debug")
        logger.debug("enabled")
        assert runtime.manager.flush()
        assert [entry.message for entry in runtime.manager.recent()] == ["visible", "enabled"]
        assert namespace.handlers.count(runtime.manager.handler) == 1
    assert (
        namespace.level,
        tuple(namespace.handlers),
        root.level,
        tuple(root.handlers),
    ) == previous
    assert runtime.manager.status().state == "closed"


def test_runtime_is_process_unique(tmp_path: Path) -> None:
    with DiagnosticRuntime(LogConfig(tmp_path / "one")):
        other = DiagnosticRuntime(LogConfig(tmp_path / "two"))
        with pytest.raises(RuntimeError, match="already installed"):
            other.start()
        assert not other.manager.config.directory.exists()


def test_memory_only_manager_never_creates_files(tmp_path: Path) -> None:
    target = tmp_path / "absent"
    with LogManager(LogConfig(target, file_enabled=False, disk_bytes_per_second=1)) as manager:
        manager.get_logger("test").error("retained")
        assert manager.flush()
        assert manager.status().written == 0
        assert manager.status().file_skipped == 1
        assert manager.status().file_dropped == manager.status().rate_limited == 0
        assert manager.recent()[0].message == "retained"
    assert not target.exists()


def test_bad_directory_falls_back_to_visible_memory_mode(tmp_path: Path, capsys) -> None:
    target = tmp_path / "file"
    target.write_text("keep", encoding="utf-8")
    with DiagnosticRuntime(LogConfig(target)) as runtime:
        assert not runtime.manager.config.file_enabled
        assert runtime.fallback_reason
        logging.getLogger("limbowave.test").warning("still captured")
        assert runtime.manager.flush()
        assert runtime.manager.recent()[-1].message == "still captured"
    assert "仅内存" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep"


def test_occupied_target_uses_process_specific_file_without_touching_owner(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("limbowave.infrastructure.diagnostics.runtime.os.getpid", lambda: 4321)
    with LogManager(LogConfig(tmp_path)) as owner:
        with DiagnosticRuntime(LogConfig(tmp_path)) as runtime:
            assert runtime.manager.config.file_enabled
            assert runtime.manager.config.name == "diagnostics-4321"
            assert runtime.fallback_reason is None
            runtime.manager.get_logger("secondary").info("secondary message")
        owner.get_logger("owner").info("file message")
        assert owner.flush()
        assert [entry.message for entry in LogReader(owner.config).iter_entries()] == [
            "file message"
        ]
    secondary = LogReader(LogConfig(tmp_path, name="diagnostics-4321"))
    assert [entry.message for entry in secondary.iter_entries()] == [
        "diagnostics.target_in_use",
        "secondary message",
    ]


def test_occupied_target_with_unavailable_alternate_falls_back_to_memory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("limbowave.infrastructure.diagnostics.runtime.os.getpid", lambda: 4321)
    with (
        LogManager(LogConfig(tmp_path)),
        LogManager(LogConfig(tmp_path, name="diagnostics-4321")),
        DiagnosticRuntime(LogConfig(tmp_path)) as runtime,
    ):
        assert not runtime.manager.config.file_enabled
        assert runtime.fallback_reason


def test_exception_hooks_chain_restore_and_do_not_keep_objects(tmp_path: Path, monkeypatch) -> None:
    called = []
    previous = {}
    for owner, name in ((sys, "excepthook"), (threading, "excepthook"), (sys, "unraisablehook")):

        def handler(*args, _name=name):
            called.append(_name)

        monkeypatch.setattr(owner, name, handler)
        previous[name] = handler
    error = ValueError("password=do-not-log")
    with DiagnosticRuntime(LogConfig(tmp_path)) as runtime:
        sys.excepthook(ValueError, error, None)
        threading.excepthook(
            SimpleNamespace(
                exc_type=ValueError,
                exc_value=error,
                exc_traceback=None,
                thread=SimpleNamespace(name="worker"),
            )
        )
        sys.unraisablehook(
            SimpleNamespace(
                exc_type=ValueError,
                exc_value=error,
                exc_traceback=None,
                object="PRIVATE_OBJECT_MUST_NOT_BE_RECORDED",
                err_msg="private object repr",
            )
        )
        assert runtime.manager.flush()
        data = runtime.manager.recent()
        assert [entry.message for entry in data] == [
            "python.unhandled",
            "thread.unhandled",
            "python.unraisable",
        ]
        assert "do-not-log" not in repr(data)
        assert "PRIVATE_OBJECT" not in repr(data)
    assert called == ["excepthook", "excepthook", "unraisablehook"]
    # sys 和 threading 的同名属性各自恢复，不相互覆盖。
    assert sys.excepthook is not threading.excepthook
    assert sys.unraisablehook is previous["unraisablehook"]


def test_warnings_preserve_original_output(tmp_path: Path, monkeypatch) -> None:
    observed = []

    def original(*args):
        observed.append(args)

    monkeypatch.setattr(warnings, "showwarning", original)
    with DiagnosticRuntime(LogConfig(tmp_path)) as runtime:
        warnings.showwarning("password=redact-this", UserWarning, "sample.py", 9)
        assert runtime.manager.flush()
        entry = runtime.manager.recent()[0]
        assert "redact-this" not in entry.message
        assert entry.context["warning_line"] == 9
    assert warnings.showwarning is original
    assert len(observed) == 1


def test_asyncio_hook_preserves_existing_handler_even_after_loop_close(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    observed = []

    def original(_loop, context):
        observed.append(context)

    loop.set_exception_handler(original)
    with DiagnosticRuntime(LogConfig(tmp_path)) as runtime:
        runtime.attach_loop(loop)
        installed = loop.get_exception_handler()
        runtime.attach_loop(loop)
        assert loop.get_exception_handler() is installed
        loop.call_exception_handler(
            {
                "message": "PRIVATE_CALLBACK_ARGUMENTS",
                "exception": RuntimeError("failure"),
                "task": "PRIVATE_TASK_REPR",
            }
        )
        assert runtime.manager.flush()
        entry = runtime.manager.recent()[0]
        assert entry.message == "asyncio.unhandled"
        assert "PRIVATE" not in repr(entry)
        loop.close()
    assert loop.get_exception_handler() is original
    assert len(observed) == 1


def test_newer_hook_is_not_overwritten_during_restore(tmp_path: Path, monkeypatch) -> None:
    previous = sys.excepthook

    def replacement(*args):
        pass

    monkeypatch.setattr(sys, "excepthook", previous)
    with DiagnosticRuntime(LogConfig(tmp_path)):
        sys.excepthook = replacement
    assert sys.excepthook is replacement


def test_exception_in_runtime_body_is_written_and_writer_closed(tmp_path: Path) -> None:
    runtime = DiagnosticRuntime(LogConfig(tmp_path))
    with pytest.raises(ValueError, match="body failed"), runtime:
        raise ValueError("body failed")
    assert runtime.manager.status().state == "closed"
    records = list(LogReader(runtime.manager.config).iter_entries())
    assert records[-1].message == "application.unhandled"
    assert records[-1].exception_type == "ValueError"


def test_close_timeout_is_reported_without_claiming_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    runtime = DiagnosticRuntime(LogConfig(tmp_path)).start()
    original = runtime.manager.close
    monkeypatch.setattr(runtime.manager, "close", lambda timeout: False)
    try:
        assert not runtime.close(timeout=0)
        assert "关闭超时" in capsys.readouterr().err
    finally:
        monkeypatch.setattr(runtime.manager, "close", original)
        assert runtime.close()
