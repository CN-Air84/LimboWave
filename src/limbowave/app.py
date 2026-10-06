"""GUI 进程入口：建立 Qt 与 asyncio 共用的事件循环，装配主窗口与 Agent 内核。

``--smoke`` 用于自动化冒烟：启动事件循环、建窗口、正常退出，供 bootstrap 验收使用。
冒烟路径不启动内核（内核需要 node/Pi 与模型配置），只验证窗口与事件循环。

内核装配（Phase 1A）：
- 由应用权威配置驱动（站点、逻辑模型、路由、密钥引用），见 ``composition.build_kernel``。
- 未配置或 Pi 不可用时，应用以"无内核"模式启动，输入区禁用并提示。
- 权限裁决：弹一个非阻塞确认框，默认拒绝（合同 GATE-04）。

持久化与加密（ADR-0002）：
- 启动时解锁资料库（首次运行则设置主密码）；权威数据落 SQLite，敏感字段逐值加密。
- 取消解锁则进入降级模式：内存仓库、无内核，本次会话不持久化，界面明示。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

# qasync 提供 QEventLoop；延迟导入以便 --smoke 在无显示环境下也能部分工作
from qasync import QEventLoop

from limbowave import __version__
from limbowave.application.background import run_blocking
from limbowave.application.events import FIRST_RESPONSE_COMPLETED
from limbowave.application.history_view_payload import HistoryViewPayload, load_history_view
from limbowave.application.kernel import AgentKernel, KernelEvent, KernelSetup
from limbowave.application.services.history_reader import HistoryReader
from limbowave.application.services.run_coordinator import (
    DEFAULT_CONVERSATION_TITLE,
    RunContext,
)
from limbowave.application.services.session_controller import ChatEvent, SessionController
from limbowave.application.services.thinking_trial import ThinkingTrial
from limbowave.bootstrap import APP_DISPLAY_NAME, APP_NAME, AppContext, AppPaths, create_context
from limbowave.domain.conversation import Message
from limbowave.domain.files import FileKind
from limbowave.domain.redaction import redact_text
from limbowave.infrastructure.crypto.secret_store import migrate_legacy_secrets
from limbowave.infrastructure.crypto.vault import InvalidPassword, Vault, VaultKey
from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
from limbowave.infrastructure.diagnostics import LogConfig, LogLevel
from limbowave.infrastructure.diagnostics.runtime import DiagnosticRuntime
from limbowave.infrastructure.memory_repositories import in_memory_uow_factory
from limbowave.infrastructure.tools.ipc_server import IPC_ENV, RATE_LIMIT_IPC_ENV, ToolIpcServer
from limbowave.ui import markdown_render
from limbowave.ui.chat_view import HISTORY_PAGE, HistoryEntry
from limbowave.ui.floating import (
    FloatingPanel,
    ask_alert,
    ask_confirm,
    ask_prompt,
)
from limbowave.ui.login_page import LoginPage
from limbowave.ui.main_window import MainWindow
from limbowave.ui.model_selector import ModelSite
from limbowave.ui.session_toolbar import RouteCandidate

if TYPE_CHECKING:
    from limbowave.application.repositories import UnitOfWorkFactory
    from limbowave.application.services.data_reset_service import DataResetService, ResetScope
    from limbowave.application.services.tool_gateway import ToolGateway
    from limbowave.domain.permissions import GrantScope, ToolRequest
    from limbowave.ui.data_reset_panel import DataResetPanel
    from limbowave.ui.settings_panel import SettingsPage
    from limbowave.web.server import WebServer

SMOKE_EXIT_MS = 400
_LOG = logging.getLogger(__name__)

# 换行常量：多行文本拼接用它，避免转义在不同工具链里被吃掉
NL = chr(10)


def build_application(argv: Sequence[str]) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        from limbowave.ui.smooth_scroll import install_smooth_scrolling

        install_smooth_scrolling(existing)
        return existing

    app = QApplication(list(argv))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)

    # 全局主题：设计令牌集中在 ui.theme，视图只声明 objectName / 动态属性。
    # 具体调色板与字号在这里先按默认装好，main() 读到偏好后会再设一次。
    from limbowave.ui import theme
    from limbowave.ui.checkbox_style import CheckBoxStyle
    from limbowave.ui.smooth_scroll import install_smooth_scrolling

    # 代理样式必须先于样式表装上：样式表样式会包住当时的应用样式。
    app.setStyle(CheckBoxStyle())
    app.setStyleSheet(theme.app_stylesheet())
    install_smooth_scrolling(app)
    return app


def _build_kernel(
    paths: AppPaths,
    key: VaultKey | None,
    *,
    extra_env: dict[str, str] | None = None,
) -> KernelSetup | None:
    """从权威配置构建内核（Phase 1A）。未配置或不可用则返回 None（优雅降级）。

    返回装配结果而非裸内核：路由决策要一并交给上层，供请求意图快照记录。
    """
    from limbowave.composition import build_kernel

    return build_kernel(paths, key, extra_env=extra_env)


def _run_panel_until_closed(panel: FloatingPanel) -> None:
    """嵌套 Qt 事件循环里等悬浮窗关闭（**启动同步路径专用**）。

    启动解锁发生在主 Qt 循环启动**之前**——那一刻没有任何循环在处理 Qt 事件，
    悬浮窗既不会绘制也收不到输入，窗口就是「未响应」。上一版在这里开 asyncio
    局部循环等回调正是这个坑：asyncio 不处理 Qt 事件，回调永远不来。
    模态对话框的本质就是嵌套 Qt 循环，这里显式做同一件事。
    """
    from PySide6.QtCore import QEventLoop

    sub = QEventLoop()
    panel.closed.connect(sub.quit)
    if panel.isVisible():
        sub.exec()


class _StartupTaskCompletion(QObject):
    finished = Signal()


def _run_startup_task[T](work: Callable[[], T]) -> T:
    """Run non-UI startup work off-thread while Qt continues rendering.

    The queued completion also covers work that finishes just before exec(): its
    notification stays in the GUI queue. No polling delay or processEvents spin.
    Callers must return data/services, never widgets or live SQLite connections.
    """
    from PySide6.QtCore import QEventLoop

    sub = QEventLoop()
    completion = _StartupTaskCompletion()
    completion.finished.connect(sub.quit, Qt.ConnectionType.QueuedConnection)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="limbowave-startup") as executor:
        future = executor.submit(work)
        future.add_done_callback(lambda _future: completion.finished.emit())
        if not future.done():
            sub.exec()
        return future.result()


def _ask_password(parent: QWidget, title: str, label: str) -> str | None:
    """密码输入（不回显）。取消返回 None。

    启动解锁需要**同步**答案（后续装配依赖密钥），所以用嵌套 Qt 循环等
    悬浮窗回调——提交得值，✕/Esc/点外部得 None。
    """
    # 启动主窗口使用整页入口；通用 QWidget 调用者仍可使用悬浮输入框。
    page = (
        parent.findChild(LoginPage)
        if isinstance(parent, MainWindow) and parent.showing_full_page
        else None
    )
    if page is not None:
        from PySide6.QtCore import QEventLoop

        result: dict[str, str | None] = {"value": None}
        sub = QEventLoop()

        def _submit(value: str) -> None:
            result["value"] = value
            sub.quit()

        page.set_prompt(title, label)
        page.submitted.connect(_submit)
        page.cancelled.connect(sub.quit)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.lastWindowClosed.connect(sub.quit)
        try:
            sub.exec()
        finally:
            page.submitted.disconnect(_submit)
            page.cancelled.disconnect(sub.quit)
            if isinstance(app, QApplication):
                app.lastWindowClosed.disconnect(sub.quit)
        return result["value"]

    from limbowave.ui.floating import ask_prompt

    holder: dict[str, object] = {"value": None, "done": False}

    def _finish(value: str | None) -> None:
        if holder["done"]:
            return
        holder["value"] = value
        holder["done"] = True

    panel = ask_prompt(parent, title, label, _finish, password=True)
    # 「取消」路径不回调 on_submit，靠 closed 兜底成 None
    panel.closed.connect(lambda: _finish(None))
    _run_panel_until_closed(panel)
    value = holder["value"]
    return value if isinstance(value, str) else None


def _ask_startup_confirm(parent: QWidget, title: str, detail: str, *, confirm_text: str) -> bool:
    """启动路径的同步确认框。返回用户是否点了确认键。"""
    holder: dict[str, object] = {"ok": False, "done": False}

    def _answer(value: bool) -> None:
        if holder["done"]:
            return
        holder["ok"] = value
        holder["done"] = True

    panel = ask_confirm(parent, title, detail, _answer, confirm_text=confirm_text)
    _run_panel_until_closed(panel)
    ok = holder["ok"]
    return bool(ok)


def _show_startup_alert(parent: QWidget, title: str, detail: str) -> None:
    """启动路径的同步提示框：等用户看完再继续。"""
    panel = ask_alert(parent, title, detail)
    _run_panel_until_closed(panel)


def _unlock_vault(paths: AppPaths, parent: QWidget) -> VaultKey | None:
    """启动时的资料库解锁流。

    - 首次运行：设置主密码（输入两次）并创建资料库；
    - 已有资料库：最多三次解锁尝试；
    - 取消/失败返回 None——应用进入「未解锁」降级模式（不持久化、无内核）。
    解锁成功后顺带迁移 Phase 1A 旧格式密钥。
    """
    vault = Vault(paths.data_root / "vault.json")
    if not vault.exists:
        password = _ask_password(
            parent, "首次运行：设置主密码", "为资料库设置主密码（用于加密会话与密钥）："
        )
        if password is None:
            return None
        confirm = _ask_password(parent, "确认主密码", "再输入一次：")
        if confirm != password:
            _show_startup_alert(parent, "主密码不一致", "两次输入不一致，本次以未解锁模式运行。")
            return None
        key: VaultKey | None = _run_startup_task(partial(vault.create, password))
    else:
        key = None
        for _ in range(3):
            password = _ask_password(parent, "解锁资料库", "主密码：")
            if password is None:
                return None
            try:
                key = _run_startup_task(partial(vault.unlock, password))
                break
            except InvalidPassword:
                login_page = parent.findChild(LoginPage) if isinstance(parent, MainWindow) else None
                if login_page is not None:
                    login_page.show_password_error()
                else:
                    _show_startup_alert(parent, "解锁失败", "主密码错误。")
        if key is None:
            # 忘掉主密码时的出路：系统保护重置（Task 1.3）。
            # 没有启用恢复就如实说明，不吊着用户。
            key = _offer_system_recovery(vault, parent)

    if key is None:
        return None
    _run_startup_task(partial(migrate_legacy_secrets, paths.data_root / "vault", key))
    return key


def _offer_system_recovery(vault: Vault, parent: QWidget) -> VaultKey | None:
    """主密码尝试用尽后，询问是否用系统保护重置。

    这是「忘记主密码」的**唯一出路**——没启用恢复时明确告知并结束，
    不假装还能救回来。
    """
    from limbowave.domain.platform_capabilities import (
        SYSTEM_RECOVERY_UNAVAILABLE,
        supports_system_identity,
    )

    if not supports_system_identity():
        _show_startup_alert(parent, "系统保护恢复不可用", SYSTEM_RECOVERY_UNAVAILABLE)
        return None
    if not vault.recovery_enabled():
        _show_startup_alert(
            parent,
            "无法解锁",
            "主密码三次输入错误，且未启用系统保护恢复——资料库无法解锁。"
            + NL
            + NL
            + "（系统保护可在解锁后于「设置 → 安全」里开启，"
            + "用于将来忘记主密码时重置。）",
        )
        return None

    confirmed = _ask_startup_confirm(
        parent,
        "忘记主密码？",
        "检测到本资料库启用了系统保护恢复。"
        + NL
        + NL
        + "可以用它重置主密码——不需要旧主密码，但需要能进入当前 Windows 会话。"
        + NL
        + NL
        + "现在重置？",
        confirm_text="重置",
    )
    if not confirmed:
        return None

    new_password = _ask_password(parent, "设置新主密码", "新的主密码（用于加密会话与密钥）：")
    if new_password is None:
        return None
    confirm = _ask_password(parent, "确认新主密码", "再输入一次：")
    if confirm != new_password:
        _show_startup_alert(parent, "主密码不一致", "两次输入不一致，已取消重置。")
        return None

    login_page = parent.findChild(LoginPage) if isinstance(parent, MainWindow) else None
    if login_page is not None:
        login_page.set_busy(
            "正在验证系统身份…",
            "请在 Windows Hello 系统窗口中完成验证。",
        )

    try:
        # Hello/PowerShell can wait for a long time. Running it on the GUI thread makes
        # Windows mark the application as unresponsive.
        key = _run_startup_task(lambda: vault.unlock_with_recovery(new_password))
    except Exception as exc:
        _show_startup_alert(parent, "重置失败", f"{exc}" + NL + NL + "资料库未改动。")
        return None
    _show_startup_alert(parent, "已重置", "主密码已重置，资料库内容未变。")
    return key


def _run_context(
    setup: KernelSetup | dict[str, KernelSetup | None] | None,
    override: dict[str, str | None] | None = None,
    thinking: dict[str, str | None] | None = None,
) -> Callable[[], RunContext]:
    """把装配时的路由决策固化成每轮的运行上下文。

    ``override`` 是会话级站点覆盖（§四.3）：非空时意图快照记录**实际使用的端点**，
    与该会话真正生效的路由一致——记录与执行不能脱节。
    """
    def _context() -> RunContext:
        active_setup = setup.get("value") if isinstance(setup, dict) else setup
        if active_setup is None:
            return RunContext(
                logical_model_id="unknown",
                endpoint_id="unknown",
                routing_reason="未配置可用模型",
            )
        chosen = (override or {}).get("endpoint")
        level = (thinking or {}).get("level")
        if active_setup.supports_thinking is False:
            level = "off"
        elif active_setup.thinking_level_locked or (
            level and level != "off" and active_setup.available_thinking_levels
            and level not in active_setup.available_thinking_levels
        ):
            level = active_setup.default_thinking_level or "off"
        if chosen:
            return RunContext(
                logical_model_id=active_setup.logical_model_id,
                endpoint_id=chosen,
                routing_reason=(
                    active_setup.routing_reason if chosen == active_setup.endpoint_id
                    else f"会话级站点覆盖（配置默认是 {active_setup.endpoint_id}）"
                ),
                app_params=dict(active_setup.app_params),
                supports_images=active_setup.supports_images,
                retry_policy=active_setup.retry_policy,
                thinking_level=level,
                thinking_trial_id=active_setup.thinking_trial_id,
            )
        return RunContext(
            logical_model_id=active_setup.logical_model_id,
            endpoint_id=active_setup.endpoint_id,
            routing_reason=active_setup.routing_reason,
            app_params=dict(active_setup.app_params),
            supports_images=active_setup.supports_images,
            retry_policy=active_setup.retry_policy,
            thinking_level=level,
            thinking_trial_id=active_setup.thinking_trial_id,
        )

    return _context


def _parse_gate_request(title: str, detail: str) -> dict[str, Any] | None:
    """解析扩展的结构化裁决请求（标题固定前缀 + JSON 载荷）。未知格式返回 None。"""
    prefix = "limbowave.gate:"
    if not title.startswith(prefix):
        return None
    try:
        payload = json.loads(detail)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "tool" not in payload:
        return None
    return payload


async def _ask_memory_user(window: MainWindow, title: str, detail: str) -> bool:
    """完整正文使用可滚动纯文本显示，关闭和取消均拒绝。"""
    from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QPushButton

    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    panel = FloatingPanel(window, title, width=560)
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setPlainText(detail)
    text.setMinimumHeight(220)
    panel.content_layout.addWidget(text)
    row = QHBoxLayout()
    reject, accept = QPushButton("拒绝"), QPushButton("允许本次")
    row.addWidget(reject)
    row.addWidget(accept)
    panel.content_layout.addLayout(row)

    def answer(value: bool) -> None:
        if not future.done():
            future.set_result(value)
        panel.close_panel()

    reject.clicked.connect(lambda: answer(False))
    accept.clicked.connect(lambda: answer(True))
    panel.closed.connect(lambda: future.set_result(False) if not future.done() else None)
    panel.popup()
    try:
        return await future
    finally:
        panel.close_panel()


def _paths_from_input(input_params: dict[str, Any]) -> tuple[str, ...]:
    """从工具参数里取路径（供权限判定的资源范围）。"""
    path = input_params.get("path") or input_params.get("file_path")
    return (str(path),) if path else ()


async def _ask_user(window: MainWindow, title: str, detail: str) -> bool:
    """权限确认（悬浮窗）。异步代码保持 await 语义：Future 桥接回调。"""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()

    def _answer(value: bool) -> None:
        if not future.done():
            future.set_result(value)

    ask_confirm(window, title, detail, _answer)
    return await future


async def _ask_permission(
    window: MainWindow, request: ToolRequest, reason: str, scope: GrantScope | None
) -> str:
    """权限确认框（拒绝 / 本会话允许 / 允许本次）。同样用 Future 桥接回调。"""
    from limbowave.ui.permission_prompt import ask_permission

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _answer(value: str) -> None:
        if not future.done():
            future.set_result(value)

    ask_permission(window, request, reason, scope, _answer)
    return await future


def _gate_tool_request(gate: dict[str, Any], gateway: ToolGateway | None) -> ToolRequest:
    """把扩展的裁决载荷变成权限请求。

    应用侧工具交给网关构造——与执行时同一段校验、规范化与路径解析，闸门判定的范围
    （解析后的绝对路径）才与会话授权、网关的检查对得上。若直接用未解析的参数，
    相对路径、空路径永远落不进授权范围：确认过、授权过也照样再弹。
    """
    from limbowave.application.services.tool_gateway import (
        InvalidParams,
        capability_of,
    )
    from limbowave.domain.permissions import Capability, ResourceScope, ToolRequest

    tool = str(gate["tool"])
    input_params = gate.get("input")
    if not isinstance(input_params, dict):
        input_params = {}
    try:
        capability = capability_of(tool)
    except InvalidParams:
        capability = Capability.TERMINAL if tool == "user_bash" else Capability.FILE_READ
    else:
        if gateway is not None:
            return gateway.prepare_request(tool, input_params)
    return ToolRequest(
        tool_name=tool,
        capability=capability,
        scope=ResourceScope(command=gate.get("command"), paths=_paths_from_input(input_params)),
        raw_params=input_params,
    )


def _make_permission_handler(
    window: MainWindow,
    permissions: Any = None,
    conversation_provider: Callable[[], str | None] | None = None,
    gateway: ToolGateway | None = None,
    preset_provider: Callable[[str], Any] | None = None,
    memory_service: Any = None,
    memory_context: Callable[[], Any] | None = None,
) -> Callable[[str, str], Awaitable[bool]]:
    """普通工具使用预选权限；记忆工具独立遵守逐次询问或自动允许策略。"""

    async def confirm(title: str, detail: str) -> bool:
        from limbowave.application.services.run_origin import remote_tools_denied

        if remote_tools_denied(gateway.origin_provider if gateway is not None else None):
            return False
        gate = _parse_gate_request(title, detail)
        conversation_id = conversation_provider() if conversation_provider else None
        if gate is not None and gate.get("tool") == "add_session_memory":
            from limbowave.domain.memory import MemoryPolicy

            context = memory_context() if memory_context else None
            if memory_service is None or context is None or gate.get("run_id") != context.run_id:
                return False
            call_id = gate.get("call_id")
            params = gate.get("input")
            if (not isinstance(call_id, str) or not call_id
                    or not isinstance(params, dict) or set(params) != {"content"}):
                return False
            content = params.get("content")
            if not isinstance(content, str):
                return False
            try:
                content = memory_service.validate_content(content)
                allowed = (
                    await run_blocking(memory_service.effective_policy, context.conversation_id)
                    is MemoryPolicy.ALLOW
                )
                asked = not allowed
                if asked:
                    allowed = await _ask_memory_user(window, "添加会话记忆？",
                        f"""会话 {context.conversation_id} · 分支 {context.branch_id}

