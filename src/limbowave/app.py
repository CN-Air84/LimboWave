"""GUI 进程入口：建立 Qt 与 asyncio 共用的事件循环，装配主窗口与 Agent 内核。

``--smoke`` 用于自动化冒烟：启动事件循环、建窗口、正常退出，供 bootstrap 验收使用。
冒烟路径不启动内核（内核需要 node/Pi 与模型配置），只验证窗口与事件循环。

内核装配（Phase 1A）：
- 由应用权威配置驱动（站点、逻辑模型、路由、密钥引用），见 ``composition.build_kernel``。
- 未配置或 Pi 不可用时，应用以"无内核"模式启动，输入区禁用并提示。
- 权限裁决：弹一个非阻塞确认框，默认拒绝（合同 GATE-04）。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtWidgets import QApplication, QMessageBox

# qasync 提供 QEventLoop；延迟导入以便 --smoke 在无显示环境下也能部分工作
from qasync import QEventLoop

from limbowave import __version__
from limbowave.application.kernel import AgentKernel
from limbowave.application.services.session_controller import ChatEvent, SessionController
from limbowave.bootstrap import APP_DISPLAY_NAME, APP_NAME, AppContext, AppPaths, create_context
from limbowave.ui.main_window import MainWindow

SMOKE_EXIT_MS = 400


def build_application(argv: Sequence[str]) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing

    app = QApplication(list(argv))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    return app


def _build_kernel(paths: AppPaths) -> AgentKernel | None:
    """从权威配置构建内核（Phase 1A）。未配置或不可用则返回 None（优雅降级）。

    不再依赖 LIMBOWAVE_PROVIDER / LIMBOWAVE_MODEL 环境变量——那是 Phase 0B 的临时接缝。
    现在由应用权威配置驱动：站点、逻辑模型、路由、密钥引用。
    """
    from limbowave.composition import build_kernel

    return build_kernel(paths)


def _make_permission_handler(
    window: MainWindow,
) -> Callable[[str, str], Awaitable[bool]]:
    """把权限裁决接到一个非阻塞确认框。"""

    async def confirm(title: str, detail: str) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            f"权限请求：{title}",
            detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            window,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)

        def _on_finished(result: int) -> None:
            if not future.done():
                future.set_result(result == QMessageBox.StandardButton.Yes)

        box.finished.connect(_on_finished)
        box.open()
        return await future

    return confirm


def _wire(window: MainWindow, paths: AppPaths) -> SessionController:
    """装配内核 → 控制器 → 视图。返回控制器供生命周期管理。"""
    controller = SessionController(_build_kernel(paths))
    chat = window.chat

    # 用户意图 → 控制器（调度到事件循环）
    window.command_requested.connect(lambda text: asyncio.ensure_future(controller.send(text)))
    window.stop_requested.connect(lambda: asyncio.ensure_future(controller.abort()))

    # 权限裁决 → 确认框
    controller.set_permission_handler(_make_permission_handler(window))

    # 控制器事件 → 视图
    def _on_event(event: ChatEvent) -> None:
        kind = event.kind
        data = event.data
        if kind == "user":
            chat.add_user_message(str(data.get("text", "")))
            chat.set_busy(True)
            chat.set_status("生成中…")
        elif kind == "assistant_start":
            chat.begin_assistant()
        elif kind == "assistant_delta":
            chat.append_assistant_delta(str(data.get("text", "")))
        elif kind == "assistant_end":
            chat.end_assistant(str(data.get("text", "")))
        elif kind == "tool":
            phase = "运行" if data.get("phase") == "start" else "完成"
            chat.set_status(f"工具 {data.get('name')} {phase}")
        elif kind == "settled":
            chat.set_busy(False)
            chat.set_status("就绪")
        elif kind == "error":
            chat.add_error(str(data.get("message", "未知错误")))
            chat.set_busy(False)
            chat.set_status("出错")

    controller.subscribe(_on_event)
    return controller


def _report(context: AppContext, window: MainWindow) -> None:
    print(f"[smoke] {APP_DISPLAY_NAME} {__version__}")
    print(f"[smoke] python={context.python_version} frozen={context.frozen}")
    print(f"[smoke] data_root={context.paths.data_root}")
    print(f"[smoke] log_root={context.paths.log_root}")
    print(f"[smoke] qt={qVersion()}")
    print(f"[smoke] window_title={window.windowTitle()}")
    print(f"[smoke] window_visible={window.isVisible()} win_id={int(window.winId())}")
    print("[smoke] ok")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    smoke = "--smoke" in args
    if smoke:
        args.remove("--smoke")

    app = build_application(args)
    context = create_context()

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # 主窗口不接收业务上下文：窗口只展示与发命令，装配由上层负责。
    window = MainWindow()
    window.show()

    if smoke:
        # 冒烟只验证窗口与事件循环，不启动内核
        window.chat.set_available(False, "冒烟模式：内核未启动")
        _report(context, window)
        QTimer.singleShot(SMOKE_EXIT_MS, loop.stop)
        with loop:
            loop.run_forever()
        return 0

    controller = _wire(window, context.paths)

    async def _startup() -> None:
        await controller.start()
        if controller.available:
            state = await controller.state()
            model = state.model_id if state else None
            window.chat.set_available(True)
            window.chat.set_status(f"就绪 · {model or '已连接'}")
        else:
            window.chat.set_available(
                False, "未配置模型：设置 LIMBOWAVE_PROVIDER 与 LIMBOWAVE_MODEL 后重启"
            )

    with loop:
        start_task = loop.create_task(_startup())
        app.aboutToQuit.connect(loop.stop)
        loop.run_forever()
        # 窗口关闭后，优雅关闭内核（stdin EOF → Pi 退出码 0）
        if not start_task.done():
            start_task.cancel()
        loop.run_until_complete(controller.shutdown())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
