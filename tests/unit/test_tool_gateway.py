"""工具网关验收（Phase 6 / 设计计划 §九 + §十一）。

锁住的不变量：
- 网关是唯一入口：无授权 → 需确认；确认后放行；越界 → 拒绝；
- 高影响操作即使已授权也要二次确认（凭据路径、批量删除、破坏性命令）；
- 授权按会话持久（重开会话自动恢复）；
- 审计如实记录每次决策（工具、参数、规则、结果、是否用户确认）；
- 文件工具的越权路径穿越被拒（网关层，不只是领域层）；
- 输出上限截断并如实标记；
- 联网：私有网段默认拒、元数据始终拒、未配置搜索如实报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.tool_gateway import (
    NetworkTools,
    ToolGateway,
)
from limbowave.domain.permissions import Capability, Decision, ExecutionMode, PermissionPreset
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def permissions(store: InMemoryStore) -> PermissionService:
    return PermissionService(in_memory_uow_factory(store))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("# 笔记\n第一行\n第二行\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return root


@pytest.fixture
def gateway(permissions: PermissionService, workspace: Path) -> ToolGateway:
    return ToolGateway(permissions, workspace)


@pytest.fixture
def network_gateway(permissions: PermissionService, workspace: Path) -> ToolGateway:
    """配了联网工具的网关——网络守卫才会真正执行。"""
    network = NetworkTools(fetcher=lambda _url: (200, "text/plain", b"ok"), max_bytes=1000)
    return ToolGateway(permissions, workspace, network=network)


CONV = "c1"


# ---------- 权限网关路径 ----------


def test_no_grant_requires_confirmation(gateway: ToolGateway) -> None:
    result = gateway.invoke("list_directory", {"path": "."}, CONV)
    assert not result.ok
    assert result.data.get("needs_confirmation") is True
    assert "尚无会话授权" in (result.error or "")


def test_grant_then_allowed(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    result = gateway.invoke("list_directory", {"path": "."}, CONV)
    assert result.ok
    names = {e["name"] for e in result.data["entries"]}
    assert "notes.md" in names


def test_out_of_scope_denied_after_confirmation(
    gateway: ToolGateway,
    permissions: PermissionService,
    workspace: Path,
    tmp_path: Path,
) -> None:
    """授权只覆盖工作区；工作区外的路径即使确认也不放行（越界）。"""
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = gateway.invoke("stat_file", {"path": str(outside)}, CONV, user_confirmed=True)
    assert not result.ok
    # 路径守卫在权限判定前就拦住了（越界）
    assert "越界" in (result.error or "")


def test_credential_path_needs_extra_confirmation(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    """凭据目录即使已授权也要二次确认（§11.2）。"""
    (workspace / ".ssh").mkdir()
    (workspace / ".ssh" / "id_rsa").write_text("key", encoding="utf-8")
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))

    result = gateway.invoke("stat_file", {"path": ".ssh/id_rsa"}, CONV)
    assert not result.ok
    assert result.data.get("needs_confirmation") is True

    # 用户确认后可执行
    confirmed = gateway.invoke("stat_file", {"path": ".ssh/id_rsa"}, CONV, user_confirmed=True)
    assert confirmed.ok


def test_destructive_command_risk_flagged_by_domain() -> None:
    """破坏性命令被识别为高影响（领域规则）。"""
    from limbowave.domain.permissions import ResourceScope, ToolRequest, evaluate

    request = ToolRequest(
        tool_name="run_command",
        capability=Capability.TERMINAL,
        scope=ResourceScope(command="rm -rf /", working_directory="."),
    )
    decision = evaluate(request, [])
    assert decision.risk.value == "high"


def test_grant_persists_across_reads(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    """授权在库里，重开会话（新服务实例）自动恢复。"""
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    fresh_service = PermissionService(permissions._uow_factory)
    assert len(fresh_service.list_grants(CONV)) == 1


def test_permission_preset_persists_with_conversation(
    permissions: PermissionService,
) -> None:
    from datetime import UTC, datetime

    from limbowave.domain.conversation import Conversation

    with permissions._uow_factory() as uow:
        uow.conversations.add(Conversation(id=CONV, title="t", created_at=datetime.now(UTC)))
        uow.commit()

    assert permissions.get_preset(CONV) is PermissionPreset.READ_ONLY
    assert permissions.set_preset(CONV, PermissionPreset.FULL_ACCESS)
    fresh_service = PermissionService(permissions._uow_factory)
    assert fresh_service.get_preset(CONV) is PermissionPreset.FULL_ACCESS


def test_custom_permissions_replace_existing_grants(
    permissions: PermissionService, workspace: Path
) -> None:
    permissions.grant(CONV, Capability.TERMINAL)
    permissions.replace_custom_grants(
        CONV,
        {Capability.FILE_READ, Capability.NETWORK},
        workspace_root=str(workspace),
    )

    grants = permissions.list_grants(CONV)
    assert {grant.capability for grant in grants} == {Capability.FILE_READ, Capability.NETWORK}
    assert next(g for g in grants if g.capability is Capability.FILE_READ).allowed_paths == (
        str(workspace),
    )
    assert next(g for g in grants if g.capability is Capability.NETWORK).allowed_domains == ("*",)


def test_revoke_removes_grant(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    grant = permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    assert gateway.invoke("list_directory", {}, CONV).ok
    assert permissions.revoke(grant.id)
    assert not gateway.invoke("list_directory", {}, CONV).ok  # 回到需确认


def test_audit_records_every_decision(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    gateway.invoke("list_directory", {}, CONV)  # 放行
    gateway.invoke("stat_file", {"path": "missing.txt"}, CONV)  # 放行后执行失败，也记审计

    audit = permissions.list_audit(CONV)
    assert len(audit) == 2
    assert all(a.tool_name for a in audit)
    assert audit[0].decision is Decision.ALLOW
    assert audit[0].matched_rule  # 匹配规则


def test_audit_records_user_confirmation(
    gateway: ToolGateway, permissions: PermissionService
) -> None:
    """未确认 → 记 CONFIRM/未确认；确认后 → 记 ALLOW/已确认。

    不依赖两条记录的先后（同刻度下排序会退化为随机 id）——断言的是不变量：
    「确认与否」被如实记下来。
    """
    gateway.invoke("list_directory", {}, CONV)  # 无授权 → 需确认
    gateway.invoke("list_directory", {}, CONV, user_confirmed=True)

    audit = permissions.list_audit(CONV)
    assert len(audit) == 2
    unconfirmed = [a for a in audit if not a.user_confirmed]
    confirmed = [a for a in audit if a.user_confirmed]
    assert len(unconfirmed) == 1 and len(confirmed) == 1
    assert unconfirmed[0].decision is Decision.CONFIRM
    assert confirmed[0].decision is Decision.ALLOW


def test_direct_terminal_mode_widens_confirmation(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    """直接终端模式：内置工具的细粒度约束不适用 —— 明确要求确认（§9.1）。"""
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    result = gateway.invoke("list_directory", {}, CONV, mode=ExecutionMode.DIRECT_TERMINAL)
    assert not result.ok
    assert result.data.get("needs_confirmation") is True


# ---------- 参数校验 ----------


def test_invalid_params_rejected_before_permission(gateway: ToolGateway) -> None:
    result = gateway.invoke("stat_file", {}, CONV)  # 缺必填 path
    assert not result.ok
    assert "缺少必填参数" in (result.error or "")


def test_unknown_tool_rejected(gateway: ToolGateway) -> None:
    result = gateway.invoke("no_such_tool", {}, CONV)
    assert not result.ok
    assert "未知工具" in (result.error or "")


def test_wrong_type_rejected(gateway: ToolGateway) -> None:
    result = gateway.invoke("search_text", {"pattern": 123}, CONV)
    assert not result.ok
    assert "类型不符" in (result.error or "")


# ---------- 文件工具（Task 6.2） ----------


@pytest.fixture
def granted_gateway(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> ToolGateway:
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    permissions.grant(CONV, Capability.FILE_WRITE, allowed_paths=(str(workspace),))
    return gateway


def test_list_directory(granted_gateway: ToolGateway) -> None:
    result = granted_gateway.invoke("list_directory", {}, CONV)
    assert result.ok
    assert result.data["count"] == 2  # notes.md + sub


def test_stat_file(granted_gateway: ToolGateway) -> None:
    result = granted_gateway.invoke("stat_file", {"path": "notes.md"}, CONV)
    assert result.ok
    assert result.data["type"] == "file"
    assert result.data["size"] > 0


def test_search_text_reports_line_numbers(granted_gateway: ToolGateway) -> None:
    result = granted_gateway.invoke("search_text", {"pattern": "第二行"}, CONV)
    assert result.ok
    assert result.data["count"] == 1
    assert result.data["hits"][0]["line"] == 3  # 行号从 1 开始，与读取工具一致


def test_create_file_refuses_overwrite(granted_gateway: ToolGateway) -> None:
    ok = granted_gateway.invoke("create_file", {"path": "new.txt", "content": "hi"}, CONV)
    assert ok.ok
    again = granted_gateway.invoke("create_file", {"path": "new.txt", "content": "x"}, CONV)
    assert not again.ok
    assert "不覆盖" in (again.error or "")


def test_modify_file_requires_unique_match(granted_gateway: ToolGateway) -> None:
    granted_gateway.invoke("create_file", {"path": "m.txt", "content": "aa bb aa"}, CONV)
    ambiguous = granted_gateway.invoke(
        "modify_file", {"path": "m.txt", "old_text": "aa", "new_text": "cc"}, CONV
    )
    assert not ambiguous.ok
    assert "恰好一次" in (ambiguous.error or "")

    ok = granted_gateway.invoke(
        "modify_file", {"path": "m.txt", "old_text": "bb", "new_text": "cc"}, CONV
    )
    assert ok.ok


def test_path_traversal_blocked_at_gateway(granted_gateway: ToolGateway) -> None:
    """越权穿越在网关层被拦（不只是领域层）。"""
    result = granted_gateway.invoke("stat_file", {"path": "../outside.txt"}, CONV)
    assert not result.ok
    assert "越界" in (result.error or "")


def test_write_grant_does_not_grant_read(
    gateway: ToolGateway, permissions: PermissionService, workspace: Path
) -> None:
    """能力分离：只有写授权时，读操作仍需确认。"""
    permissions.grant(CONV, Capability.FILE_WRITE, allowed_paths=(str(workspace),))
    result = gateway.invoke("list_directory", {}, CONV)
    assert not result.ok  # 读需要读授权


# ---------- 空的可选参数 / 闸门与网关同源的请求 ----------


def test_blank_optional_path_means_workspace_root(granted_gateway: ToolGateway) -> None:
    """模型常把「不填」写成空串：可选路径为空 = 默认目录，而不是报「路径为空」。"""
    result = granted_gateway.invoke("list_directory", {"path": ""}, CONV)
    assert result.ok, result.error
    assert result.data["count"] == 2
    assert result.data["path"] == str(granted_gateway.workspace_root)

    search = granted_gateway.invoke("search_text", {"pattern": "第二行", "path": "  "}, CONV)
    assert search.ok, search.error
    assert search.data["count"] == 1


def test_blank_required_path_still_rejected(granted_gateway: ToolGateway) -> None:
    result = granted_gateway.invoke("stat_file", {"path": ""}, CONV)
    assert not result.ok
    assert "路径为空" in (result.error or "")


def test_prepare_request_resolves_paths(gateway: ToolGateway, workspace: Path) -> None:
    """闸门据此判定：范围是解析后的绝对路径，与授权、执行时的检查同一种形式。"""
    request = gateway.prepare_request("stat_file", {"path": "sub/a.txt"})
    assert request.scope.paths == (str((workspace / "sub" / "a.txt").resolve()),)
    blank = gateway.prepare_request("list_directory", {"path": ""})
    assert blank.scope.paths == (str(gateway.workspace_root),)
    assert blank.raw_params == {}  # 空的可选参数被规范掉


# ---------- 「本会话允许」的授权边界 ----------


def test_session_scope_for_file_read_covers_workspace(gateway: ToolGateway) -> None:
    from limbowave.domain.permissions import session_scope_for

    root = str(gateway.workspace_root)
    request = gateway.prepare_request("search_text", {"pattern": "x", "path": "sub"})
    scope = session_scope_for(request, root)
    assert scope is not None
    assert scope.capability is Capability.FILE_READ
    assert scope.allowed_paths == (root,)


def test_session_scope_for_refuses_when_grant_would_not_help(gateway: ToolGateway) -> None:
    """高影响（凭据路径 / 破坏性命令）与没有可授权目标（网页搜索）：不给「记住」。"""
    from limbowave.domain.permissions import ResourceScope, ToolRequest, session_scope_for

    root = str(gateway.workspace_root)
    credential = gateway.prepare_request("stat_file", {"path": ".ssh/id_rsa"})
    assert session_scope_for(credential, root) is None
    search = gateway.prepare_request("web_search", {"query": "python"})
    assert session_scope_for(search, root) is None
    destructive = ToolRequest(
        tool_name="run_command",
        capability=Capability.TERMINAL,
        scope=ResourceScope(command="rm -rf /tmp/x"),
    )
    assert session_scope_for(destructive, root) is None


def test_session_scope_for_network_and_terminal(gateway: ToolGateway) -> None:
    from limbowave.domain.permissions import session_scope_for

    root = str(gateway.workspace_root)
    url = gateway.prepare_request("read_url", {"url": "https://example.com/a"})
    scope = session_scope_for(url, root)
    assert scope is not None and scope.allowed_domains == ("example.com",)

    command = gateway.prepare_request("run_command", {"command": "Get-Date"})
    terminal = session_scope_for(command, root)
    assert terminal is not None
    assert terminal.capability is Capability.TERMINAL
    assert terminal.allowed_paths == () and terminal.allowed_domains == ()


def test_session_grant_then_gateway_allows(
    gateway: ToolGateway, permissions: PermissionService
) -> None:
    """按提议的边界建授权后，网关对同类请求直接放行（闸门与网关判的是同一件事）。"""
    from limbowave.domain.permissions import session_scope_for

    scope = session_scope_for(
        gateway.prepare_request("list_directory", {"path": ""}), str(gateway.workspace_root)
    )
    assert scope is not None
    permissions.grant(CONV, scope.capability, allowed_paths=scope.allowed_paths)
    assert gateway.invoke("list_directory", {"path": ""}, CONV).ok
    assert gateway.invoke("stat_file", {"path": "sub/a.txt"}, CONV).ok


# ---------- 联网工具（Task 6.3） ----------


def test_read_url_blocks_private_even_when_domain_granted(
    network_gateway: ToolGateway, permissions: PermissionService
) -> None:
    """纵深防御：用户显式放行该域名，网络守卫仍拦住环回地址。"""
    permissions.grant(CONV, Capability.NETWORK, allowed_domains=("127.0.0.1",))
    result = network_gateway.invoke("read_url", {"url": "http://127.0.0.1:8080/x"}, CONV)
    assert not result.ok
    assert "禁止" in (result.error or "")
    assert "环回" in (result.error or "")


def test_read_url_blocks_metadata_even_when_granted(
    network_gateway: ToolGateway, permissions: PermissionService
) -> None:
    """云元数据地址：即使授权域名匹配也拒绝。"""
    permissions.grant(CONV, Capability.NETWORK, allowed_domains=("169.254.169.254",))
    result = network_gateway.invoke(
        "read_url", {"url": "http://169.254.169.254/latest/meta-data/"}, CONV
    )
    assert not result.ok
    assert "始终禁止" in (result.error or "")


def test_read_url_truncates_output(permissions: PermissionService, monkeypatch) -> None:
    """输出超上限：截断并如实标记。"""
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    fetcher = lambda _url: (200, "text/plain", b"x" * 100_000)  # noqa: E731
    network = NetworkTools(fetcher=fetcher, max_bytes=1000)
    gateway = ToolGateway(permissions, Path.cwd(), network=network)
    permissions.grant(CONV, Capability.NETWORK, allowed_domains=("example.com",))

    result = gateway.invoke("read_url", {"url": "https://example.com/big"}, CONV)
    assert result.ok
    assert result.truncated is True
    assert result.notes  # 如实告知被截断
    assert len(result.data["text"]) <= 1000


def test_web_search_unconfigured_reports_honestly(
    network_gateway: ToolGateway, permissions: PermissionService
) -> None:
    """搜索未配置：如实报错，不伪造结果。"""
    permissions.grant(CONV, Capability.NETWORK, allowed_domains=("example.com",))
    # 搜索目标由服务端决定，无资源范围 → 需用户确认
    result = network_gateway.invoke("web_search", {"query": "python"}, CONV, user_confirmed=True)
    assert not result.ok
    assert "未配置" in (result.error or "")


# ---------- 直接终端模式（Task 7.4 / §9.1） ----------


class FakeShellRunner:
    """假 shell 执行器：不真跑 shell，只验证网关的判定与装配。"""

    def __init__(self, stdout: str = "ok", exit_code: int = 0) -> None:
        self.commands: list[str] = []
        self._stdout = stdout
        self._exit_code = exit_code

    def run(self, command: str, *, working_directory=None):
        from limbowave.domain.shell import ShellResult

        self.commands.append(command)
        return ShellResult(
            shell="powershell",
            command_id="cmd_fake",
            working_directory="C:/ws",
            exit_code=self._exit_code,
            timed_out=False,
            stdout=self._stdout,
            stderr="",
        )


@pytest.fixture
def terminal_gateway(permissions: PermissionService, workspace: Path):
    from limbowave.application.services.tool_gateway import TerminalTools

    runner = FakeShellRunner()
    gateway = ToolGateway(
        permissions,
        workspace,
        terminal=TerminalTools(runner),  # type: ignore[arg-type]
    )
    return gateway, runner


def test_run_command_requires_confirmation_without_grant(
    terminal_gateway, permissions: PermissionService
) -> None:
    """终端命令无授权时需确认（§11.2：终端是独立能力）。"""
    gateway, runner = terminal_gateway
    result = gateway.invoke("run_command", {"command": "Get-Date"}, CONV)
    assert not result.ok
    assert result.data.get("needs_confirmation") is True
    assert runner.commands == []  # 没执行


def test_run_command_executes_after_confirmation(terminal_gateway) -> None:
    gateway, runner = terminal_gateway
    result = gateway.invoke("run_command", {"command": "Get-Date"}, CONV, user_confirmed=True)
    assert result.ok
    assert runner.commands == ["Get-Date"]
    assert result.data["exit_code"] == 0
    assert result.data["succeeded"] is True


def test_run_command_reports_bash_syntax_diagnostics(terminal_gateway) -> None:
    """Bash 误用：返回结构化诊断，**不自动改写后执行**（§10.4）。"""
    gateway, runner = terminal_gateway
    result = gateway.invoke("run_command", {"command": "export FOO=bar"}, CONV, user_confirmed=True)
    assert result.ok  # 命令照跑（由模型决定是否修正）
    diagnostics = result.data["bash_syntax_diagnostics"]
    assert any(d["pattern"] == "export" for d in diagnostics)
    assert all(d["suggestion"] for d in diagnostics)
    assert runner.commands == ["export FOO=bar"]  # 原样执行，没被改写


def test_run_command_destructive_needs_confirmation_even_when_granted(
    permissions: PermissionService, workspace: Path
) -> None:
    """破坏性命令属高影响：即使有终端授权也要二次确认。"""
    from limbowave.application.services.tool_gateway import TerminalTools
    from limbowave.domain.permissions import Capability

    permissions.grant(CONV, Capability.TERMINAL)
    runner = FakeShellRunner()
    gateway = ToolGateway(permissions, workspace, terminal=TerminalTools(runner))  # type: ignore[arg-type]

    result = gateway.invoke("run_command", {"command": "rm -rf /tmp/x"}, CONV)
    assert not result.ok
    assert result.data.get("needs_confirmation") is True

    ok = gateway.invoke("run_command", {"command": "rm -rf /tmp/x"}, CONV, user_confirmed=True)
    assert ok.ok


def test_run_command_failure_still_returns_output(
    permissions: PermissionService, workspace: Path
) -> None:
    """非零退出码：结果仍返回（模型需要看到 stdout/stderr 才能修）。"""
    from limbowave.application.services.tool_gateway import TerminalTools

    permissions.grant(CONV, Capability.TERMINAL)
    runner = FakeShellRunner(stdout="partial output", exit_code=1)
    gateway = ToolGateway(permissions, workspace, terminal=TerminalTools(runner))  # type: ignore[arg-type]

    result = gateway.invoke("run_command", {"command": "failing"}, CONV)
    assert result.ok  # 工具调用本身成功（拿到了结果）
    assert result.data["exit_code"] == 1
    assert result.data["succeeded"] is False
    assert result.data["stdout"] == "partial output"


def test_terminal_not_configured_reports_clearly(
    gateway: ToolGateway, permissions: PermissionService
) -> None:
    from limbowave.domain.permissions import Capability

    permissions.grant(CONV, Capability.TERMINAL)
    result = gateway.invoke("run_command", {"command": "x"}, CONV)
    assert not result.ok
    assert "未配置" in (result.error or "")


# ---------- read_document：按 file_id 精确读取附件（设计计划 §八.2） ----------


@pytest.fixture
def documents(tmp_path: Path) -> tuple[object, str]:
    """登记一个 5 行的磁盘文档，返回 (FileService, file_id)。"""
    from limbowave.application.services.file_service import FileService
    from limbowave.infrastructure.memory_repositories import in_memory_uow_factory

    files = FileService(in_memory_uow_factory(InMemoryStore()))
    path = tmp_path / "attachments" / "notes.md"
    path.parent.mkdir()
    path.write_text("\n".join(f"第{i}行" for i in range(1, 6)) + "\n", encoding="utf-8")
    document = files.index_path(path)
    return files, document.id


def test_read_document_returns_structured_range(
    permissions: PermissionService, workspace: Path, documents: tuple[object, str]
) -> None:
    files, file_id = documents
    gateway = ToolGateway(permissions, workspace, documents=files)  # type: ignore[arg-type]
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))

    result = gateway.invoke(
        "read_document", {"file_id": file_id, "start_line": 2, "end_line": 3}, CONV
    )
    assert result.ok, result.error
    assert result.data["file_id"] == file_id
    assert result.data["requested_range"] == [2, 3]
    assert result.data["actual_range"] == [2, 3]
    assert result.data["total_lines"] == 5
    assert result.data["content_hash"]
    assert result.data["text"] == "第2行\n第3行"


def test_read_document_defaults_and_over_range_rejected(
    permissions: PermissionService, workspace: Path, documents: tuple[object, str]
) -> None:
    """不填范围读到默认上限内；超上限拒绝并给建议分段（不擅自截取）。"""
    files, file_id = documents
    gateway = ToolGateway(permissions, workspace, documents=files)  # type: ignore[arg-type]
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))

    head = gateway.invoke("read_document", {"file_id": file_id}, CONV)
    assert head.ok
    assert head.data["actual_range"] == [1, 5]  # 实际收敛到文件行数

    over = gateway.invoke(
        "read_document", {"file_id": file_id, "start_line": 1, "end_line": 400}, CONV
    )
    assert not over.ok
    assert over.data["limit"] == 200
    assert over.data["suggested_segments"] == [[1, 200], [201, 400]]
    assert "建议分段" in (over.error or "")


def test_read_document_unknown_id_and_missing_documents(
    permissions: PermissionService, workspace: Path, documents: tuple[object, str]
) -> None:
    files, file_id = documents
    gateway = ToolGateway(permissions, workspace, documents=files)  # type: ignore[arg-type]
    permissions.grant(CONV, Capability.FILE_READ, allowed_paths=(str(workspace),))
    missing = gateway.invoke("read_document", {"file_id": "file_nope"}, CONV)
    assert not missing.ok
    assert "不存在" in (missing.error or "")

    bare = ToolGateway(permissions, workspace)  # 未配置文档服务
    no_docs = bare.invoke("read_document", {"file_id": file_id}, CONV)
    assert not no_docs.ok
    assert "未配置" in (no_docs.error or "")


def test_read_document_requires_authorization_like_other_reads(
    permissions: PermissionService, workspace: Path, documents: tuple[object, str]
) -> None:
    files, file_id = documents
    gateway = ToolGateway(permissions, workspace, documents=files)  # type: ignore[arg-type]
    result = gateway.invoke("read_document", {"file_id": file_id}, CONV)
    assert not result.ok
    assert result.data.get("needs_confirmation") is True
