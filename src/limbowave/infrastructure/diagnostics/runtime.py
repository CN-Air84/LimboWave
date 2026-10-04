"""GUI 进程的日志接线；显式安装/恢复钩子，不导入 Qt、不重定向标准流。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import warnings
from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Any, TextIO

from limbowave.infrastructure.diagnostics.manager import LogManager
from limbowave.infrastructure.diagnostics.models import LogConfig, LogLevel
from limbowave.infrastructure.diagnostics.sanitize import safe_text
from limbowave.infrastructure.diagnostics.storage import LogTargetInUseError

LoopHandler = Callable[[asyncio.AbstractEventLoop, dict[str, Any]], object]
_ACTIVE: DiagnosticRuntime | None = None
_INSTALL_LOCK = threading.Lock()


def emergency_notice(message: str) -> None:
    """最后的可见提示；不通过 logging，防止写者故障递归。"""
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"[diagnostics] {safe_text(message)}\n")
    except Exception:
        pass


class DiagnosticRuntime:
    def __init__(self, config: LogConfig) -> None:
        self.manager = LogManager(config)
        self.fallback_reason: str | None = None
        self._installed = False
        self._hooks: list[tuple[Any, str, Any, Any]] = []
        self._loops: list[tuple[asyncio.AbstractEventLoop, LoopHandler | None, LoopHandler]] = []
        self._namespace = logging.getLogger("limbowave")
        self._old_level = self._namespace.level
        self._installed_level = int(self.manager.level)

    def start(self) -> DiagnosticRuntime:
        global _ACTIVE
        if self._installed:
            return self
        with _INSTALL_LOCK:
            if _ACTIVE is not None:
                raise RuntimeError("Diagnostic runtime is already installed in this process")
            _ACTIVE = self
        try:
            try:
                self.manager.start()
            except LogTargetInUseError:
                self.manager.close()
                suffix = f"-{os.getpid()}"
                alternate_name = f"{self.manager.config.name[: 64 - len(suffix)]}{suffix}"
                self.manager = LogManager(
                    replace(self.manager.config, name=alternate_name)
                )
                try:
                    self.manager.start()
                except (OSError, LogTargetInUseError) as exc:
                    self._fall_back_to_memory(exc)
                else:
                    self.manager.get_logger("limbowave.diagnostics").warning(
                        "diagnostics.target_in_use",
                        extra={"log_name": alternate_name},
                    )
            except OSError as exc:
                self._fall_back_to_memory(exc)
            self._old_level = self._namespace.level
            self._namespace.addHandler(self.manager.handler)
            self.set_level(self.manager.level)
            self._install_hooks()
            self._installed = True
            if self.fallback_reason:
                self.manager.get_logger("limbowave.diagnostics").error(
                    "diagnostics.memory_only", extra={"reason": self.fallback_reason}
                )
            return self
        except BaseException:
            self.close()
            raise

    def _fall_back_to_memory(self, exc: OSError | LogTargetInUseError) -> None:
        """仅在主目标和进程专属目标都不可用时关闭文件输出。"""
        self.fallback_reason = f"{type(exc).__name__}: {safe_text(str(exc), 256)}"
        self.manager.close()
        self.manager = LogManager(replace(self.manager.config, file_enabled=False))
        self.manager.start()
        emergency_notice("日志文件不可用，已降级为仅内存记录：" + self.fallback_reason)

    def __enter__(self) -> DiagnosticRuntime:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if isinstance(exc, Exception):
                self.manager.get_logger("limbowave.app").critical(
                    "application.unhandled", exc_info=(type(exc), exc, tb)
                )
        finally:
            self.close()

    def set_level(self, level: LogLevel | str | int) -> None:
        self.manager.set_level(level)
        self._installed_level = int(self.manager.level)
        self._namespace.setLevel(self._installed_level)

    def _hook(self, owner: Any, name: str, handler: Any) -> None:
        previous = getattr(owner, name)
        self._hooks.append((owner, name, previous, handler))
        setattr(owner, name, handler)

    def _install_hooks(self) -> None:
        log = self.manager.get_logger("limbowave.diagnostics.python")
        previous_sys = sys.excepthook
        previous_thread = threading.excepthook
        previous_unraisable = sys.unraisablehook
        previous_warning = warnings.showwarning

        def sys_exception(
            kind: type[BaseException], value: BaseException, tb: TracebackType | None
        ) -> None:
            if not issubclass(kind, (KeyboardInterrupt, SystemExit)):
                log.critical("python.unhandled", exc_info=(kind, value, tb))
            previous_sys(kind, value, tb)

        def thread_exception(args: Any) -> None:
            if args.exc_type is not SystemExit:
                log.error(
                    "thread.unhandled",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                    extra={"origin_thread": args.thread.name if args.thread is not None else ""},
                )
            previous_thread(args)

        def unraisable(args: Any) -> None:
            log.error(
                "python.unraisable", exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
            )
            # 不读取 args.object：repr 可能包含业务内容或重新抛异常。
            previous_unraisable(args)

        def show_warning(
            message: Warning | str,
            category: type[Warning],
            filename: str,
            lineno: int,
            file: TextIO | None = None,
            line: str | None = None,
        ) -> None:
            log.warning(
                "%s",
                message,
                extra={
                    "warning_category": category.__name__,
                    "warning_file": filename,
                    "warning_line": lineno,
                },
            )
            previous_warning(message, category, filename, lineno, file, line)

        self._hook(sys, "excepthook", sys_exception)
        self._hook(threading, "excepthook", thread_exception)
        self._hook(sys, "unraisablehook", unraisable)
        self._hook(warnings, "showwarning", show_warning)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._installed:
            raise RuntimeError("Start diagnostics before attaching the event loop")
        if any(item[0] is loop for item in self._loops):
            return
        previous = loop.get_exception_handler()
        log = self.manager.get_logger("limbowave.diagnostics.asyncio")

        def on_exception(source: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            error = context.get("exception")
            # context.message / handle / task 的 repr 可能包含回调参数（甚至用户输入）。
            log.error(
                "asyncio.unhandled",
                exc_info=error if isinstance(error, BaseException) else None,
                extra={"has_task": "task" in context or "future" in context},
            )
            if previous is not None:
                previous(source, context)
            else:
                source.default_exception_handler(context)

        loop.set_exception_handler(on_exception)
        self._loops.append((loop, previous, on_exception))

    def close(self, timeout: float = 5.0) -> bool:
        global _ACTIVE
        for loop, previous, installed in reversed(self._loops):
            if loop.get_exception_handler() is installed:
                loop.set_exception_handler(previous)
        self._loops.clear()
        for owner, name, previous, installed in reversed(self._hooks):
            if getattr(owner, name) is installed:
                setattr(owner, name, previous)
        self._hooks.clear()
        self._namespace.removeHandler(self.manager.handler)
        if self._namespace.level == self._installed_level:
            self._namespace.setLevel(self._old_level)
        self._installed = False
        with _INSTALL_LOCK:
            if _ACTIVE is self:
                _ACTIVE = None
        stopped = self.manager.close(timeout)
        if not stopped:
            emergency_notice("日志写者关闭超时，仍有未完成的输出。")
        elif self.manager.status().last_file_error:
            emergency_notice("日志写入发生故障，请检查日志目录权限及可用空间。")
        return stopped