{content}""")
                allowed = allowed and memory_context is not None and memory_context() == context
                await run_blocking(
                    memory_service.record_decision,
                    context, call_id, content, allowed=allowed, asked=asked
                )
                allowed = allowed and memory_context is not None and memory_context() == context
                if not allowed:
                    return False
                memory_service.approve(context, call_id, content)
                return True
            except Exception:
                return False

        if permissions is not None and gate is not None and conversation_id is not None:
            from limbowave.application.services.tool_gateway import ToolError
            from limbowave.domain.path_guard import PathEscape
            from limbowave.domain.permissions import (
                Capability,
                Decision,
                PermissionPreset,
                RiskLevel,
                classify_risk,
            )

            try:
                request = await run_blocking(_gate_tool_request, gate, gateway)
            except (PathEscape, ToolError):
                return False
            preset = (
                await run_blocking(preset_provider, conversation_id)
                if preset_provider is not None
                else await run_blocking(permissions.get_preset, conversation_id)
            )
            if not isinstance(preset, PermissionPreset):
                preset = PermissionPreset(preset)

            if preset is PermissionPreset.CHAT_ONLY:
                await run_blocking(permissions.authorize, request, conversation_id)
                return False

            if preset is PermissionPreset.FULL_ACCESS:
                await run_blocking(
                    permissions.authorize, request, conversation_id, user_confirmed=True
                )
                return True

            if preset is PermissionPreset.READ_ONLY:
                risk, _reason = classify_risk(request)
                allowed = (
                    request.capability in (Capability.FILE_READ, Capability.NETWORK)
                    and risk is RiskLevel.NORMAL
                )
                await run_blocking(
                    permissions.authorize, request, conversation_id, user_confirmed=allowed
                )
                return allowed

            decision = await run_blocking(permissions.authorize, request, conversation_id)
            return decision.decision is Decision.ALLOW

        return await _ask_user(window, f"权限请求：{title}", detail)

    return confirm


def _open_data_reset(
    parent: QWidget,
    data_root: Path,
    spawn: Callable[[Coroutine[Any, Any, object]], None],
    on_confirmed: Callable[[DataResetService], None],
) -> DataResetPanel:
    """系统验证在工作线程执行；关窗/取消会作废本次授权，迟到结果不能触发删除。"""
    from shiboken6 import isValid

    from limbowave.application.services.data_reset_service import DataResetError, DataResetService
    from limbowave.ui.data_reset_panel import DataResetPanel

    panel = DataResetPanel(parent, str(data_root))
    service: DataResetService | None = None
    confirmed = False

    async def _authenticate(attempt: DataResetService, password: str) -> None:
        try:
            await asyncio.to_thread(attempt.authenticate, password)
        except InvalidPassword:
            if isValid(panel) and not panel._closing:
                panel.verification_failed("主密码错误，未执行删除。请重新输入。")
        except DataResetError as exc:
            if isValid(panel) and not panel._closing:
                panel.verification_failed(str(exc))
        except Exception:
            if isValid(panel) and not panel._closing:
                panel.verification_failed(
                    "身份验证失败，未执行删除。请检查资料库和 Windows Hello。"
                )
        else:
            if isValid(panel) and not panel._closing:
                attempt.begin_confirmation()
                panel.verification_succeeded()
        finally:
            password = ""

    def _verify(password: str, scope: ResetScope) -> None:
        nonlocal service
        if service is not None:
            service.cancel()
        service = DataResetService(data_root, scope)
        spawn(_authenticate(service, password))

    def _confirm() -> None:
        nonlocal confirmed
        if service is None:
            return
        try:
            service.confirm()
        except DataResetError as exc:
            panel.close_panel()
            ask_alert(parent, "未执行重置", str(exc))
            return
        confirmed = True
        panel.close_panel()
        on_confirmed(service)

    def _cancel() -> None:
        if not confirmed and service is not None:
            service.cancel()

    panel.verification_requested.connect(_verify)
    panel.confirmation_requested.connect(_confirm)
    panel.closed.connect(_cancel)
    panel.popup()
    panel._password.setFocus()
    return panel


def _wire(
    window: MainWindow,
    paths: AppPaths,
    key: VaultKey | None,
    *,
    on_reset: Callable[[DataResetService], None] | None = None,
    diagnostics_available: bool = False,
    appearance_prepared: bool = False,
) -> tuple[
    SessionController, ToolIpcServer | None, Callable[[], None],
    Callable[[], Awaitable[None]], Callable[[], Awaitable[None]],
]:
    with ExitStack() as rollback:
        result = _wire_impl(
            window, paths, key, rollback=rollback, on_reset=on_reset,
            diagnostics_available=diagnostics_available,
            appearance_prepared=appearance_prepared,
        )
        rollback.pop_all()
        return result


def _wire_impl(
    window: MainWindow,
    paths: AppPaths,
    key: VaultKey | None,
    *,
    rollback: ExitStack,
    on_reset: Callable[[DataResetService], None] | None = None,
    diagnostics_available: bool = False,
    appearance_prepared: bool = False,
) -> tuple[
    SessionController,
    ToolIpcServer | None,
    Callable[[], None],
    Callable[[], Awaitable[None]],
    Callable[[], Awaitable[None]],
]:
    """装配通道、内核与视图；返回控制器、工具通道、预热、启动收尾与关闭回调。

    已解锁：权威数据落 SQLite（加密字段 + 事务 + 迁移）。
    未解锁：退回内存仓库——本次会话不持久化，界面明示。

    **顺序有讲究**：工具通道必须**先于内核**启动——通道凭据要随内核进程的环境
    一起下发；反过来（内核先起）模型就永远拿不到应用侧工具。
    """
    # Factory construction runs migrations, but closes its connection before returning.
    # Later units of work still open/close their own connections on the calling thread.
    storage = (
        _run_startup_task(partial(sqlite_uow_factory, paths.data_root / "limbowave.db", key))
        if key is not None else None
    )
    if storage is not None:
        rollback.callback(storage.close)
    _LOG.info("storage.ready", extra={"persistent": storage is not None})
    uow_factory = storage if storage is not None else in_memory_uow_factory()
    shutting_down = False
    resetting = False
    chat = window.chat
    sidebar = window.sidebar
    # Display navigation must not move the single kernel away from an active run.
    preview_scope: tuple[str, str] | None = None
    detached_scope: tuple[str | None, str | None] | None = None

    def _display_scope() -> tuple[str | None, str | None]:
        return preview_scope or detached_scope or (controller.conversation_id, controller.branch_id)

    # MainWindow 构造时 ChatView 默认可用；内核尚未 start 前必须先封住输入，
    # 否则用户可以在 Pi 进程创建前提交 prompt。
    chat.set_available(False, "正在启动模型内核…")

    # 工具网关与对模型的暴露通道（§九.1 / T7.1）
    #
    # 模型发起的工具调用经扩展薄代理送到这里，由应用统一执行：
    # 参数校验 → 权限与资源范围 → 高影响检查 → 执行 → 输出限制 → 审计。
    # 因此执行器、权限、审计都只有**一份实现**。
    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import ToolGateway

    permissions = PermissionService(uow_factory)
    # 附件文档服务（read_document 的执行后端）在这里就要建好：工具通道先于内核
    # 启动，网关构造不能晚于通道；blob 仓需要主密钥，未解锁时保持 None——
    # read_document 会如实返回「文档服务未配置」，不会静默假装可读。
    blob_store = files = None
    if key is not None:
        from limbowave.application.services.file_service import FileService
        from limbowave.infrastructure.crypto.blob_store import BlobStore

        blob_store = BlobStore(paths.data_root / "blobs", key)
        files = FileService(uow_factory, blob_store=blob_store)
    from limbowave.application.services.tool_gateway import TerminalTools
    from limbowave.infrastructure.shell import create_executor, probe_shell

    shell_env = _run_startup_task(partial(probe_shell, paths.data_root))
    terminal = TerminalTools(create_executor(shell_env)) if shell_env is not None else None
    tool_gateway = ToolGateway(
        permissions, paths.data_root, documents=files, terminal=terminal
    )
    # 装配可观测：read_document 报「文档服务未配置」时，先看这条日志——
    # documents=False 意味着这个进程跑在未解锁状态，或是修复前的旧进程。
    _LOG.info(
        "tool_gateway.ready",
        extra={"documents": files is not None, "workspace": str(paths.data_root)},
    )
    # 控制器稍后才建：派发经持有者延迟取会话标识（闭包在调用时才解引用）
    holder: dict[str, object] = {}

    async def _dispatch_tool(
        tool_name: str, params: dict[str, object], conversation_id: str, confirmed: bool
    ) -> dict[str, object]:
        """通道来的调用统一走网关。**通道不能自行铸造授权**：
        未标记为已确认时，需要确认的请求被拒（确认已在扩展的 tool_call 闸门做过）。"""
        if shutting_down:
            return {"ok": False, "error": "应用正在关闭，工具调用已停止"}
        if tool_name == "add_session_memory":
            context = tool_gateway.memory_context()
            if context is None:
                return {"ok": False, "error": "记忆调用没有活动运行"}
            conversation_id = context.conversation_id
        result = await run_blocking(
            tool_gateway.invoke, tool_name, params, conversation_id, user_confirmed=confirmed
        )
        return {
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "truncated": result.truncated,
            "notes": list(result.notes),
        }

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from limbowave.application.services.endpoint_rate_limiter import EndpointRateLimiter

    request_limiter = EndpointRateLimiter()
    rollback.callback(request_limiter.stop)
    # 测活等待 RPM 可能很长，不能占满对话/存储共用的默认 executor。
    probe_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="model-probe")
    rollback.callback(lambda: probe_executor.shutdown(wait=False, cancel_futures=True))

    async def _run_probe(function: Callable[..., Any], *args: Any) -> Any:
        cancelled = Event()
        try:
            return await asyncio.get_running_loop().run_in_executor(
                probe_executor,
                partial(function, *args, limiter=request_limiter, cancelled=cancelled),
            )
        finally:
            cancelled.set()

    tool_ipc: ToolIpcServer | None = None
    extra_env: dict[str, str] = {
        "LIMBOWAVE_SHELL_ENV": (
            shell_env.describe() if shell_env is not None else "本机未找到可用 Shell，终端不可用。"
        ),
    }
    if key is not None:
        tool_ipc = ToolIpcServer(
            _dispatch_tool,
            rate_limit=request_limiter.try_acquire,
            conversation_provider=lambda: getattr(
                holder.get("controller"), "conversation_id", None
            ),
        )
        loop = asyncio.get_event_loop_policy().get_event_loop()
        def _rollback_ipc(server: ToolIpcServer = tool_ipc) -> None:
            loop.run_until_complete(server.stop())

        rollback.callback(_rollback_ipc)
        loop.run_until_complete(tool_ipc.start())
        _LOG.info("tool_ipc.started")
        if tool_ipc.session is not None:
            extra_env[IPC_ENV] = tool_ipc.session.to_env_value()
        if tool_ipc.rate_limit_session is not None:
            extra_env[RATE_LIMIT_IPC_ENV] = tool_ipc.rate_limit_session.to_env_value()

    setup = _run_startup_task(partial(_build_kernel, paths, key, extra_env=extra_env))
    setup_state: dict[str, KernelSetup | None] = {"value": setup}
    # 会话级站点覆盖（§四.3）：在 controller 之前建立，供 _run_context 读取
    session_endpoint_override: dict[str, str | None] = {"endpoint": None}
    thinking_state: dict[str, str | None] = {"level": None}
    thinking_trial: ThinkingTrial | None = None
    controller = SessionController(
        setup.kernel if setup is not None else None,
        uow_factory=uow_factory,
        context=_run_context(setup_state, session_endpoint_override, thinking_state),
    )
    holder["controller"] = controller
    tool_gateway.origin_provider = lambda: controller.coordinator().run_origin
    conversation_process = None
    if key is not None:
        from limbowave.infrastructure.conversation_process import (
            ConversationProcess,
            ConversationProcessConfig,
        )

        conversation_process = ConversationProcess(ConversationProcessConfig(
            paths.data_root / "limbowave.db", paths.data_root, key.key_bytes()
        ))
        rollback.callback(conversation_process.close_unstarted)
        controller.coordinator().storage_worker = conversation_process

    # 三个查询/管理服务（只读或经服务层落盘；窗口不直接碰仓库）
    from limbowave.application.services.configuration_service import ConfigurationService
    from limbowave.application.services.conversation_title_service import (
        ConversationTitleService,
    )
    from limbowave.application.services.credential_service import CredentialService
    from limbowave.application.services.history_service import ConversationSummary, HistoryService
    from limbowave.application.services.memory_service import MemoryService
    from limbowave.application.services.request_log_service import RequestLogService
    from limbowave.application.services.settings_service import SettingsService
    from limbowave.infrastructure.configuration.json_config_repository import (
        JsonConfigRepository,
    )
    from limbowave.infrastructure.crypto.secret_store import SecretStore

    memories = MemoryService(uow_factory, ConfigurationService(
        JsonConfigRepository(paths.data_root / "config.json")
    ))
    controller.set_memory_service(memories)
    tool_gateway.memory_service = memories
    tool_gateway.memory_context = lambda: controller.memory_context
    history = HistoryService(uow_factory)
    history_reader = HistoryReader()
    history_list_generation = 0
    conversation_titles = ConversationTitleService()
    request_logs = RequestLogService(uow_factory)
    settings = SettingsService(
        ConfigurationService(JsonConfigRepository(paths.data_root / "config.json"))
    )
    request_limiter.configure(settings.load().endpoints)
    credentials = (
        CredentialService(SecretStore(key, paths.data_root / "vault" / "secrets.json"))
        if key is not None
        else None
    )

    # Phase 4：文件 / 剪贴板 / 图片 / 附件装配。blob 仓与 FileService 已在
    # 工具网关之前建好（read_document 需要）；这里补齐其余服务并接装配器。
    images = clipboard = attachments = None
    if key is not None:
        from limbowave.application.services.attachment_service import AttachmentService
        from limbowave.application.services.clipboard_service import ClipboardService
        from limbowave.application.services.image_service import ImageService

        assert blob_store is not None and files is not None
        images = ImageService(uow_factory, blob_store)
        clipboard = ClipboardService(uow_factory, blob_store)
        attachments = AttachmentService(files, images)
        controller.coordinator().attachment_builder = attachments.build

    # 界面偏好（Task 8.4）：主题与字号，普通配置（不含密钥）
    from limbowave.application.services.appearance_theme_service import AppearanceThemeService
    from limbowave.application.services.preferences_service import PreferencesService

    preferences = PreferencesService(paths.data_root / "preferences.json")

    def _load_appearance_themes() -> AppearanceThemeService:
        legacy = preferences.load()
        return AppearanceThemeService(
            paths.data_root / "themes",
            legacy_theme=legacy.theme,
            legacy_background_image=legacy.background_image,
            legacy_blur_radius=legacy.blur_radius,
        )

    appearance_themes = _run_startup_task(_load_appearance_themes)

    # Phase 5：上下文压缩（版本管理 + 生成编排）
    from limbowave.application.services.compression_service import CompressionService

    compression = CompressionService(uow_factory)

    # 终端后端已在工具网关装配时探测并接线。
    if shell_env is not None and shell_env.is_fallback:
        # §10.1：pwsh 缺失回退到 Windows PowerShell 5.1 必须**明示**给用户
        chat.set_status(f"终端环境：{shell_env.kind.value} {shell_env.version}（回退）")

    def _apply_appearance(
        preview: object | None = None, *, refresh_history: bool = False
    ) -> None:
        """Apply a committed theme or an editor preview without persisting the latter."""
        from shiboken6 import isValid

        from limbowave.domain.appearance import AppearanceTheme
        from limbowave.ui import theme
        from limbowave.ui.click_ripple import install_click_ripples
        from limbowave.ui.cursor_reveal import install_cursor_reveal
        from limbowave.ui.font_registry import family_for_file
        from limbowave.ui.theme_effects import apply_text_glow, install_hover_suspension

        definition = (
            preview if isinstance(preview, AppearanceTheme) else appearance_themes.active_theme
        )
        current = preferences.load()
        previous_palette = theme.current_palette()
        old_card_surface = theme.card_surface()
        old_sizes = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
        theme.apply_appearance_theme(definition)
        theme.set_font_scale(current.font_scale)
        family_for_file(current.font_file)
        theme.set_font_family(current.font_family)
        image = appearance_themes.resolve_asset(definition.background.asset)
        window.set_appearance_theme(definition, str(image) if image is not None else "")
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(theme.app_stylesheet())
        theme.refresh_inline_styles(
            window, previous_palette, old_sizes, old_card_surface=old_card_surface
        )
        apply_text_glow(window, definition.text_glow)
        install_hover_suspension(window, definition.materials)
        install_click_ripples(window)
        install_cursor_reveal(window)
        sidebar.restyle()
        if settings_page is not None and isValid(settings_page):
            settings_page.restyle()
        if refresh_history:
            _refresh_conversations()
            messages = _current_messages()
            if messages:
                chat.load_history(_history_payload(
                    messages, uow_factory=uow_factory, branch_id=controller.branch_id
                ))
        chat.set_status(
            f"外观预览：{definition.name}"
            if preview is not None
            else f"外观已应用：{definition.name}"
        )

    def _refresh_conversations(summaries: list[ConversationSummary] | None = None) -> None:
        nonlocal history_list_generation
        if shutting_down:
            return
        history_list_generation += 1
        generation = history_list_generation
        if summaries is None:
            async def _read_list() -> None:
                loaded = await history_reader.read(history.list_conversations)
                if generation == history_list_generation:
                    _refresh_conversations(loaded)

            _spawn(_read_list())
            return
        rows = [
            (s.conversation.id, s.conversation.title, s.message_count)
            for s in summaries
        ]
        sidebar.show_conversations(rows)
        _refresh_branches()  # 当前会话指示 + 已展开会话的分支消息数

    # fire-and-forget 任务要持强引用——否则 GC 可能在完成前回收它（实测踩过）
    _pending_tasks: set[asyncio.Task[object]] = set()
    _title_in_flight: set[str] = set()
    _title_checked: set[str] = set()

    def _spawn(coro: Coroutine[Any, Any, object]) -> None:
        if shutting_down:
            coro.close()
            return
        task = asyncio.ensure_future(coro)
        _pending_tasks.add(task)

        def _finished(done: asyncio.Task[object]) -> None:
            _pending_tasks.discard(done)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                # fire-and-forget 任务没有 await 方；必须在这里消费异常并反馈，
                # 否则只会在控制台留下 "Task exception was never retrieved"。
                _LOG.error("background.operation_failed", exc_info=error)
                chat.add_error(f"后台操作失败：{redact_text(str(error))}")

        task.add_done_callback(_finished)

    def _active_title_route() -> tuple[str, str] | None:
        """当前实际使用的 (provider/站点, 远端模型 ID)。"""
        if setup is None:
            return None
        config = settings.load()
        model = next((m for m in config.models if m.id == setup.logical_model_id), None)
        endpoint_id = session_endpoint_override["endpoint"] or setup.endpoint_id
        binding = next(
            (b for b in (model.bindings if model else ()) if b.endpoint_id == endpoint_id),
            None,
        )
        if binding is None:
            return None
        return endpoint_id, binding.model_id

    async def _maybe_offer_conversation_title(
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        route: tuple[str, str],
        kernel: AgentKernel,
    ) -> None:
        """首轮落库后后台生成标题，再让用户确认或编辑。"""
        try:
            await controller.wait_idle()
            if (
                await history_reader.read(history.title, conversation_id)
                != DEFAULT_CONVERSATION_TITLE
            ):
                return

            suggestion = await conversation_titles.suggest(
                kernel,
                user_text,
                assistant_text,
                provider=route[0],
                model_id=route[1],
            )
            if not suggestion:
                return
            # 用户等待期间可能已切换、删除或手动重命名；此时不弹错位确认框。
            if controller.conversation_id != conversation_id:
                return
            if (
                await history_reader.read(history.title, conversation_id)
                != DEFAULT_CONVERSATION_TITLE
            ):
                return

            def _apply_title(title: str) -> None:
                _spawn(_save_title(title))

            async def _save_title(title: str) -> None:
                # 弹窗打开后仍可能从侧栏完成重命名，避免覆盖用户的新标题。
                if (
                    await history_reader.read(history.title, conversation_id)
                    != DEFAULT_CONVERSATION_TITLE
                ):
                    return
                if await run_blocking(history.rename, conversation_id, title):
                    _refresh_conversations()
                    chat.set_status("会话名称已更新")

            ask_prompt(
                window,
                "为会话命名",
                "模型建议如下。可直接采用，也可修改后保存：",
                _apply_title,
                default_text=suggestion,
                confirm_label="采用",
            )
        finally:
            _title_in_flight.discard(conversation_id)
            _title_checked.add(conversation_id)

    # 用户意图 → 控制器（调度到事件循环）。
    # 发送时带上附件栏里的附件：装配成内核图片载荷 + 附件 id 列表（Task 4.4）。
    send_preparing = False
    attachment_tasks: set[asyncio.Task[Any]] = set()

    async def _prepare_attachment_send(
        text: str, ids: list[str], scope: tuple[str | None, str | None],
        message_id: str | None = None, *, draft_generation: int,
    ) -> None:
        nonlocal send_preparing
        # 发送准备只锁定交互，保留聊天内容，不显示会话加载遮罩。
        chat.set_history_loading(True, show_overlay=False)
        try:
            while attachment_tasks:
                await asyncio.gather(*tuple(attachment_tasks))
                ids = chat.attachments.attachment_ids()
            if (_display_scope() != scope or controller.busy
                    or chat.attachments.generation != draft_generation):
                chat.restore_draft(text)
                chat.set_status("运行目标已变化或正在忙，输入已保留。")
                return
            with controller.coordinator().runtime_transition():
                payload = await run_blocking(attachments.build, ids) if attachments else None
                if (_display_scope() != scope
                        or chat.attachments.generation != draft_generation):
                    return
            chat.attachments.clear()
            chat.set_history_loading(False)
            kwargs: dict[str, Any] = {
                "attachment_ids": payload.attachment_ids, "images": payload.images,
                "document_note": payload.document_note,
            } if payload else {}
            if message_id is None:
                if not await _send_with_catalog_sync(text, **kwargs):
                    chat.restore_draft(text)
            else:
                await controller.edit_user_message(message_id, text, **kwargs)
        finally:
            send_preparing = False
            chat.set_history_loading(False)

    def _send(text: str) -> None:
        nonlocal send_preparing
        if send_preparing:
            return
        # §七.1：达到阻断阈值时**不放行**这次发送，而是给出明确的处置选择。
        # 计划书用词是「阻止可能超限的发送，除非用户调整上下文」——
        # 所以这里不是硬拒绝，而是把「先压缩」这条路摆在用户面前。
        if chat.usage_action == "block":

            def _on_block_answer(ok: bool) -> None:
                if ok:
                    _spawn(_do_compress())
                else:
                    chat.set_status("已取消发送（上下文超过阻断阈值）")

            ask_confirm(
                window,
                "上下文接近上限",
                "当前上下文占用已超过阻断阈值，直接发送可能超出模型窗口。"
                + NL
                + NL
                + "先压缩上下文再发送？",
                _on_block_answer,
                confirm_text="先压缩",
            )
            return
        ids = chat.attachments.attachment_ids()
        scope = _display_scope()
        send_preparing = True
        _spawn(_prepare_attachment_send(
            text, ids, scope, draft_generation=chat.attachments.generation
        ))

    window.command_requested.connect(_send)

    @dataclass
    class _CompressionRun:
        """进行中的压缩任务。停止键只取消这一轮，不转去中止主对话。"""

        task: asyncio.Task[Any] | None = None
        abortable: bool = False

    compression_run = _CompressionRun()

    def _abort_compression() -> bool:
        """取消正在生成的压缩。没有可取消的压缩时返回 False。"""
        if not compression_run.abortable:
            return False
        task = compression_run.task
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        return True

    def _on_stop() -> None:
        # 压缩遮罩盖住了对话的停止键。遮罩还在时，这一下只属于压缩。
        if chat.compressing:
            if _abort_compression():
                chat.note_compression_stopping()
            return
        _spawn(controller.abort())

    window.stop_requested.connect(_on_stop)

    # ---------- 附件栏（Phase 4） ----------

    # 已发送附件的缩略图：用户消息 id → 意图快照里的附件 id → 展示数据。
    # 意图快照在发出 "user" 事件前已提交，所以新消息也能查到。
    from limbowave.ui.sent_attachments import SentAttachment

    sent_ids_by_message: dict[str, tuple[str, ...]] = {}
    sent_items: dict[str, SentAttachment | None] = {}

    def _read_sent_attachment(attachment_id: str) -> tuple[str, str, QImage | None] | None:
        # QImage 解码可在线程中执行；QPixmap 只能在下面的 GUI 应用阶段创建。
        try:
            image = images.get(attachment_id) if images is not None else None
            if images is not None and image is not None:
                summary = images.summarize(image.id)
                decoded = QImage.fromData(images.read_bytes(image.id))
                return (
                    Path(image.source_path).name if image.source_path else image.id,
                    f"{image.format.value.upper()} · {image.dimensions} · {summary.size_text}"
                    f" · ~{summary.context_tokens_estimate} tokens(估算)",
                    decoded if not decoded.isNull() else None,
                )
            if files is not None and (document := files.get(attachment_id)) is not None:
                return (
                    document.display_name,
                    f"{document.line_count} 行 · {document.size_bytes} B",
                    None,
                )
        except Exception:
            pass  # 附件已被删除或读不出：不影响消息本身。
        return None

    def _display_attachment(data: tuple[str, str, QImage | None] | None) -> SentAttachment | None:
        if data is None:
            return None
        title, detail, image = data
        return SentAttachment(
            title, detail, QPixmap.fromImage(image) if image is not None else None
        )

    def _read_sent_attachment_ids(message_id: str) -> tuple[str, ...]:
        with uow_factory() as uow:
            message = uow.messages.get(message_id)
            intent = (
                uow.snapshots.get_intent(message.run_id) if message and message.run_id else None
            )
            return tuple(intent.attachment_ids) if intent else ()

    attachment_reads: set[str] = set()

    async def _load_sent_attachments(message_id: str) -> None:
        try:
            ids = sent_ids_by_message.get(message_id)
            if ids is None:
                ids = await history_reader.read(_read_sent_attachment_ids, message_id)
            missing = set(ids) - sent_items.keys()
            data = await history_reader.read(
                lambda: {aid: _read_sent_attachment(aid) for aid in missing}
            )
            sent_ids_by_message[message_id] = ids
            sent_items.update({aid: _display_attachment(value) for aid, value in data.items()})
            items = [item for aid in ids if (item := sent_items.get(aid)) is not None]
            chat.refresh_sent_attachments(message_id, items)
            if window.history_preview is not None:
                window.history_preview.refresh_sent_attachments(message_id, items)
        finally:
            attachment_reads.discard(message_id)

    def _sent_attachments(message_id: str) -> list[SentAttachment]:
        ids = sent_ids_by_message.get(message_id)
        if ids is None or any(aid not in sent_items for aid in ids):
            if message_id not in attachment_reads:
                attachment_reads.add(message_id)
                _spawn(_load_sent_attachments(message_id))
            return []
        return [item for aid in ids if (item := sent_items.get(aid)) is not None]

    chat.set_attachment_resolver(_sent_attachments)

    def _add_imported_attachment(attachment_id: str, data: Any) -> None:
        item = _display_attachment(data)
        sent_items[attachment_id] = item
        if item is not None:
            chat.attachments.add_attachment(
                attachment_id, item.title, item.detail, thumbnail=item.thumbnail
            )

    async def _import_attachments(
        paths: list[str], raw: bytes | None, generation: int,
        scope: tuple[str | None, str | None],
    ) -> None:
        assert images is not None and files is not None

        def import_one(path: str | None) -> tuple[str, Any]:
            from limbowave.domain.files import classify_path

            if path is None:
                assert raw is not None
                attachment = images.import_bytes(raw)
            elif isinstance(classify_path(Path(path)), FileKind):
                document = files.index_path(Path(path))
                return document.id, _read_sent_attachment(document.id)
            else:
                attachment = images.import_file(Path(path))
            return attachment.id, _read_sent_attachment(attachment.id)

        for path in paths if raw is None else [None]:
            try:
                aid, data = await run_blocking(import_one, path)
                if generation != chat.attachments.generation or scope != _display_scope():
                    return
                _add_imported_attachment(aid, data)
            except Exception as exc:
                chat.add_error(f"无法添加附件：{exc}")

    def _queue_attachment(work: Coroutine[Any, Any, None]) -> None:
        if shutting_down:
            work.close()
            return
        task = asyncio.create_task(work)
        attachment_tasks.add(task)
        _pending_tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            attachment_tasks.discard(done)
            _pending_tasks.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                chat.add_error(f"附件处理失败：{redact_text(str(error))}")

        task.add_done_callback(finished)

    def _queue_import(paths: list[str], raw: bytes | None = None) -> None:
        if files is None or images is None:
            chat.set_status("资料库未解锁：附件功能不可用")
            return
        _queue_attachment(_import_attachments(
            paths, raw, chat.attachments.generation, _display_scope()
        ))

    def _attach_paths(paths: list[str]) -> None:
        _queue_import(paths)

    def _attach_pasted_image(raw: bytes) -> None:
        _queue_import([], raw)

    def _attach_files() -> None:
        from PySide6.QtWidgets import QFileDialog

        paths, _ = QFileDialog.getOpenFileNames(
            window, "添加附件", "", "支持的文件 (*.txt *.md *.jpg *.jpeg *.png)"
        )
        _attach_paths(paths)

    def _attach_clipboard() -> None:
        if clipboard is None:
            chat.set_status("资料库未解锁：剪贴板文档不可用")
            return
        from PySide6.QtWidgets import QApplication as _QApp

        text = _QApp.clipboard().text()
        if not text.strip():
            chat.set_status("剪贴板为空")
            return
        generation, scope = chat.attachments.generation, _display_scope()

        async def save_clipboard() -> None:
            document = await run_blocking(clipboard.save, text)
            if generation != chat.attachments.generation or scope != _display_scope():
                return
            chat.attachments.add_attachment(
                document.id, document.display_name, f"{document.line_count} 行 · 剪贴板"
            )
            chat.set_status(f"已保存为临时文档：{document.display_name}")

        _queue_attachment(save_clipboard())

    chat.attachments.attach_files_requested.connect(_attach_files)
    chat.attachments.attach_clipboard_requested.connect(_attach_clipboard)
    chat.files_dropped.connect(_attach_paths)
    chat.image_pasted.connect(_attach_pasted_image)

    # ---------- 附件上拉菜单：图片 / 文档 / 文件夹 / 其他会话片段 ----------

    def _pick_files(title: str, name_filter: str) -> None:
        from PySide6.QtWidgets import QFileDialog

        paths, _ = QFileDialog.getOpenFileNames(window, title, "", name_filter)
        _attach_paths(paths)

    def _attach_folder() -> None:
        """文件夹：添加其中（不递归）受支持的文件；其余跳过并如实提示。"""
        from PySide6.QtWidgets import QFileDialog

        from limbowave.domain.files import UnsupportedFileType, classify_path

        folder = QFileDialog.getExistingDirectory(window, "添加文件夹")
        if not folder:
            return
        generation, scope = chat.attachments.generation, _display_scope()

        def scan() -> tuple[list[str], int]:
            supported: list[str] = []
            skipped = 0
            for entry in sorted(Path(folder).iterdir()):
                if not entry.is_file():
                    continue
                try:
                    classify_path(entry)
                except UnsupportedFileType:
                    skipped += 1
                    continue
                supported.append(str(entry))
            return supported, skipped

        async def load() -> None:
            supported, skipped = await run_blocking(scan)
            if generation != chat.attachments.generation or scope != _display_scope():
                return
            limit = 50
            if not supported:
                chat.set_status(f"文件夹内没有支持的文件（txt/md/jpg/png）：{folder}")
                return
            _attach_paths(supported[:limit])
            extra = len(supported) - limit
            note = f"，超出上限未添加 {extra} 个" if extra > 0 else ""
            chat.set_status(
                f"已从文件夹添加 {min(len(supported), limit)} 个文件"
                f"（跳过不支持的 {skipped} 个{note}）"
            )

        _queue_attachment(load())

    def _attach_snippet() -> None:
        _spawn(_prepare_snippet())

    async def _prepare_snippet() -> None:
        """其他会话片段：选会话 → 勾选消息 → 保存为加密临时文档作为附件。"""
        if clipboard is None:
            chat.set_status("资料库未解锁：会话片段不可用")
            return
        from PySide6.QtWidgets import QComboBox, QListWidget, QListWidgetItem, QPushButton

        others = [
            s for s in await history_reader.read(history.list_conversations)
            if s.conversation.id != controller.conversation_id
        ]
        if not others:
            chat.set_status("没有其他会话可引用")
            return
        panel = FloatingPanel(window, "添加其他会话片段", width=520)
        combo = QComboBox()
        for summary in others:
            combo.addItem(
                f"{summary.conversation.title}（{summary.message_count} 条）",
                summary.conversation.id,
            )
        panel.content_layout.addWidget(combo)
        messages_list = QListWidget()
        messages_list.setMinimumHeight(280)
        panel.content_layout.addWidget(messages_list, 1)
        role_names = {"user": "用户", "assistant": "助手"}

        async def _load() -> None:
            from shiboken6 import isValid

            selected = str(combo.currentData())
            opened = await history_reader.read(history.open_conversation, selected)
            if not isValid(combo) or str(combo.currentData()) != selected:
                return
            messages_list.clear()
            for message in opened[1] if opened else []:
                role = role_names.get(message.role.value)
                if role is None or not message.content.strip():
                    continue
                preview = " ".join(message.content.split())[:80]
                item = QListWidgetItem(f"{role}：{preview}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, f"【{role}】{NL}{message.content}")
                messages_list.addItem(item)

        combo.currentIndexChanged.connect(lambda _index: _spawn(_load()))
        _spawn(_load())

        submit = QPushButton("添加为附件")
        submit.setProperty("accent", True)

        async def _submit() -> None:
            parts = [
                str(messages_list.item(i).data(Qt.ItemDataRole.UserRole))
                for i in range(messages_list.count())
                if messages_list.item(i).checkState() == Qt.CheckState.Checked
            ]
            if not parts:
                chat.set_status("请先勾选要引用的消息")
                return
            title = combo.currentText().rsplit("（", 1)[0]
            document = await run_blocking(
                clipboard.save, (NL + NL).join(parts), display_name=f"会话片段 · {title}"
            )
            chat.attachments.add_attachment(
                document.id,
                document.display_name,
                f"{len(parts)} 条消息 · {document.line_count} 行",
            )
            panel.close_panel()

        submit.clicked.connect(lambda: _queue_attachment(_submit()))
        panel.content_layout.addWidget(submit, 0, Qt.AlignmentFlag.AlignRight)
        panel.popup()

    chat.attachments.attach_images_requested.connect(
        lambda: _pick_files("添加图片", "图片 (*.jpg *.jpeg *.png)")
    )
    chat.attachments.attach_documents_requested.connect(
        lambda: _pick_files("添加文档", "文档 (*.txt *.md)")
    )
    chat.attachments.attach_folder_requested.connect(_attach_folder)
    chat.attachments.attach_snippet_requested.connect(_attach_snippet)

    # ---------- 上下文压缩（Phase 5） ----------

    from limbowave.application.services.compression_trigger import CompressionTrigger

    compression_trigger = CompressionTrigger()
    usage_refresh_generation = {"value": 0}

    def _compression_settings() -> object:
        from limbowave.domain.configuration import CompressionSettings

        return settings.load().compression or CompressionSettings()

    async def _refresh_usage() -> None:
        usage_refresh_generation["value"] += 1
        generation = usage_refresh_generation["value"]
        scope = (controller.conversation_id, controller.branch_id)
        if preview_scope is not None:
            return
        config_settings = _compression_settings()
        report = await compression.estimate(
            controller.coordinator().kernel,
            settings=config_settings,  # type: ignore[arg-type]
        )
        if (preview_scope is not None
                or scope != (controller.conversation_id, controller.branch_id)
                or generation != usage_refresh_generation["value"]):
            return
        if scope[1] is not None:
            active = await run_blocking(compression.get_active, scope[1])
            if active is not None:
                compression_trigger.mark_handled(scope[1])
        if (preview_scope is not None
                or scope != (controller.conversation_id, controller.branch_id)
                or generation != usage_refresh_generation["value"]):
            return
        percent = report.usage.percent if report.usage else None
        chat.set_context_usage(percent, report.display)
        chat.set_usage_action(report.action.value)  # 供发送前阻断判断（§七.1）

        if report.action.value == "block":
            chat.set_status("上下文接近上限：发送会被阻止，请先压缩")
        elif report.action.value == "preview":
            chat.set_status("上下文已过预览阈值：建议压缩（长按压缩按钮）")
            # §七.1「达到阈值后生成压缩预览」：配置允许时自动生成一次
            if getattr(config_settings, "auto_preview", False):
                await _do_compress(automatic=True)

    async def _refresh_compression_markers(conversation_id: str, branch_id: str) -> None:
        scope = (conversation_id, branch_id)
        if preview_scope is not None or scope != (controller.conversation_id, controller.branch_id):
            return
        payload = await _load_branch_payload(branch_id)
        # A different conversation/branch may have opened while storage was being read.
        if preview_scope is None and scope == (controller.conversation_id, controller.branch_id):
            chat.sync_compression_markers(payload)

    async def _run_compression(version_id: str, kernel: AgentKernel) -> bool:
        """生成一次摘要，期间输入框被遮罩成进度条（见 ``ChatView.begin_compression``）。

        进度初值 = 当前占用；完成后按「压缩后 tokens / 上下文窗口」多退少补。
        """
        report = await compression.estimate(kernel)
        usage = report.usage
        chat.begin_compression(usage.percent if usage else None)
        compression_run.abortable = True
        after: float | None = None
        stopped = False
        try:
            ok = await compression.generate_isolated(
                version_id, kernel, on_event=_compression_event
            )
            if ok and usage is not None and usage.context_window > 0:
                version = await run_blocking(compression.get, version_id)
                if version is not None:
                    after = version.tokens_after / usage.context_window * 100.0
        except asyncio.CancelledError:
            stopped = True
            raise
        finally:
            compression_run.abortable = False
            chat.finish_compression(after, stopped=stopped)
        return ok

    def _compression_event(event: KernelEvent) -> None:
        # Isolated streams reach presentation only, never the main run coordinator.
        if event.kind != 'message.update':
            return
        delta = event.payload.get('assistantMessageEvent') or {}
        if delta.get('type') == 'thinking_delta':
            chat.append_thinking_delta(str(delta.get('delta', '')))
        elif delta.get('type') == 'text_delta':
            chat.append_assistant_delta(str(delta.get('delta', '')))

    async def _do_compress(
        *, automatic: bool = False, retry_version_id: str | None = None,
    ) -> None:
        coordinator = controller.coordinator()
        conversation_id, branch_id = controller.conversation_id, coordinator.branch_id
        if conversation_id is None or branch_id is None:
            chat.set_status('还没有会话可压缩')
            return
        kernel = coordinator.kernel
        if kernel is None or coordinator.busy or preview_scope is not None:
            if not automatic:
                chat.set_status('当前正忙或未配置内核，无法压缩')
            return
        ticket = compression_trigger.try_begin(branch_id, automatic=automatic)
        if ticket is None:
            return
        try:
            compression_run.task = asyncio.current_task()
            if retry_version_id is not None:
                previous = await run_blocking(compression.get, retry_version_id)
                if previous is None or previous.branch_id != branch_id:
                    chat.set_status("请先切回该压缩版本所属的分支")
                    return
            if (preview_scope is not None or coordinator.kernel is not kernel
                    or (coordinator.conversation_id, coordinator.branch_id)
                    != (conversation_id, branch_id)):
                return
            # Freeze the preview boundary/route while its isolated model is running.
            with coordinator.runtime_transition():
                await coordinator.wait_idle()
                report = await compression.estimate(kernel)
                version = await run_blocking(
                    compression.create_version, conversation_id, branch_id,
                    tokens_before=report.usage.tokens if report.usage else 0,
                    compression_model_id=setup.logical_model_id if setup else '',
                    compression_endpoint_id=setup.endpoint_id if setup else '',
                )
                await _run_compression(version.id, kernel)

            from limbowave.ui.compression_widgets import CompressionPreviewDialog

            panel = FloatingPanel(window, '压缩预览', width=760)
            dialog = CompressionPreviewDialog(
                compression, version.id, panel, runtime_managed=True
            )
            dialog.setWindowFlags(Qt.WindowType.Widget)

            async def apply_version(version_id: str, edited: str) -> None:
                ok = await coordinator.apply_compression(version_id, edited_summary=edited)
                if ok:
                    compression_trigger.mark_handled(branch_id)
                    await _refresh_compression_markers(conversation_id, branch_id)
                if shiboken6.isValid(dialog):
                    dialog.finish_runtime_change(ok)
                await _refresh_usage()

            async def rollback_version(target_branch: str) -> None:
                ok = await coordinator.rollback_compression(target_branch)
                if ok:
                    await _refresh_compression_markers(conversation_id, target_branch)
                if shiboken6.isValid(dialog):
                    dialog.finish_runtime_change(ok, rollback=True)
                await _refresh_usage()

            dialog.apply_requested.connect(lambda vid, text: _spawn(apply_version(vid, text)))
            dialog.rollback_requested.connect(lambda bid: _spawn(rollback_version(bid)))
            dialog.retry_requested.connect(lambda vid: _spawn(_retry_compress(vid)))
            panel.content_layout.addWidget(dialog, 1)
            dialog.finished.connect(panel.close_panel)
            panel.popup()
            await _refresh_usage()
        finally:
            if compression_run.task is asyncio.current_task():
                compression_run.task = None
            compression_trigger.finish(ticket)

    async def _retry_compress(version_id: str) -> None:
        """Claim the shared guard before even reading the previous version."""
        await _do_compress(retry_version_id=version_id)

    chat.compress_requested.connect(lambda: _spawn(_do_compress()))

    # ---------- 输入区权限档位 ----------

    from limbowave.domain.permissions import Capability, PermissionPreset
    from limbowave.ui.permissions_dialog import open_custom_permissions

    permission_updates = asyncio.Lock()
    permission_preset_draft = [PermissionPreset.READ_ONLY]
    permission_custom_draft: set[Capability] = set()
    chat.set_permission_preset(permission_preset_draft[0])

    def _persist_permission_selection(conversation_id: str) -> None:
        preset = permission_preset_draft[0]
        permissions.set_preset(conversation_id, preset)
        if preset is PermissionPreset.CUSTOM:
            saved = {
                grant.capability for grant in permissions.list_grants(conversation_id)
            }
            if saved != permission_custom_draft:
                permissions.replace_custom_grants(
                    conversation_id,
                    permission_custom_draft,
                    workspace_root=str(tool_gateway.workspace_root),
                )

    async def _prepare_prompt_permissions() -> None:
        async with permission_updates:
            await _flush_prompt_permissions()

    async def _flush_prompt_permissions() -> None:
        if conversation_process is not None and controller.conversation_id is not None:
            await conversation_process.call(
                "permissions", (controller.conversation_id, controller.branch_id),
                controller.conversation_id, permission_preset_draft[0],
                set(permission_custom_draft), str(tool_gateway.workspace_root),
            )

    if conversation_process is not None:
        controller.coordinator().before_prompt = _prepare_prompt_permissions

    async def _save_preset(conversation_id: str, preset: PermissionPreset) -> None:
        async with permission_updates:
            await run_blocking(permissions.set_preset, conversation_id, preset)

    def _on_permission_preset_changed(value: str) -> None:
        try:
            preset = PermissionPreset(value)
        except ValueError:
            return
        permission_preset_draft[0] = preset
        chat.set_permission_preset(preset)
        conversation_id = controller.conversation_id
        if conversation_id is not None:
            _spawn(_save_preset(conversation_id, preset))
        chat.set_status(f"会话权限已设为：{chat.permission_anchor.toolTip().split('：', 1)[-1]}")

    def _open_custom_permission_panel() -> None:
        _spawn(_prepare_custom_permission_panel())

    async def _prepare_custom_permission_panel() -> None:
        conversation_id = controller.conversation_id
        selected = (
            {grant.capability for grant in
             await run_blocking(permissions.list_grants, conversation_id)}
            if conversation_id is not None
            else set(permission_custom_draft)
        )

        def _apply(capabilities: set[Capability]) -> None:
            _spawn(_apply_custom(capabilities))

        async def _apply_custom(capabilities: set[Capability]) -> None:
            if conversation_id != controller.conversation_id:
                return
            permission_custom_draft.clear()
            permission_custom_draft.update(capabilities)
            permission_preset_draft[0] = PermissionPreset.CUSTOM
            if conversation_id is not None:
                async with permission_updates:
                    await run_blocking(
                        permissions.replace_custom_grants, conversation_id,
                        capabilities,
                        workspace_root=str(tool_gateway.workspace_root),
                    )
                    await run_blocking(
                        permissions.set_preset, conversation_id, PermissionPreset.CUSTOM
                    )
            chat.set_permission_preset(PermissionPreset.CUSTOM)
            chat.set_status("已应用自定义会话权限")

        open_custom_permissions(
            window,
            selected,
            _apply,
            anchor=chat.permission_anchor,
        )

    chat.permission_preset_changed.connect(_on_permission_preset_changed)
    chat.permission_custom_requested.connect(_open_custom_permission_panel)

    # 工具调用只读取已选档位，不在模型调用途中弹权限询问框。
    controller.set_permission_handler(
        _make_permission_handler(
            window,
            permissions,
            conversation_provider=lambda: controller.conversation_id,
            gateway=tool_gateway,
            preset_provider=permissions.get_preset,
            memory_service=memories,
            memory_context=lambda: controller.memory_context,
        )
    )

    # ---------- 左栏意图 ----------

    @dataclass
    class _LoadedHistory:
        branch_id: str
        messages: list[Message]
        payload: list[HistoryEntry]
        rendered: dict[str, str]
        attachment_ids: dict[str, tuple[str, ...]]
        attachments: dict[str, tuple[str, str, QImage | None] | None]
        preset: PermissionPreset
        capabilities: set[Capability]
        branches: list[tuple[str, str, int]]

    def _read_history_view(
        conversation_id: str, branch_id: str | None, cached_items: set[str],
        prepared: HistoryViewPayload | None = None,
    ) -> _LoadedHistory | None:
        data = prepared or load_history_view(uow_factory, conversation_id, branch_id)
        if data is None:
            return None
        branch_id, messages, payload = data.branch_id, data.messages, data.entries
        offset = max(0, len(payload) - HISTORY_PAGE)
        while (0 < offset < len(payload) and payload[offset].run_id is not None
               and payload[offset - 1].run_id == payload[offset].run_id):
            offset -= 1
        rendered = {
            text: markdown_render.render(text)
            for entry in payload[offset:] if entry.role == "assistant"
            for text in ([part.content for part in entry.segments]
                         if entry.segments else [entry.content])
        }
        # 一次读取所有用户消息的附件索引，避免每渲染一行都在 GUI 线程扫描/解密快照。
        ids = data.attachment_ids
        # 与消息窗口一样只解码当前页图片，避免长会话一次载入全部原图。
        visible_users = {
            entry.message_id for entry in payload[offset:] if entry.role == "user"
        }
        attachment_ids = {
            aid for message_id, values in ids.items() if message_id in visible_users
            for aid in values
        }
        items = {aid: _read_sent_attachment(aid) for aid in attachment_ids - cached_items}
        return _LoadedHistory(
            branch_id, messages, payload, rendered, ids, items,
            data.preset, data.capabilities, data.branches,
        )

    def _set_history_loading(loading: bool, text: str = "正在加载会话…") -> None:
        chat.set_history_loading(loading, text)
        sidebar.set_history_loading(loading)
        toolbar.setEnabled(not loading and preview_scope is None)
        if window.history_preview is not None:
            window.history_preview.set_history_loading(loading, text)

    async def _activate_preview(scope: tuple[str, str]) -> None:
        # A queued completion must not override a more recent sidebar selection.
        if preview_scope == scope and not controller.busy and not shutting_down:
            await _open_history(*scope, from_preview=True)

    async def _open_history(
        conversation_id: str, branch_id: str | None = None, *, from_preview: bool = False
    ) -> None:
        nonlocal preview_scope, detached_scope
        if chat.history_loading or chat.compressing or shutting_down:
            sidebar.set_active(*_display_scope())
            return
        if detached_scope is None and conversation_id == controller.conversation_id and (
            branch_id is None or branch_id == controller.branch_id
        ):
            # Return to the actual live widgets, including draft, tools and partial text.
            preview_scope = None
            detached_scope = None
            window.clear_history_preview()
            sidebar.set_active(*_display_scope())
            toolbar.setEnabled(True)
            return
        label = "分支" if branch_id is not None else "会话"
        _set_history_loading(True, f"正在加载{label}…")
        preview_loaded = False
        attempted_switch = False
        try:
            prepared = None
            if conversation_process is not None:
                prepared = await conversation_process.call(
                    "history_view", (conversation_id, branch_id), conversation_id, branch_id
                )
                if prepared is None:
                    (window.history_preview or chat).add_error(
                        f"加载{label}失败：会话不存在或没有可用分支"
                    )
                    return
            loaded = await history_reader.read(
                _read_history_view, conversation_id, branch_id, set(sent_items), prepared
            )
            if loaded is None:
                (window.history_preview or chat).add_error(
                    f"加载{label}失败：会话不存在或没有可用分支"
                )
                return
            # Cache attachments for both surfaces; never change active permission drafts
            # or kernel scope just to look at another conversation during generation.
            sent_ids_by_message.update(loaded.attachment_ids)
            sent_items.update({
                aid: _display_attachment(data) for aid, data in loaded.attachments.items()
            })
            if controller.busy:
                preview = window.prepare_history_preview()
                preview.set_attachment_resolver(_sent_attachments)
                preview.set_permission_preset(loaded.preset)
                preview.set_tool_steps_visible(not chat._tool_steps_hidden)
                await preview.load_history_incrementally(loaded.payload, rendered=loaded.rendered)
                preview_scope = (conversation_id, loaded.branch_id)
                sidebar.set_branches(conversation_id, loaded.branches)
                window.show_history_preview()
                preview_loaded = True
                return
            attempted_switch = True
            switched = await controller.switch_conversation(
                conversation_id, loaded.branch_id, known_has_messages=bool(loaded.messages)
            )
            if not switched:
                (window.history_preview or chat).add_error(
                    f"切换{label}失败：当前正忙或无法恢复历史上下文"
                )
                return
            permission_preset_draft[0] = loaded.preset
            permission_custom_draft.clear()
            permission_custom_draft.update(loaded.capabilities)
            chat.set_permission_preset(loaded.preset)
            sidebar.set_branches(conversation_id, loaded.branches)
            await chat.load_history_incrementally(loaded.payload, rendered=loaded.rendered)
            preview_scope = None
            detached_scope = None
            window.clear_history_preview()
            chat.set_status(f"已切换到历史{label}（{len(loaded.messages)} 条消息）")
            _refresh_agent_state()
            _spawn(_refresh_usage())
        finally:
            sidebar.set_active(*_display_scope())
            _set_history_loading(False)
            # Settlement may have arrived during either threaded reading or rendering.
            if (
                preview_scope is not None and not controller.busy
                and (preview_loaded or (not from_preview and not attempted_switch))
            ):
                _spawn(_activate_preview(preview_scope))

    async def _open_conversation(conversation_id: str) -> None:
        await _open_history(conversation_id)

    search_timer = QTimer(window)
    search_timer.setSingleShot(True)
    search_timer.setInterval(180)
    search_request: tuple[str, int, Any] = ("", 0, None)

    async def _search_history() -> None:
        from shiboken6 import isValid

        text, generation, results = search_request
        hits = await history_reader.read(history.search, text)
        if shutting_down or generation != history_list_generation:
            return
        rows = [(h.conversation.id, f"{h.conversation.title} — {h.snippet}") for h in hits]
        sidebar.show_search_results(rows)
        if results is not None and isValid(results):
            from PySide6.QtWidgets import QListWidgetItem

            results.clear()
            for conversation_id, label in rows:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, conversation_id)
                results.addItem(item)

    search_timer.timeout.connect(lambda: _spawn(_search_history()))

    def _on_search(text: str, results: Any = None) -> None:
        nonlocal history_list_generation, search_request
        history_list_generation += 1
        search_timer.stop()
        search_request = (text, history_list_generation, results)
        if results is not None:
            results.clear()
        if not text.strip():
            _refresh_conversations()
            return
        search_timer.start()

    def _open_search() -> None:
        """搜索悬浮窗：点🔍呼出，输入即搜，点结果打开会话。"""
        from PySide6.QtWidgets import QListWidget, QListWidgetItem

        panel = FloatingPanel(window, "搜索历史", width=460)
        edit = QLineEdit()
        edit.setPlaceholderText("搜标题与消息内容…")
        panel.content_layout.addWidget(edit)
        results = QListWidget()
        results.setMinimumHeight(260)
        panel.content_layout.addWidget(results, 1)

        def _refresh_hits(text: str) -> None:
            _on_search(text, results)

        def _open_hit(item: QListWidgetItem) -> None:
            conversation_id = item.data(Qt.ItemDataRole.UserRole)
            if conversation_id:
                panel.close_panel()
                _spawn(_open_conversation(str(conversation_id)))

        edit.textChanged.connect(_refresh_hits)
        results.itemClicked.connect(_open_hit)
        panel.popup()
        edit.setFocus()

    settings_page: SettingsPage | None = None

    def _open_settings(
        initial_tab: str | None = None, *, warm_only: bool = False
    ) -> None:
        nonlocal settings_page
        if credentials is None:
            chat.set_status("资料库未解锁：设置不可用")
            return
        if settings_page is not None:
            from shiboken6 import isValid

            if isValid(settings_page):
                if not warm_only:
                    settings_page.prepare_open(controller.conversation_id, initial_tab)
                    window.show_full_page(settings_page)
                return
            settings_page = None

        # Optional settings are created once, on demand, on the GUI thread.
        # Building hundreds of hidden controls must not delay chat readiness.
        from limbowave.ui.settings_panel import SettingsPage

        page = SettingsPage(
            window,
            settings,
            credentials,
            preferences=preferences,
            appearance_themes=appearance_themes,
            vault=Vault(paths.data_root / "vault.json"),
            request_logs=request_logs,
            conversation_id=controller.conversation_id,
            memories=memories,
            diagnostics_available=diagnostics_available,
            lan_panel=lan_access.panel(),
        )
        page.diagnostics_requested.connect(window.diagnostics_requested.emit)
        settings_page = page
        page.endpoints_tab.configuration_changed.connect(
            lambda: request_limiter.configure(settings.load().endpoints)
        )

        async def _discover_models(endpoint: Any) -> None:
            from shiboken6 import isValid

            from limbowave.application.services.model_probe import discover_models

            try:
                secret = credentials.resolve(endpoint.credential_ref)
            except Exception:
                secret = None
            result = await _run_probe(discover_models, endpoint, secret)
            if isValid(page):
                page.apply_model_discovery(endpoint, result)

        async def _probe_actual_model(task: Any) -> None:
            from shiboken6 import isValid

            from limbowave.application.services.model_probe import (
                ModelProbeProgress,
                probe_model_capabilities,
            )

            loop = asyncio.get_running_loop()

            def apply_progress(progress: ModelProbeProgress) -> None:
                if isValid(page):
                    page.apply_model_probe_progress(task.endpoint.id, task.model_id, progress)

            def report_progress(progress: ModelProbeProgress) -> None:
                # 探测在专用 executor 中运行；Qt 状态只在 GUI 事件循环里更新。
                loop.call_soon_threadsafe(apply_progress, progress)

            try:
                secret = credentials.resolve(task.endpoint.credential_ref)
            except Exception:
                secret = None
            try:
                result = await _run_probe(
                    partial(probe_model_capabilities, on_progress=report_progress),
                    task.endpoint, task.model_id, secret,
                )
            except Exception:
                if isValid(page):
                    page.show_model_probe_error(
                        task.endpoint.id, task.model_id, "检测异常，请重试或手动设置能力"
                    )
                return
            if not isValid(page):
                return
            if not result.alive:
                page.show_model_probe_error(task.endpoint.id, task.model_id, result.stream.detail)
                return
            try:
                current_endpoint = next(
                    (
                        endpoint
                        for endpoint in settings.load().endpoints
                        if endpoint.id == task.endpoint.id
                    ),
                    None,
                )
                if current_endpoint != task.endpoint:
                    raise ValueError("站点配置已变化，请重新检测")
                # 只写实际模型目录：导入实际模型不创建逻辑模型（逻辑模型由用户手动建立）
                settings.record_probed_model(
                    endpoint_id=task.endpoint.id,
                    model_id=task.model_id,
                    display_name=task.display_name,
                    default_thinking_level=result.default_thinking_level,
                    thinking_level_locked=result.thinking_level_locked,
                    available_thinking_levels=result.thinking.levels,
                    supports_thinking=(
                        result.thinking.supported or None
                        if result.thinking.inconclusive
                        else result.thinking.supported
                    ),
                    supports_tools=result.supports_tools,
                    supports_streaming=result.stream.alive,
                )
            except (ValueError, OSError) as exc:
                page.show_model_probe_error(task.endpoint.id, task.model_id, str(exc))
                return
            page.apply_model_probe(task.endpoint.id, result)

        def _save_unverified_model(task: Any) -> None:
            from shiboken6 import isValid

            if not isValid(page) or page.actual_models._endpoint != task.endpoint:
                return
            current_endpoint = next(
                (e for e in settings.load().endpoints if e.id == task.endpoint.id), None
            )
            if current_endpoint != task.endpoint:
                page.show_model_probe_error(
                    task.endpoint.id, task.model_id, "站点配置已变化，请重新保存"
                )
                return
            try:
                settings.record_unverified_model(
                    endpoint_id=task.endpoint.id,
                    model_id=task.model_id,
                    display_name=task.display_name,
                )
            except ValueError as exc:
                page.show_model_probe_error(task.endpoint.id, task.model_id, str(exc))
                return
            page.apply_manual_model_save(task.endpoint.id, task.model_id)

        def _save_model_capability(change: Any) -> None:
            from shiboken6 import isValid

            if not isValid(page) or page.actual_models._endpoint != change.endpoint:
                return
            try:
                current_endpoint = next(
                    (e for e in settings.load().endpoints if e.id == change.endpoint.id), None
                )
                if current_endpoint != change.endpoint:
                    raise ValueError("站点配置已变化，请重新保存")
                config, _ = settings.set_model_capability(
                    endpoint_id=change.endpoint.id,
                    model_id=change.model_id,
                    display_name=change.display_name,
                    capability=change.capability,
                    supported=change.supported,
                )
            except (ValueError, OSError) as exc:
                page.actual_models.show_capability_save_error(change, str(exc))
                return
            actual = config.actual_model(change.endpoint.id, change.model_id)
            if actual is not None:
                page.apply_model_capability_save(change, actual)

        page.model_discovery_requested.connect(lambda endpoint: _spawn(_discover_models(endpoint)))
        page.model_probe_requested.connect(lambda task: _spawn(_probe_actual_model(task)))
        page.manual_model_save_requested.connect(_save_unverified_model)
        page.model_capability_save_requested.connect(_save_model_capability)
        page.appearance_changed.connect(_apply_appearance)
        page.backup_requested.connect(_backup)
        page.restore_requested.connect(_restore)
        page.reset_requested.connect(_reset)

        def _close_settings() -> None:
            # 设置页可能增删改了逻辑模型：在工作区滑入前同步输入框的模型列表。
            _refresh_toolbar()
            window.show_workspace(release_page=False)
            # 站点/模型/密钥的改动热更新进正在运行的内核，不必重启
            _spawn(_on_config_changed())
            # Sidebar database/list work must not occupy the transition frames.
            QTimer.singleShot(230, _refresh_conversations)

        page.close_requested.connect(_close_settings)
        if warm_only:
            return
        page.prepare_open(controller.conversation_id, initial_tab)
        window.show_full_page(page)

    async def _on_new_conversation() -> None:
        nonlocal preview_scope, detached_scope
        if chat.history_loading:
            return
        if await controller.new_session():
            preview_scope = None
            detached_scope = None
            window.clear_history_preview()
            toolbar.setEnabled(True)
            chat.clear_transcript()
            chat.set_status("新会话")
            permission_preset_draft[0] = PermissionPreset.CHAT_ONLY
            permission_custom_draft.clear()
            chat.set_permission_preset(PermissionPreset.CHAT_ONLY)
        else:
            chat.set_status("未能新建会话：当前正忙，或内核上下文未成功重置")
        _refresh_branches()  # 左栏指示跟随（新会话落库前没有高亮行）
        _refresh_agent_state()

    def _on_rename(conversation_id: str) -> None:
        _spawn(_rename_conversation(conversation_id))

    async def _rename_conversation(conversation_id: str) -> None:
        current = next(
            (
                s.conversation.title
                for s in await history_reader.read(history.list_conversations)
                if s.conversation.id == conversation_id
            ),
            "",
        )

        def _do_rename(new_title: str) -> None:
            _spawn(_rename(new_title))

        async def _rename(new_title: str) -> None:
            await run_blocking(history.rename, conversation_id, new_title)
            _refresh_conversations()

        ask_prompt(
            window,
            "重命名会话",
            "新标题：",
            _do_rename,
            default_text=current,
        )

    def _on_delete(conversation_id: str) -> None:
        async def _delete() -> None:
            if controller.busy:
                chat.set_status("当前会话仍在进行中或正在切换，请稍后删除")
                return
            is_active = controller.conversation_id == conversation_id
            # 先结束旧轮并切走内核，失败时不删数据库，也不清屏。
            if is_active:
                if not await controller.new_session():
                    chat.set_status("删除失败：无法清空当前会话上下文")
                    return
            else:
                with controller.coordinator().runtime_transition():
                    await controller.wait_idle()
            if not await run_blocking(history.delete, conversation_id):
                return
            if is_active:
                chat.clear_transcript()
            _refresh_conversations()
            _refresh_agent_state()

        def _on_delete_answer(ok: bool) -> None:
            if ok:
                _spawn(_delete())

        ask_confirm(
            window,
            "删除会话",
            "删除后无法恢复：会话的消息、运行记录与请求日志都会删掉。",
            _on_delete_answer,
            confirm_text="删除",
            danger=True,
        )

    def _on_rename_branch(conversation_id: str, branch_id: str) -> None:
        _spawn(_rename_branch(conversation_id, branch_id))

    async def _rename_branch(conversation_id: str, branch_id: str) -> None:
        current = next(
            (
                label
                for candidate_id, label, _count in
                await history_reader.read(history.list_branches, conversation_id)
                if candidate_id == branch_id
            ),
            "",
        )

        def _do_rename(new_title: str) -> None:
            _spawn(_rename(new_title))

        async def _rename(new_title: str) -> None:
            if await run_blocking(history.rename_branch, branch_id, new_title):
                await _refresh_branches_async()

        ask_prompt(
            window,
            "重命名分支",
            "新标题：",
            _do_rename,
            default_text=current,
        )

    def _on_delete_branch(conversation_id: str, branch_id: str) -> None:
        _spawn(_prepare_delete_branch(conversation_id, branch_id))

    async def _prepare_delete_branch(conversation_id: str, branch_id: str) -> None:
        branches = await history_reader.read(history.list_branches, conversation_id)
        if len(branches) <= 1:
            chat.set_status("每个会话至少要保留一个分支")
            return
        branch_label = next(
            (label for candidate_id, label, _count in branches if candidate_id == branch_id),
            "这个分支",
        )

        async def _delete() -> None:
            fallback_id = next(
                candidate_id
                for candidate_id, _label, _count in branches
                if candidate_id != branch_id
            )
            is_active = (
                controller.conversation_id == conversation_id
                and controller.coordinator().branch_id == branch_id
            )
            if is_active and not await controller.switch_branch(fallback_id):
                chat.set_status("删除分支失败：无法切换到保留分支")
                return
            if not await run_blocking(history.delete_branch, branch_id):
                chat.set_status("删除分支失败：分支不存在或是唯一分支")
                return
            if controller.conversation_id == conversation_id:
                active_branch_id = controller.coordinator().branch_id
                if active_branch_id is not None:
                    _show_branch(active_branch_id, animate=False)
            _refresh_conversations()
            chat.set_status(f"已删除分支：{branch_label}")

        def _on_delete_answer(ok: bool) -> None:
            if ok:
                _spawn(_delete())

        ask_confirm(
            window,
            "删除分支",
            f"删除「{branch_label}」后无法恢复，其中的消息与运行记录都会删掉。",
            _on_delete_answer,
            confirm_text="删除",
            danger=True,
        )

    sidebar.conversation_selected.connect(lambda cid: _spawn(_open_conversation(cid)))
    sidebar.new_conversation_requested.connect(lambda: _spawn(_on_new_conversation()))
    sidebar.rename_requested.connect(_on_rename)
    sidebar.delete_requested.connect(_on_delete)
    sidebar.branch_rename_requested.connect(_on_rename_branch)
    sidebar.branch_delete_requested.connect(_on_delete_branch)
    sidebar.search_requested.connect(_open_search)
    sidebar.settings_requested.connect(_open_settings)

    # ---------- 导出与备份（Phase 8） ----------

    maintenance_lock = asyncio.Lock()

    async def _maintenance(work: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        async with maintenance_lock:
            return await run_blocking(work, *args, **kwargs)

    def _export_branch() -> None:
        _spawn(_prepare_export())

    async def _prepare_export() -> None:
        """导出悬浮窗：多选会话/分支 + 时间筛选 + 格式（md/txt/json/html）。"""
        from limbowave.ui.export_panel import ExportPanel

        def load_rows() -> list[Any]:
            return [
                (summary.conversation.id, summary.conversation.title,
                 [(bid, label) for bid, label, _ in
                  history.list_branches(summary.conversation.id)], summary.last_activity)
                for summary in history.list_conversations()
            ]

        rows = await history_reader.read(load_rows)
        if not rows:
            chat.set_status("还没有可导出的会话：先聊一轮")
            return

        from limbowave.application.services.export_service import (
            ExportService,
            privacy_notice,
        )
        from limbowave.infrastructure.crypto.blob_store import BlobStore

        def _run_export(
            branch_ids: list[str], _labels: list[str], fmt: str, target: Path,
            include_model_info: bool,
        ) -> None:
            def _proceed(ok: bool) -> None:
                _spawn(_export(ok))

            async def _export(ok: bool) -> None:
                if not ok:
                    chat.set_status("已取消导出")
                    return
                blob_store = BlobStore(paths.data_root / "blobs", key) if key is not None else None
                try:
                    config = settings.load()
                    exporter = ExportService(
                        uow_factory, blob_store,
                        include_model_info=include_model_info,
                        model_names={model.id: model.name for model in config.models},
                        endpoint_names={
                            endpoint.id: endpoint.name for endpoint in config.endpoints
                        },
                    )
                    chat.set_status("正在导出…")
                    outputs = await _maintenance(exporter.export_selection, branch_ids, fmt, target)
                except Exception as exc:
                    chat.add_error(f"导出失败：{exc}")
                    return
                destination = str(outputs[0]) if len(outputs) == 1 else str(target.parent)
                chat.set_status(f"已导出 {len(outputs)} 个分支到 {destination}")

            ask_confirm(
                window, "导出前请确认", privacy_notice(include_model_info=include_model_info),
                _proceed, confirm_text="导出",
            )

        ExportPanel(
            window, rows, default_dir=str(Path.home()),
            current_branch_id=controller.coordinator().branch_id, on_export=_run_export,
        )

    def _backup() -> None:
        """备份：悬浮窗两步输入密码（设置+确认），目录用系统目录选择器选。"""
        from limbowave import __version__
        from limbowave.application.services.backup_service import BackupService
        from limbowave.infrastructure.database.migrations import CURRENT_VERSION

        if key is None:
            chat.set_status("资料库未解锁：无法备份")
            return

        service = BackupService(
            paths.data_root, app_version=__version__, schema_version=CURRENT_VERSION
        )
        state: dict[str, str] = {}

        def _ask_dir(password: str) -> None:
            from PySide6.QtWidgets import QFileDialog

            target, _ = QFileDialog.getSaveFileName(
                window, "保存加密备份", "limbowave-backup.lwb", "LimboWave 备份 (*.lwb)"
            )
            if not target:
                return
            _spawn(_create_backup(Path(target), password))

        async def _create_backup(target: Path, password: str) -> None:
            chat.set_status("正在创建加密备份…")
            try:
                result = await _maintenance(service.create, target, password)
            except Exception as exc:
                chat.add_error(f"备份失败：{exc}")
                return
            size_kb = result.manifest.total_bytes() / 1024
            chat.set_status(f"备份完成：{result.path}（{size_kb:.1f} KB）")

        def _on_confirm(confirm: str) -> None:
            if confirm == state["password"]:
                _ask_dir(state["password"])
            else:
                ask_alert(window, "密码不一致", "两次输入不一致，已取消备份。")

        def _on_password(password: str) -> None:
            state["password"] = password
            ask_prompt(
                window,
                "确认备份密码",
                "再输入一次：",
                _on_confirm,
                password=True,
            )

        ask_prompt(
            window,
            "设置备份密码",
            "备份使用独立密码（与资料库主密码无关，用于在新设备上恢复）：",
            _on_password,
            password=True,
        )

    def _restore() -> None:
        """恢复：选文件 → 输密码 → 悬浮窗确认 → 校验后停在临时目录等重启。"""
        from limbowave import __version__
        from limbowave.application.services.backup_service import BackupService
        from limbowave.infrastructure.database.migrations import CURRENT_VERSION

        service = BackupService(
            paths.data_root, app_version=__version__, schema_version=CURRENT_VERSION
        )

        def _run(password: str, source: Path) -> None:
            _spawn(_inspect_backup(password, source))

        async def _inspect_backup(password: str, source: Path) -> None:
            chat.set_status("正在校验备份…")
            try:
                inspection = await _maintenance(service.inspect, source, password)
            except Exception as exc:
                chat.add_error(f"无法读取备份：{exc}")
                return

            def _do_restore(ok: bool) -> None:
                _spawn(_restore_backup(ok))

            async def _restore_backup(ok: bool) -> None:
                if not ok:
                    return
                import tempfile

                staging = Path(tempfile.mkdtemp(prefix="limbowave-restore-"))
                try:
                    await _maintenance(service.restore_to, source, password, staging)
                except Exception as exc:
                    chat.add_error(f"恢复未完成（当前资料库未改动）：{exc}")
                    return
                chat.set_status(
                    f"备份已校验并解到临时目录：{staging}"
                    + NL
                    + "切换资料库需要重启应用——请先关闭应用，用该目录内容替换数据目录。"
                )

            ask_confirm(
                window,
                "确认恢复",
                inspection.summary()
                + NL
                + NL
                + "恢复会覆盖当前资料库。备份已通过完整性校验，"
                + "但请确认当前内容不再需要（或已另行备份）。"
                + NL
                + NL
                + "继续恢复？",
                _do_restore,
                confirm_text="恢复",
                danger=True,
            )

        def _ask_password_for(source: Path) -> None:
            ask_prompt(
                window,
                "备份密码",
                "输入该备份的密码：",
                lambda password: _run(password, source),
                password=True,
            )

        def _pick_file() -> None:
            from PySide6.QtWidgets import QFileDialog

            source, _ = QFileDialog.getOpenFileName(
                window, "选择备份文件", "", "LimboWave 备份 (*.lwb)"
            )
            if source:
                _ask_password_for(Path(source))

        _pick_file()

    reset_panel: DataResetPanel | None = None

    def _reset() -> None:
        nonlocal reset_panel
        from shiboken6 import isValid

        if key is None or on_reset is None or resetting or shutting_down:
            return
        if reset_panel is not None and isValid(reset_panel) and not reset_panel._closing:
            reset_panel.raise_()
            return
        if settings_page is not None and not settings_page.resolve_pending_changes():
            return

        def _confirmed(service: DataResetService) -> None:
            nonlocal resetting
            resetting = True
            lan_access.invalidate()
            if on_reset is not None:
                on_reset(service)

        reset_panel = _open_data_reset(window, paths.data_root, _spawn, _confirmed)

    # ---------- 分支动作（Task 3.2） ----------

    def _refresh_branches() -> None:
        """刷新左栏的当前会话指示与其分支子行（双击会话行展开）。"""
        sidebar.set_active(*_display_scope())
        if _display_scope()[0] is not None:
            _spawn(_refresh_branches_async())

    async def _refresh_branches_async() -> None:
        """切换热路径专用：分支计数会读消息，不能在 GUI 线程同步刷新。"""
        conversation_id, branch_id = _display_scope()
        branches = (
            await history_reader.read(history.list_branches, conversation_id)
            if conversation_id is not None
            else []
        )
        if _display_scope() != (conversation_id, branch_id):
            return
        if conversation_id is not None:
            sidebar.set_branches(conversation_id, branches)
        sidebar.set_active(conversation_id, branch_id)

    async def _load_branch_payload(branch_id: str) -> list[HistoryEntry]:
        if conversation_process is not None:
            result: list[HistoryEntry] = await conversation_process.call(
                "history_payload", (None, branch_id), branch_id
            )
            return result
        return await history_reader.read(lambda: _history_payload(
            history.branch_messages(branch_id), uow_factory=uow_factory, branch_id=branch_id
        ))

    def _current_messages() -> list[Message]:
        """当前定位分支的完整对话（含分叉前继承的前缀）。未定位返回空。"""
        branch_id = controller.coordinator().branch_id
        return history.branch_messages(branch_id) if branch_id is not None else []

    def _show_branch(branch_id: str, *, animate: bool = True) -> None:
        """把消息区重绘成指定分支的完整对话。

        编辑/重生成产生新分支时，``branched`` 后会在同一调用链里立刻发出
        ``user`` 事件。此时不能使用异步的渐隐/渐显切换：否则新用户气泡先追加，
        动画完成后又被分叉点快照覆盖。只有用户主动切换分支时才播放动画。
        """
        async def show() -> None:
            scope = _display_scope()
            payload = await _load_branch_payload(branch_id)
            if scope == _display_scope():
                await chat.load_history_incrementally(payload)

        _spawn(show())

    async def _prepare_edit(message_id: str) -> None:
        if detached_scope is not None:
            chat.set_status("请先在侧栏重新打开当前会话，再进行消息操作。")
            return
        scope, generation = _display_scope(), chat.attachments.generation

        def read_edit() -> tuple[Any, dict[str, Any]]:
            with uow_factory() as uow:
                message = uow.messages.get(message_id)
            ids = _read_sent_attachment_ids(message_id)
            return message, {aid: _read_sent_attachment(aid) for aid in ids}

        message, data = await history_reader.read(read_edit)
        if scope != _display_scope() or generation != chat.attachments.generation:
            return
        if message is None:
            chat.set_status("找不到要编辑的消息")
            return
        chat.cancel_edit()
        chat.attachments.clear()
        for aid, item in data.items():
            _add_imported_attachment(aid, item)
        chat.begin_edit(message_id, message.content)

    def _on_edit_message(message_id: str) -> None:
        if not chat.history_loading:
            _spawn(_prepare_edit(message_id))

    def _on_edit_submitted(message_id: str, new_text: str) -> None:
        if detached_scope is not None:
            chat.set_status("请先在侧栏重新打开当前会话，再进行消息操作。")
            return
        nonlocal send_preparing
        if chat.history_loading or send_preparing:
            return
        send_preparing = True
        _spawn(_prepare_attachment_send(
            new_text, chat.attachments.attachment_ids(), _display_scope(), message_id,
            draft_generation=chat.attachments.generation,
        ))

    def _on_fork(message_id: str) -> None:
        if detached_scope is not None:
            chat.set_status("请先在侧栏重新打开当前会话，再进行消息操作。")
            return
        if chat.history_loading:
            return
        _spawn(controller.fork_message(message_id))

    regenerate_pending = False

    def _on_regenerate(message_id: str) -> None:
        if detached_scope is not None:
            chat.set_status("请先在侧栏重新打开当前会话，再进行消息操作。")
            return
        nonlocal regenerate_pending
        if chat.history_loading or controller.busy or regenerate_pending:
            return
        regenerate_pending = True
        chat.set_branch_actions_enabled(False)
        chat.set_status("正在准备重新生成…")

        async def regenerate() -> None:
            nonlocal regenerate_pending
            try:
                await controller.regenerate(message_id)
            finally:
                regenerate_pending = False
                if not shutting_down:
                    chat.set_branch_actions_enabled(True)

        _spawn(regenerate())

    def _on_retry(message_id: str) -> None:
        if detached_scope is not None:
            chat.set_status("请先在侧栏重新打开当前会话，再进行消息操作。")
            return
        if chat.history_loading:
            return
        _spawn(controller.retry_user_message(message_id))

    def _on_switch_branch(conversation_id: str, branch_id: str) -> None:
        _spawn(_open_history(conversation_id, branch_id))

    async def _load_branches(conversation_id: str) -> None:
        if sidebar.branches_loading(conversation_id):
            return
        sidebar.set_branches_loading(conversation_id, True)
        try:
            branches = await history_reader.read(history.list_branches, conversation_id)
            sidebar.set_branches(conversation_id, branches)
        finally:
            sidebar.set_branches_loading(conversation_id, False)

    chat.edit_message_requested.connect(_on_edit_message)
    chat.edit_submitted.connect(_on_edit_submitted)
    chat.fork_requested.connect(_on_fork)
    chat.regenerate_requested.connect(_on_regenerate)
    chat.retry_requested.connect(_on_retry)
    sidebar.branch_switch_requested.connect(_on_switch_branch)
    sidebar.branches_requested.connect(
        lambda cid: _spawn(_load_branches(cid))
    )

    # ---------- 工具步骤（§三.2） ----------

    def _on_toggle_tool_steps() -> None:
        """一键隐藏/显示工具步骤（只影响展示，审计数据不删）。"""
        hidden = not chat._tool_steps_hidden
        chat.set_tool_steps_visible(not hidden)
        toolbar.set_tool_steps_hidden(hidden)
        toolbar.show_steps_notice("已隐藏工具步骤" if hidden else "已显示工具步骤")

    chat.tool_steps_toggle_requested.connect(_on_toggle_tool_steps)

    def _refresh_agent_state() -> None:
        """把当前会话的 Agent 状态回显到工具栏（会话隔离的可见面）。"""
        toolbar.set_execution_mode(
            chat.execution_mode, is_fallback_shell=bool(shell_env and shell_env.is_fallback)
        )
        toolbar.set_tool_steps_hidden(chat._tool_steps_hidden)

    def _open_memory() -> None:
        from limbowave.ui.session_memory_panel import MemoryPanel

        conversation_id, branch_id = controller.conversation_id, controller.branch_id
        if conversation_id is None or branch_id is None:
            chat.set_status("请先开始一个会话；全局记忆可在设置中管理")
            return
        panel = FloatingPanel(window, "会话记忆", width=620)
        viewer = MemoryPanel(memories, panel, conversation_id=conversation_id, branch_id=branch_id)
        panel.content_layout.addWidget(viewer, 1)

        def _scope_changed(_event: Any) -> None:
            if (controller.conversation_id, controller.branch_id) != (conversation_id, branch_id):
                panel.close_panel()
            elif _event.kind == "settled" and not viewer.is_editing:
                viewer.reload()

        unsubscribe = controller.subscribe(_scope_changed)
        panel.closed.connect(unsubscribe)
        panel.popup()

    def _open_permissions() -> None:
        """查看/撤销/缩小会话授权（§11.1）。"""
        from limbowave.ui.permissions_dialog import PermissionsDialog

        conversation_id = controller.conversation_id
        if conversation_id is None:
            chat.set_status("还没有会话：授权按会话保存，先聊一轮")
            return
        # 权限查看器以**内嵌部件**进悬浮窗（不再是一扇系统窗口）
        panel = FloatingPanel(window, "会话权限", width=560)
        viewer = PermissionsDialog(permissions, conversation_id, panel)
        viewer.setWindowFlags(Qt.WindowType.Widget)
        panel.content_layout.addWidget(viewer, 1)
        panel.popup()

    def _toggle_execution_mode() -> None:
        """切换内置工具 ↔ 直接终端模式（§9.1：会话必须清晰显示当前模式）。"""
        current = chat.execution_mode
        target = "builtin" if current == "terminal" else "terminal"
        # 工具能力是**站点-模型级**声明（§二.4）：模型不支持工具调用时，
        # 两种工具模式都没有意义——明确告知，而不是切过去发现什么都不发生。
        if setup is not None and not setup.supports_tools:
            toolbar.show_mode_notice("模型未声明支持工具调用")
            return
        if target == "terminal" and shell_env is None:
            toolbar.show_mode_notice("本机未找到可用 Shell")
            return
        chat.set_execution_mode(target)
        is_fallback = bool(shell_env and shell_env.is_fallback)
        toolbar.set_execution_mode(target, is_fallback_shell=is_fallback)
        if target == "terminal":
            # §10.1：pwsh 缺失回退到 Windows PowerShell 5.1 时必须明示
            toolbar.show_mode_notice("终端模式（Powershell5.1兼容）" if is_fallback else "终端模式")
        else:
            toolbar.show_mode_notice("内置工具模式")

    # ---------- 会话工具栏与二级抽屉（Task 8.2/8.3） ----------

    toolbar = window.toolbar

    def _refresh_toolbar() -> None:
        """刷新工具栏：模型、实际站点、占用、思考强度、路由候选。"""
        config = settings.load()
        # 下拉框展示显示名，itemData 仍用 id（会话目标保持稳定标识）
        model_entries = [(model.id, model.name) for model in config.models]
        endpoint_names = {endpoint.id: endpoint.name for endpoint in config.endpoints}
        model_sites = {
            model.id: tuple(
                ModelSite(
                    binding.endpoint_id,
                    endpoint_names.get(binding.endpoint_id, binding.endpoint_id),
                    binding.model_id,
                    default=index == 0,
                )
                for index, binding in enumerate(model.bindings)
            )
            for model in config.models
        }
        if setup is None:
            toolbar.set_model_info("—", "—")
            chat.set_logical_models(model_entries, sites=model_sites)
            toolbar.set_session_available(False)
            _sync_toolbar_route_candidates()
            return
        # 思考强度是内核级状态（Pi set_thinking_level），不依赖已落库的会话：
        # 空会话（首条消息前）也要能调，首轮发送会沿用内核当前等级。
        toolbar.set_session_available(controller.coordinator().kernel is not None)
        toolbar.set_thinking_capability(
            setup.supports_thinking,
            locked_level=(setup.default_thinking_level if setup.thinking_level_locked else None),
            available_levels=setup.available_thinking_levels,
            runtime_levels=setup.runtime_thinking_levels or ("off",),
        )
        endpoint = session_endpoint_override["endpoint"] or setup.endpoint_id
        # 站点展示用显示名（EndpointConfig.name），id 只作会话目标标识
        endpoint_label = next(
            (e.name for e in config.endpoints if e.id == endpoint), endpoint
        )
        suffix = "（会话覆盖）" if session_endpoint_override["endpoint"] else ""
        toolbar.set_model_info(setup.logical_model_id, f"{endpoint_label}{suffix}")
        chat.set_logical_models(
            model_entries, setup.logical_model_id, sites=model_sites,
            current_endpoint=endpoint, overridden=bool(session_endpoint_override["endpoint"]),
        )
        _sync_toolbar_route_candidates()

    # 模型目录热更新：内核启动后配置可能变了（新增站点/模型、换密钥）。
    # 上次同步的目录摘要——内容没变就不打扰 Pi。
    catalog_state: dict[str, str | None] = {"fingerprint": None}

    async def _sync_model_catalog() -> bool:
        """把当前配置同步进正在运行的 Pi（重写 models.json + 进程内密钥/规则）。

        返回是否同步成功；失败时已给出提示。无内核/未解锁时直接返回 False。
        """
        kernel = controller.coordinator().kernel
        if kernel is None or credentials is None:
            return False
        from limbowave.application.services.routing_service import RoutingService
        from limbowave.composition import resolve_catalog
        from limbowave.infrastructure.pi_runtime.environment_builder import EnvironmentBuilder

        temporary_levels: dict[tuple[str, str], tuple[str, ...]] = (
            {(thinking_trial.endpoint_id, thinking_trial.model_id): (thinking_trial.level,)}
            if thinking_trial is not None else {}
        )

        def prepare_catalog() -> Any:
            catalog = resolve_catalog(RoutingService(settings.load()), credentials)
            return EnvironmentBuilder(paths.data_root / "runtime").refresh_catalog(
                catalog, temporary_thinking_levels=temporary_levels
            )

        sync = await run_blocking(prepare_catalog)
        if sync.fingerprint == catalog_state["fingerprint"]:
            return True
        try:
            await kernel.reload_models(sync.env, sync.registered)
        except Exception as exc:
            chat.add_error(f"模型目录热更新失败：{redact_text(str(exc))}")
            return False
        catalog_state["fingerprint"] = sync.fingerprint
        return True

    async def _send_with_catalog_sync(text: str, **send_kwargs: Any) -> bool:
        """Reserve target restoration, model sync and acceptance as one transition."""
        nonlocal detached_scope
        from limbowave.application.services.run_coordinator import CommandBusy

        if chat.history_loading:
            return False
        try:
            with controller.coordinator().command_scope():
                if controller.busy:
                    chat.set_status("已有设备正在生成，输入已保留。")
                    return False
                if detached_scope is not None:
                    conversation_id, branch_id = detached_scope
                    if conversation_id is None or branch_id is None:
                        restored = await controller.new_session()
                    else:
                        restored = await controller.switch_conversation(conversation_id, branch_id)
                    if not restored:
                        return False
                    detached_scope = None
                    toolbar.setEnabled(True)
                if setup is not None:
                    with controller.coordinator().runtime_transition():
                        if not await _sync_model_catalog():
                            return False
                return await controller.send(text, **send_kwargs) is not None
        except CommandBusy:
            chat.set_status("另一个设备正在提交，输入已保留。")
            return False

    async def _on_config_changed() -> None:
        """设置页关闭后：热更新模型目录，并让当前运行上下文跟上新配置。"""
        nonlocal setup
        request_limiter.configure(settings.load().endpoints)
        if controller.busy:
            return
        with controller.coordinator().runtime_transition():
            _clear_thinking_trial()
            if setup is None or not await _sync_model_catalog():
                return
            from limbowave.application.services.routing_service import RoutingService
            from limbowave.composition import kernel_setup

            current = next(
                (
                    d
                    for d in RoutingService(settings.load()).catalog()
                    if d.model.id == setup.logical_model_id and d.endpoint.id == setup.endpoint_id
                ),
                None,
            )
            if current is None:
                chat.set_status("当前模型或站点已从配置中移除，请重新选择模型")
                return
            kernel = controller.coordinator().kernel
            assert kernel is not None
            # 只刷新能力与参数，不改路由：保留原路由理由
            setup = replace(kernel_setup(kernel, current), routing_reason=setup.routing_reason)
            setup_state["value"] = setup
            _refresh_toolbar()
            await _refresh_thinking_level()

    async def _apply_logical_model(model_id: str, endpoint_id: str | None = None) -> None:
        """一次切换逻辑模型及可选站点；空串明确恢复默认，None 保留普通点击语义。"""
        nonlocal setup
        if setup is None or controller.coordinator().kernel is None:
            chat.set_status("未配置内核：无法切换逻辑模型")
            _refresh_toolbar()
            return
        if controller.busy:
            chat.set_status("回复生成中，暂不能切换逻辑模型")
            _refresh_toolbar()
            return
        with controller.coordinator().runtime_transition():
            if model_id == setup.logical_model_id and endpoint_id is None:
                return

            from limbowave.application.services.routing_service import RoutingService
            from limbowave.composition import kernel_setup
            from limbowave.domain.routing import RoutingError

            try:
                decision = RoutingService(settings.load()).route(
                    model_id, endpoint_id=endpoint_id or None
                )
            except RoutingError as exc:
                chat.set_status(str(exc))
                _refresh_toolbar()
                return

            # 试用只属于当前实际模型；切换前清理运行时覆盖。
            _clear_thinking_trial()
            # 目标可能是内核启动后才加的：先把目录同步进 Pi
            if not await _sync_model_catalog():
                _refresh_toolbar()
                return
            kernel = controller.coordinator().kernel
            assert kernel is not None
            try:
                await kernel.set_model(decision.endpoint.id, decision.model_id)
            except Exception as exc:
                chat.add_error(f"切换逻辑模型失败：{exc}")
                _refresh_toolbar()
                return

            setup = kernel_setup(kernel, decision)
            setup_state["value"] = setup
            session_endpoint_override["endpoint"] = endpoint_id or None
            _refresh_toolbar()
            await _apply_binding_defaults()
            chat.set_status(
                f"已切换逻辑模型：{decision.model.id} → "
                f"{decision.endpoint.id} / {decision.model_id}"
            )

    async def _refresh_thinking_level() -> None:
        nonlocal setup
        kernel = controller.coordinator().kernel
        if kernel is None or setup is None:
            return
        try:
            levels = await kernel.get_available_thinking_levels()
            state = await kernel.get_state()
        except Exception as exc:
            setup = replace(setup, runtime_thinking_levels=None)
            setup_state["value"] = setup
            _refresh_toolbar()
            chat.set_status(f"读取思考强度失败：{redact_text(str(exc))}")
            return
        setup = replace(setup, runtime_thinking_levels=levels)
        setup_state["value"] = setup
        thinking_state["level"] = state.thinking_level
        _refresh_toolbar()
        toolbar.set_thinking_level(state.thinking_level)

    async def _apply_binding_defaults() -> None:
        """应用**站点-模型级**的默认思考强度（§二.4 模型级覆盖）。

        放在启动时做一次：某个站点上的某个模型可能只支持特定思考等级
        （中转站常常只认后缀形式），用户配了默认值就按它来。
        """
        if setup is None:
            return
        kernel = controller.coordinator().kernel
        if kernel is None:
            return
        level = _run_context(setup, thinking=thinking_state)().thinking_level
        if level is None:
            level = setup.default_thinking_level
        try:
            if level is not None:
                await kernel.set_thinking_level(level)
        except Exception as exc:
            chat.set_status(f"应用思考强度失败：{exc}")
        await _refresh_thinking_level()

    def _clear_thinking_trial() -> None:
        nonlocal setup, thinking_trial
        previous, thinking_trial = thinking_trial, None
        if previous is not None and setup is not None and setup.thinking_trial_id == previous.id:
            setup = previous.base_setup
            setup_state["value"] = setup
            thinking_state["level"] = previous.previous_level

    async def _on_force_thinking_level(level: str) -> None:
        nonlocal setup, thinking_trial
        from limbowave.ui.session_toolbar import THINKING_LEVELS

        kernel = controller.coordinator().kernel
        if setup is None or kernel is None or controller.busy or level not in THINKING_LEVELS:
            chat.set_status("当前无法试用思考等级，请等待请求或模型切换完成")
            return
        with controller.coordinator().runtime_transition():
            _clear_thinking_trial()
            assert setup is not None
            trial = ThinkingTrial(
                setup, str(setup.app_params.get("model", "")), level,
                thinking_state["level"] or "off",
            )
            thinking_trial = trial
            original_levels = tuple(
                candidate for candidate in (setup.runtime_thinking_levels or ("off",))
                if (setup.supports_thinking is not False or candidate == "off")
                and (not setup.thinking_level_locked or candidate == setup.default_thinking_level)
                and (not setup.available_thinking_levels or candidate == "off"
                     or candidate in setup.available_thinking_levels)
            )
            setup = replace(
                setup, supports_thinking=True, thinking_level_locked=False,
                thinking_trial_id=trial.id,
                available_thinking_levels=tuple(dict.fromkeys((
                    *original_levels, level,
                ))),
                runtime_thinking_levels=None,
            )
            setup_state["value"] = setup
            try:
                if not await _sync_model_catalog():
                    raise RuntimeError("模型目录未能应用临时覆盖")
                await kernel.set_thinking_level(level)
                state = await kernel.get_state()
                if state.thinking_level != level:
                    raise RuntimeError(f"运行时仍返回 {state.thinking_level}，未解锁 {level}")
                thinking_state["level"] = level
                await _refresh_thinking_level()
            except Exception as exc:
                _clear_thinking_trial()
                try:
                    if not await _sync_model_catalog():
                        raise RuntimeError("恢复模型目录失败")
                    await kernel.set_thinking_level(trial.previous_level)
                    await _refresh_thinking_level()
                except Exception as rollback_error:
                    if setup is not None:
                        setup = replace(setup, runtime_thinking_levels=None)
                        setup_state["value"] = setup
                        _refresh_toolbar()
                    chat.add_error(f"恢复思考设置失败：{redact_text(str(rollback_error))}")
                chat.add_error(f"临时解锁失败：{redact_text(str(exc))}")
                return
        chat.set_status(f"已临时解锁 {level}；发送成功后可选择标记支持，切换模型或等级后失效")

    async def _confirm_thinking_trial(trial: ThinkingTrial) -> None:
        try:
            config = settings.confirm_thinking_level(trial.endpoint_id, trial.model_id, trial.level)
            if thinking_trial is trial:
                from limbowave.application.services.routing_service import RoutingService
                from limbowave.composition import kernel_setup

                current = next((
                    decision for decision in RoutingService(config).catalog()
                    if decision.endpoint.id == trial.endpoint_id
                    and decision.model_id == trial.model_id
                ), None)
                if current is not None:
                    # 用户可能在下一轮进行中确认；取消试用时也应恢复最新保存的能力。
                    trial.base_setup = kernel_setup(trial.base_setup.kernel, current)
        except Exception as exc:
            chat.add_error(f"标记思考等级失败：{redact_text(str(exc))}")
            return
        if not controller.busy:
            await _on_config_changed()
        chat.set_status(f"已标记支持：{trial.model_id} · {trial.level}")

    def _offer_thinking_trial(result: dict[str, Any]) -> None:
        trial = thinking_trial
        if trial is None or not trial.accept_success(result):
            return
        endpoint_name = next(
            (e.name for e in settings.load().endpoints if e.id == trial.endpoint_id),
            trial.endpoint_id,
        )
        ask_confirm(
            window,
            "将此思考等级标记为支持？",
            f"{endpoint_name} / {trial.model_id} 使用 {trial.level} 的请求已成功完成。"
            "\n请求成功不代表服务端确实采用了该思考强度，接口也可能忽略参数。"
            "\n确认后只保存这个站点、实际模型与等级；暂不保存则仅保留临时解锁。",
            lambda accepted: _spawn(_confirm_thinking_trial(trial)) if accepted else None,
            confirm_text="标记为支持", cancel_text="暂不保存",
        )

    async def _on_thinking_level(level: str) -> None:
        if setup is not None and setup.supports_thinking is False:
            chat.set_status("当前实际模型不支持思考")
            return
        if (
            setup is not None
            and setup.thinking_level_locked
            and level != setup.default_thinking_level
        ):
            chat.set_status(
                f"当前实际模型固定使用 {setup.default_thinking_level or '指定'} 思考等级"
            )
            toolbar.set_thinking_level(setup.default_thinking_level or "off")
            return
        kernel = controller.coordinator().kernel
        if kernel is None:
            chat.set_status("未配置内核：无法设置思考强度")
            return
        if controller.busy:
            chat.set_status("请求或模型切换中，暂不能修改思考强度")
            toolbar.set_thinking_level(thinking_state["level"] or "off")
            return
        try:
            with controller.coordinator().runtime_transition():
                if thinking_trial is not None:
                    _clear_thinking_trial()
                    if not await _sync_model_catalog():
                        await _refresh_thinking_level()
                        return
                levels = await kernel.get_available_thinking_levels()
                if levels is None or level not in levels:
                    await _refresh_thinking_level()
                    chat.set_status(f"当前运行时未开放思考等级：{level}")
                    return
                await kernel.set_thinking_level(level)
                state = await kernel.get_state()
                thinking_state["level"] = state.thinking_level
                toolbar.set_thinking_level(state.thinking_level)
        except Exception as exc:
            toolbar.set_thinking_level(thinking_state["level"] or "off")
            chat.add_error(f"设置思考强度失败：{exc}")
            return
        await _refresh_thinking_level()
        if state.thinking_level != level:
            chat.set_status(f"运行时未接受 {level}，实际思考强度：{state.thinking_level}")
        else:
            chat.set_status(f"思考强度：{state.thinking_level}")

    def _route_candidates() -> list[RouteCandidate] | str:
        """路由候选（§四.3）：行序即回退顺序，行可点选做会话级覆盖；**不自动回退**。

        返回状态字符串（「未配置模型」等）时抽屉只显示说明、不出行。
        覆盖激活时默认绑定行换成「恢复默认」（外发空串清除覆盖），当前行禁用。
        展示一律用站点显示名（EndpointConfig.name），id 只作选中值。
        """
        if setup is None:
            return "（未配置模型）"
        config = settings.load()
        model = next((m for m in config.models if m.id == setup.logical_model_id), None)
        if model is None:
            return "（模型已不在配置里）"
        endpoint_names = {e.id: e.name for e in config.endpoints}

        def _name(endpoint_id: str) -> str:
            return endpoint_names.get(endpoint_id, endpoint_id)

        override = session_endpoint_override["endpoint"]
        candidates: list[RouteCandidate] = []
        for index, binding in enumerate(model.bindings):
            is_current = binding.endpoint_id == (override or setup.endpoint_id)
            label = "默认" if index == 0 else f"备用 {index}"
            if override and index == 0:
                # 默认绑定没被覆盖时，恢复默认=切回默认站点，与切换无区别；
                # 只有覆盖激活时才提供这条出口（外发空串清除覆盖）。
                candidates.append(
                    RouteCandidate(
                        binding.endpoint_id,
                        _name(binding.endpoint_id),
                        binding.model_id,
                        label,
                        restore=True,
                    )
                )
            elif is_current:
                candidates.append(
                    RouteCandidate(
                        binding.endpoint_id,
                        _name(binding.endpoint_id),
                        binding.model_id,
                        label,
                        current=True,
                        override=bool(override),
                    )
                )
            else:
                candidates.append(
                    RouteCandidate(
                        binding.endpoint_id, _name(binding.endpoint_id), binding.model_id, label
                    )
                )
        return candidates

    def _sync_toolbar_route_candidates() -> None:
        """把路由候选灌进高级栏（「实际站点」上拉候选框的数据源）。"""
        candidates = _route_candidates()
        if isinstance(candidates, str):
            toolbar.set_route_candidates((), note=candidates)
        else:
            toolbar.set_route_candidates(candidates)

    async def _apply_endpoint_override(endpoint_id: str) -> bool:
        """原子更新站点、实际模型、能力及请求意图；空 ID 真正切回默认绑定。"""
        nonlocal setup
        kernel = controller.coordinator().kernel
        if kernel is None or setup is None:
            chat.set_status("未配置内核：无法切换站点")
            return False
        if controller.busy:
            chat.set_status("回复生成中，暂不能切换站点")
            _refresh_toolbar()
            return False
        from limbowave.application.services.routing_service import RoutingService
        from limbowave.composition import kernel_setup

        with controller.coordinator().runtime_transition():
            try:
                decision = RoutingService(settings.load()).route(
                    setup.logical_model_id, endpoint_id=endpoint_id or None
                )
                _clear_thinking_trial()
                if not await _sync_model_catalog():
                    _refresh_toolbar()
                    return False
                await kernel.set_model(decision.provider_key, decision.model_id)
            except Exception as exc:
                chat.add_error(f"切换站点失败：{redact_text(str(exc))}")
                _refresh_toolbar()
                return False
            setup = kernel_setup(kernel, decision)
            setup_state["value"] = setup
            session_endpoint_override["endpoint"] = endpoint_id or None
            _refresh_toolbar()
            await _apply_binding_defaults()
            chat.set_status(
                f"本会话改用站点 {decision.endpoint.id} → {decision.model_id}（下一轮生效）"
            )
            return True

    def _on_override_endpoint(endpoint_id: str) -> None:
        _spawn(_apply_endpoint_override(endpoint_id))

    # ---------- 回退前确认（§四.3） ----------

    def _failover_candidates() -> list[tuple[str, str]]:
        """除当前站点外的其他绑定：(endpoint_id, model_id)。

        §四.3：可以为逻辑模型配置有序备用端点——顺序就是 bindings 的顺序。
        """
        if setup is None:
            return []
        config = settings.load()
        model = next((m for m in config.models if m.id == setup.logical_model_id), None)
        if model is None:
            return []
        current = session_endpoint_override["endpoint"] or setup.endpoint_id
        return [(b.endpoint_id, b.model_id) for b in model.bindings if b.endpoint_id != current]

    def _offer_failover_if_available(user_message_id: str) -> None:
        """连接失败后询问是否换站点重发。

        **只对连接类失败提示**（鉴权/参数/限流换站点也白搭，且计划书明确禁止
        对这些自动跨站点重发）；而且**必须用户确认**才发第二份请求——
        避免用户被计费两次。
        """
        candidates = _failover_candidates()
        if not candidates:
            return  # 没有备用站点就不打扰用户

        lines = [
            "本轮请求失败。可用的备用站点：",
            "",
        ]
        for endpoint_id, model_id in candidates:
            lines.append(f"  · {endpoint_id} → {model_id}")
        lines.extend(
            [
                "",
                "换站点会**重新发送这一轮**（可能产生第二次调用费用）。",
                "不会对鉴权失败、参数错误或限流自动换站点。",
                "",
                "现在切换到第一个备用站点并重发？",
            ]
        )
        endpoint_id = candidates[0][0]
        location = (controller.conversation_id, controller.coordinator().branch_id)

        def _source_is_current() -> bool:
            return not controller.busy and location == (
                controller.conversation_id, controller.coordinator().branch_id
            )

        async def _switch_and_resend() -> None:
            # 确认框可能跨越切会话或下一轮发送；只能重试弹框时的原请求。
            if not _source_is_current():
                return
            await controller.wait_idle()
            latest_id = await run_blocking(_last_user_message_id)
            if not _source_is_current() or latest_id != user_message_id:
                return
            if not await _apply_endpoint_override(endpoint_id) or not _source_is_current():
                return
            chat.set_status(f"已切到 {endpoint_id}，正在重试…")
            await controller.retry_user_message(user_message_id)

        def _on_failover(ok: bool) -> None:
            if ok:
                _spawn(_switch_and_resend())
            else:
                chat.set_status("已留在当前站点（未自动换站点）")

        ask_confirm(
            window,
            "是否换站点重发？",
            NL.join(lines),
            _on_failover,
            confirm_text="切换并重发",
        )

    def _last_user_message_id() -> str | None:
        """只核对重试来源，不把原文当成新消息发送。"""
        users = [m for m in _current_messages() if m.role.value == "user"]
        return users[-1].id if users else None

    toolbar.thinking_level_changed.connect(lambda level: _spawn(_on_thinking_level(level)))
    toolbar.thinking_force_requested.connect(lambda level: _spawn(_on_force_thinking_level(level)))
    chat.logical_model_changed.connect(lambda model_id: _spawn(_apply_logical_model(model_id)))
    chat.model_site_selected.connect(
        lambda model_id, endpoint_id: _spawn(_apply_logical_model(model_id, endpoint_id))
    )
    toolbar.compress_requested.connect(lambda: _spawn(_do_compress()))
    toolbar.attach_requested.connect(_attach_files)
    # 「实际站点」上拉候选框：选行即会话级站点覆盖（§四.3）
    toolbar.endpoint_override_requested.connect(_on_override_endpoint)
    # Agent 会话级操作（按会话隔离：模式与工具步骤显隐随会话/分支恢复）
    toolbar.mode_toggle_requested.connect(_toggle_execution_mode)
    toolbar.permissions_requested.connect(_open_permissions)
    toolbar.memory_requested.connect(_open_memory)
    toolbar.tool_steps_toggle_requested.connect(_on_toggle_tool_steps)
    toolbar.export_requested.connect(_export_branch)
    _refresh_toolbar()

    # ---------- 控制器事件 → 视图 ----------

    branch_render_task: asyncio.Task[bool] | None = None

    async def _await_branch_view() -> None:
        nonlocal branch_render_task
        task = branch_render_task
        if task is None:
            return
        try:
            if not await task:
                raise RuntimeError("分支视图已变化，未自动发送新请求")
        finally:
            branch_render_task = None
            chat.set_history_loading(False)

    controller.coordinator().branch_ready = _await_branch_view

    def _on_event(event: ChatEvent) -> None:
        nonlocal branch_render_task, detached_scope
        kind = event.kind
        data = event.data
        if kind == "remote_target_changing":
            if detached_scope is None:
                detached_scope = (data.get("conversation_id"), data.get("branch_id"))
            toolbar.setEnabled(False)
            return
        scope = controller.coordinator().event_scope()
        if (scope["run_origin"] == "web" and detached_scope is not None
                and detached_scope != (scope["conversation_id"], scope["branch_id"])):
            # A remote run must not append its output into an unrelated desktop transcript.
            if kind == "settled":
                chat.set_busy(False)
                chat.set_status("手机端生成已完成；当前浏览的会话保持不变。")
                _refresh_conversations()
                if preview_scope is not None:
                    _spawn(_activate_preview(preview_scope))
            elif kind == "user":
                chat.set_busy(True)
                chat.set_status("手机端正在另一会话生成；可继续浏览历史。")
            return
        if kind == "user":
            conversation_id = controller.conversation_id
            if conversation_id is not None and conversation_process is None:
                _persist_permission_selection(conversation_id)
            if "attachment_ids" in data and data.get("message_id") is not None:
                sent_ids_by_message[str(data["message_id"])] = tuple(data["attachment_ids"])
            retry_of = data.get("retry_of_message_id")
            if retry_of:
                chat.retry_user_message(
                    str(data.get("text", "")), str(data["message_id"]), str(retry_of)
                )
            else:
                chat.add_user_message(str(data.get("text", "")), data.get("message_id"))
                chat.set_busy(True)
            chat.set_status("生成中…")
        elif kind == "rate_limited":
            chat.set_status("按站点 RPM 等待发送…可点击停止取消")
        elif kind == "request_sending":
            chat.set_status("生成中…")
        elif kind == "assistant_start":
            chat.begin_assistant()
        elif kind == "assistant_delta":
            chat.append_assistant_delta(str(data.get("text", "")))
        elif kind == "thinking_delta":
            chat.append_thinking_delta(str(data.get("text", "")))
        elif kind == "assistant_end":
            chat.end_assistant(
                str(data.get("text", "")),
                data.get("message_id"),
                stop_reason=data.get("stop_reason"),
                user_message_id=data.get("user_message_id"),
            )
        elif kind == "tool":
            phase = "运行" if data.get("phase") == "start" else "完成"
            chat.set_status(f"工具 {data.get('name')} {phase}")
            chat.note_tool_step(
                str(data.get("name") or "tool"),
                str(data.get("tool_call_id") or ""),
                is_error=bool(data.get("is_error", False)),
                phase=str(data.get("phase") or "start"),
                step=data.get("step"),
            )
        elif kind == "settled":
            chat.set_busy(False)
            chat.set_status("就绪")
            _refresh_conversations()  # 一轮落库后刷新列表（标题/消息数会变）
            chat.set_compress_available(controller.conversation_id is not None)
            if preview_scope is not None:
                _spawn(_activate_preview(preview_scope))
            else:
                _spawn(_refresh_usage())  # 上下文占用随每轮变化（Phase 5）
        elif kind == "thinking_trial_finished":
            _offer_thinking_trial(data)
        elif kind == FIRST_RESPONSE_COMPLETED:
            conversation_id = str(data.get("conversation_id") or "")
            route = _active_title_route()
            kernel = controller.coordinator().kernel
            if (
                conversation_id
                and route is not None
                and kernel is not None
                and conversation_id not in _title_checked
                and conversation_id not in _title_in_flight
            ):
                _title_in_flight.add(conversation_id)
                _spawn(
                    _maybe_offer_conversation_title(
                        conversation_id,
                        str(data.get("user_text", "")),
                        str(data.get("assistant_text", "")),
                        route,
                        kernel,
                    )
                )
        elif kind == "run_interrupted":
            retry_id = data.get("user_message_id")
            if retry_id:
                chat.set_retry_available(str(retry_id))
        elif kind == "retrying":
            chat.set_status("重试中…")
            # §八.3：重试必须**在消息内显示**，不是偷偷重发
            chat.add_retry_notice(
                str(data.get("reason", "正在重试")),
                attempt=int(data.get("attempt", 1)),
                delay_ms=int(data.get("delay_ms", 0)),
                max_attempts=int(data.get("max_attempts", 1)),
            )
        elif kind == "branched":
            # 按持久化边界重载：Fork 保留起点，编辑/重生成截在起点之前。
            # 后两者紧随其后的 user 事件会接上新消息，这里必须同步完成重绘，
            # 否则历史切换动画的延迟回调会删掉刚接上的新气泡。
            # 原分支的内容仍留在原分支里。
            if "history" in data:
                if not chat.apply_branch_history(
                    data["history"], str(data.get("from_message_id"))
                ):
                    chat.set_history_loading(True, "正在准备分支…")
                    branch_render_task = asyncio.create_task(
                        chat.load_history_incrementally(data["history"])
                    )
            else:
                _show_branch(str(data.get("branch_id")), animate=False)
            _refresh_branches()
            chat.set_status("已分叉到新分支")
        elif kind == "error":
            retry_id = data.get("user_message_id")
            chat.add_error(
                str(data.get("message", "未知错误")),
                retry_message_id=str(retry_id) if retry_id else None,
            )
            # 错误后可能还有自动重试；保留本轮回复卡，交给 settled 统一收尾。
            if not controller.busy:
                chat.set_busy(False)
            chat.set_status("出错")
            # 连接类错误重试耗尽后，提供「换站点重发」的选择（§四.3）。
            # 只提示、不自动跨站点重发——这是设计约束。
            if preview_scope is None and retry_id:
                _offer_failover_if_available(str(retry_id))

    unsubscribe = controller.subscribe(_on_event)
    if not appearance_prepared:
        _apply_appearance(refresh_history=False)
    # Read/decrypt once off-thread, then update only the widgets on the GUI thread.
    _refresh_conversations(_run_startup_task(history.list_conversations))

    # No web stack, event broker, network enumeration or LAN widgets until requested.
    from limbowave.ui.lan_controller import LanAccessController

    def _remote_model_selected(selected: KernelSetup) -> None:
        nonlocal setup, thinking_trial
        setup = selected
        setup_state["value"] = selected
        thinking_trial = None
        thinking_state["level"] = selected.default_thinking_level
        session_endpoint_override["endpoint"] = None

    def _create_lan_server() -> WebServer:
        # LanAccessController loads these Qt-free modules in a worker first.
        # Construct subscribers and asyncio-owned objects on this loop, not there.
        from limbowave.application.services.runtime_facade import RuntimeFacade
        from limbowave.infrastructure.crypto.lan_password_verifier import VaultPasswordVerifier
        from limbowave.runtime_composition import RuntimeModelCatalog
        from limbowave.web.password_gate import PasswordGate
        from limbowave.web.server import WebServer

        assert key is not None
        remote_models = RuntimeModelCatalog(
            settings, credentials, paths.data_root / "runtime",
            lambda: controller.coordinator().kernel, _remote_model_selected,
        )
        facade = RuntimeFacade(
            controller, uow_factory, models_provider=remote_models.models,
            select_model=remote_models.select_model,
            ready=lambda: key is not None and not shutting_down and not resetting,
        )
        password_gate = PasswordGate(VaultPasswordVerifier(paths.data_root / "vault.json", key))
        return WebServer(facade, password_gate=password_gate)

    lan_access = LanAccessController(
        window, create_server=_create_lan_server, spawn=_spawn,
        allowed=lambda: key is not None and not resetting and not shutting_down
        and controller.available,
    )

    def _warm_settings() -> None:
        if credentials is not None:
            _open_settings(warm_only=True)

    async def _finish_startup() -> None:
        """Pi 成功启动后才执行依赖 RPC 的初始同步。"""
        # The storage process starts on its first actual request. Waiting for a
        # spare process here costs startup CPU/RSS even when no chat is opened.
        await _apply_binding_defaults()

    async def _shutdown() -> None:
        nonlocal shutting_down
        shutting_down = True
        _LOG.info("application.services_stopping")
        await lan_access.close()
        request_limiter.stop()
        probe_executor.shutdown(wait=False, cancel_futures=True)
        unsubscribe()
        if reset_panel is not None:
            from shiboken6 import isValid

            if isValid(reset_panel) and not reset_panel._closing:
                reset_panel.close_panel()
        pending = list(_pending_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            if resetting:
                if controller.busy:
                    await controller.abort()
                await controller.mark_interrupted("用户重置数据，运行已停止")
            await controller.shutdown()
            # 内核退出也可能产生最后一轮事件，必须等其收尾事务结束。
            await controller.wait_idle()
        finally:
            try:
                if tool_ipc is not None:
                    await tool_ipc.stop()
            finally:
                # Each reader closes its SQLite connection on its own thread.
                if conversation_process is not None:
                    await conversation_process.close()
                from limbowave.application.background import drain_blocking
                from limbowave.ui.background_tasks import drain_background_tasks

                await drain_background_tasks()
                await drain_blocking()
                await history_reader.close()
                await asyncio.to_thread(request_logs.close)
                if storage is not None:
                    storage.close()
                _LOG.info("application.storage_closed", extra={"persistent": storage is not None})

    return controller, tool_ipc, _warm_settings, _finish_startup, _shutdown


def _history_payload(
    messages: list[Message], *, uow_factory: UnitOfWorkFactory | None = None,
    branch_id: str | None = None,
) -> list[HistoryEntry]:
    from limbowave.application.history_payload import history_payload

    return history_payload(messages, uow_factory=uow_factory, branch_id=branch_id)


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
    if not args or args[0].startswith("-"):
        args.insert(0, "limbowave")
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--log-level", choices=[level.name for level in LogLevel] + ["CRIT"],
                        type=str.upper, default="INFO")
    parser.add_argument("--log-console", action="store_true")
    parser.add_argument("--log-dir", type=Path)
    options, remaining = parser.parse_known_args(args[1:])
    args = [args[0], *remaining]
    smoke = "--smoke" in args
    if smoke:
        args.remove("--smoke")
    context = create_context()
    if options.log_dir is not None:
        context = replace(context, paths=replace(context.paths, log_root=options.log_dir.resolve()))
    config = LogConfig(context.paths.log_root, level=options.log_level, console=options.log_console)
    from limbowave.ui.diagnostics_runtime import QtDiagnosticsBridge

    exit_code = 1
    with ExitStack() as cleanup:
        diagnostics = cleanup.enter_context(DiagnosticRuntime(config))
        cleanup.enter_context(QtDiagnosticsBridge(diagnostics.manager))
        _LOG.info("application.startup", extra={
            "version": __version__, "python": context.python_version,
            "frozen": context.frozen, "smoke": smoke, "log_dir": str(config.directory),
        })
        try:
            exit_code = _run_gui(args, context, diagnostics, cleanup, smoke=smoke)
            return exit_code
        finally:
            _LOG.info("application.shutdown", extra={"exit_code": exit_code})


def _dispose_gui_loop(loop: asyncio.AbstractEventLoop) -> None:
    if not loop.is_closed():
        pending = list(asyncio.all_tasks(loop))
        for task in pending:
            task.cancel()
        if pending:
            try:
                loop.run_until_complete(asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=2.0
                ))
            except Exception:
                _LOG.exception("application.pending_task_cleanup_failed")
        loop.close()
    asyncio.set_event_loop(None)


def _run_gui(
    args: Sequence[str], context: AppContext, diagnostics: DiagnosticRuntime, cleanup: ExitStack,
    *, smoke: bool,
) -> int:
    app = build_application(args)

    if not smoke:
        # 首次绘制前恢复外观，避免已构造控件保留默认深色内联样式。
        from limbowave.application.services.appearance_theme_service import AppearanceThemeService
        from limbowave.application.services.preferences_service import PreferencesService
        from limbowave.ui import theme
        from limbowave.ui.click_ripple import install_click_ripples
        from limbowave.ui.cursor_reveal import install_cursor_reveal
        from limbowave.ui.font_registry import family_for_file

        startup_preferences = PreferencesService(context.paths.data_root / "preferences.json")
        prefs = startup_preferences.load()
        startup_themes = AppearanceThemeService(
            context.paths.data_root / "themes",
            legacy_theme=prefs.theme,
            legacy_background_image=prefs.background_image,
            legacy_blur_radius=prefs.blur_radius,
        )
        startup_theme = startup_themes.active_theme
        theme.apply_appearance_theme(startup_theme)
        theme.set_font_scale(prefs.font_scale)
        family_for_file(prefs.font_file)
        theme.set_font_family(prefs.font_family)
        app.setStyleSheet(theme.app_stylesheet())

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    diagnostics.attach_loop(loop)
    cleanup.callback(_dispose_gui_loop, loop)

    # 主窗口不接收业务上下文：窗口只展示与发命令，装配由上层负责。
    window = MainWindow()
    cleanup.callback(window.close)
    from limbowave.ui.diagnostics_runtime import DiagnosticsWindow

    diagnostics_window = DiagnosticsWindow(
        diagnostics.manager, window, on_level_changed=diagnostics.set_level
    )
    cleanup.callback(diagnostics_window.close)
    window.diagnostics_requested.connect(diagnostics_window.show_diagnostics)
    if diagnostics.fallback_reason:
        window.statusBar().showMessage("日志文件不可用，当前仅内存记录 · Ctrl+Shift+L 查看诊断")
    if not smoke:
        window.show_full_page(LoginPage())
    window.show()
    if not smoke:
        startup_image = startup_themes.resolve_asset(startup_theme.background.asset)
        window.set_appearance_theme(
            startup_theme, str(startup_image) if startup_image is not None else ""
        )
        from limbowave.ui.theme_effects import apply_text_glow, install_hover_suspension

        apply_text_glow(window, startup_theme.text_glow)
        install_hover_suspension(window, startup_theme.materials)
        install_click_ripples(window)
        install_cursor_reveal(window)
        window.sidebar.restyle()

    if smoke:
        # 冒烟只验证窗口与事件循环，不启动内核
        window.chat.set_available(False, "冒烟模式：内核未启动")
        _report(context, window)
        QTimer.singleShot(SMOKE_EXIT_MS, loop.stop)
        with loop:
            loop.run_forever()
        return 0

    key = _unlock_vault(context.paths, window)
    _LOG.info("vault.unlock_result", extra={"unlocked": key is not None})
    reset_request: DataResetService | None = None

    def _request_reset(service: DataResetService) -> None:
        nonlocal reset_request
        if reset_request is not None:
            return
        reset_request = service
        window.setEnabled(False)
        window.chat.set_status("正在停止后台任务并重置数据，完成后退出…")
        loop.stop()

    controller, _tool_ipc, _warm_settings, finish_startup, shutdown = _wire(
        window, context.paths, key, on_reset=_request_reset, diagnostics_available=True,
        appearance_prepared=True,
    )
    window.show_workspace()

    async def _startup() -> None:
        try:
            await controller.start()
            await finish_startup()
            if controller.available:
                state = await controller.state()
                model = state.model_id if state else None
                window.chat.set_available(True)
                window.chat.set_status(f"就绪 · {model or '已连接'}")
                _LOG.info("kernel.ready", extra={"model_id": model})
            elif key is None:
                window.chat.set_available(
                    False, "资料库未解锁：本次会话不持久化，模型不可用。重启并输入主密码。"
                )
            else:
                window.chat.set_available(
                    False, "未配置模型：先用 python -m limbowave secret set 配置密钥后重启"
                )
        except Exception as exc:
            _LOG.exception("kernel.start_failed")
            message = redact_text(str(exc))
            window.chat.set_available(False, f"模型内核启动失败：{message}")
            window.chat.add_error(f"模型内核启动失败：{message}")

    reset_error: str | None = None
    with loop:
        start_task = loop.create_task(_startup())
        app.aboutToQuit.connect(loop.stop)
        cleanup.callback(app.aboutToQuit.disconnect, loop.stop)
        try:
            loop.run_forever()
        finally:
            if not start_task.done():
                start_task.cancel()
            loop.run_until_complete(asyncio.gather(start_task, return_exceptions=True))
            try:
                loop.run_until_complete(shutdown())
            except Exception:
                _LOG.exception("application.shutdown_failed")
                if reset_request is None:
                    raise
                reset_error = "后台任务或数据库未能安全关闭，未执行删除。"

    # 已退出运行循环、停止任务并释放连接；不会删完又被旧回调创建一个空库。
    if reset_request is not None:
        if reset_error is None:
            try:
                _LOG.warning("data_reset.executing")
                reset_request.execute()
                _LOG.info("data_reset.completed")
            except Exception as exc:
                _LOG.exception("data_reset.failed")
                reset_error = f"{redact_text(str(exc))}"
        if reset_error is not None:
            # 仅允许操作错误提示，不让用户继续使用已经停止或部分删除的资料库。
            window.setEnabled(True)
            for child in window.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            ):
                child.setEnabled(False)
            _show_startup_alert(window, "重置未完成", reset_error + NL + "应用将退出。")
        window.close()
        return 1 if reset_error is not None else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
