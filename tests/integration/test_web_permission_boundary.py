"""Remote tool ceiling precedes validation, trust, confirmation and side effects."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from limbowave.application.services.permission_service import PermissionService
from limbowave.application.services.run_origin import RUN_ORIGIN, remote_origin
from limbowave.application.services.tool_gateway import (
    _SCHEMAS,
    ToolDenied,
    ToolGateway,
)
from limbowave.domain.permissions import Capability, ExecutionMode
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory

TOOLS = {
    "add_session_memory": {"content": "secret", "call_id": "call", "run_id": "run"},
    "read_document": {"file_id": "attachment"},
    "list_directory": {"path": "."},
    "stat_file": {"path": "notes.txt"},
    "search_text": {"path": ".", "pattern": "secret"},
    "create_file": {"path": "new.txt", "content": "changed"},
    "modify_file": {"path": "notes.txt", "old_text": "secret", "new_text": "changed"},
    "read_url": {"url": "https://example.com"},
    "web_search": {"query": "secret"},
    "run_command": {"command": "whoami"},
}


@pytest.fixture
def gateway(tmp_path: Path) -> ToolGateway:
    permissions = PermissionService(in_memory_uow_factory(InMemoryStore()))
    # Existing persisted conversation grants must not enlarge the remote ceiling.
    for capability in Capability:
        permissions.grant(
            "conversation", capability, allowed_paths=(str(tmp_path),), allowed_domains=("*",)
        )
    (tmp_path / "notes.txt").write_text("secret", encoding="utf-8")
    return ToolGateway(permissions, tmp_path)


def test_all_registered_tools_have_boundary_cases() -> None:
    assert set(TOOLS) == set(_SCHEMAS)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_web_denied_before_any_dependency(
    gateway: ToolGateway, tool: str, confirmed: bool, mode: ExecutionMode
) -> None:
    # Even an authorizer that would permit everything must never be consulted.
    dependencies = [Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock()]
    (
        gateway._permissions, gateway._files, gateway._network, gateway._terminal,
        gateway._documents, gateway.memory_service, gateway.memory_context,
    ) = dependencies
    with remote_origin():
        result = gateway.invoke(
            tool, TOOLS[tool], "conversation", mode=mode, user_confirmed=confirmed
        )
    assert not result.ok
    assert "web" in (result.error or "")
    assert result.data == {}  # no confirmation challenge / remote approval mechanism
    for dependency in dependencies:
        assert dependency.mock_calls == []


@pytest.mark.parametrize("tool,params", [
    ("unknown", {}),
    ("stat_file", {"path": "../../secret"}),
    ("run_command", {"command": "whoami", "run_origin": "desktop"}),
    ("add_session_memory", {}),
])
def test_boundary_precedes_validation(gateway: ToolGateway, tool: str, params: dict) -> None:
    with remote_origin():
        result = gateway.invoke(tool, params, "conversation", user_confirmed=True)
    assert not result.ok
    assert "web" in (result.error or "")


@pytest.mark.parametrize("entry", ["prepare_request", "build_request", "_execute"])
@pytest.mark.parametrize("tool", TOOLS)
def test_direct_gateway_paths_denied(gateway: ToolGateway, entry: str, tool: str) -> None:
    with remote_origin(), pytest.raises(ToolDenied, match="web"):
        getattr(gateway, entry)(tool, TOOLS[tool])


def test_document_path_denied(gateway: ToolGateway) -> None:
    with remote_origin(), pytest.raises(ToolDenied, match="web"):
        gateway._read_document({"file_id": "attachment"})


def test_nested_context_restores_after_exception() -> None:
    assert RUN_ORIGIN.get() == "desktop"
    with pytest.raises(RuntimeError), remote_origin():
        with remote_origin():
            assert RUN_ORIGIN.get() == "web"
        assert RUN_ORIGIN.get() == "web"
        raise RuntimeError("failed run")
    assert RUN_ORIGIN.get() == "desktop"


async def test_child_tasks_retries_and_threads_keep_origin(gateway: ToolGateway) -> None:
    ready = asyncio.Event()

    async def retry():
        await ready.wait()
        results = []
        for _ in range(2):
            results.append(await asyncio.to_thread(
                gateway.invoke, "stat_file", {"path": "notes.txt"}, "conversation",
                user_confirmed=True,
            ))
        return results

    with remote_origin():
        task = asyncio.create_task(retry())
    # The request scope ended; the run and retries still retain web provenance.
    assert RUN_ORIGIN.get() == "desktop"
    assert gateway.invoke("stat_file", {"path": "notes.txt"}, "conversation").ok
    ready.set()
    for result in await task:
        assert not result.ok
        assert "web" in (result.error or "")


def test_desktop_works_before_and_after_remote_denial(gateway: ToolGateway, tmp_path: Path) -> None:
    assert RUN_ORIGIN.get() == "desktop"
    assert gateway.invoke("stat_file", {"path": "notes.txt"}, "conversation").ok
    with remote_origin():
        assert not gateway.invoke(
            "modify_file", TOOLS["modify_file"], "conversation", user_confirmed=True
        ).ok
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "secret"
    assert gateway.invoke("modify_file", TOOLS["modify_file"], "conversation").ok
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "changed"

@pytest.mark.parametrize("tool", TOOLS)
async def test_preexisting_ipc_task_denies_fully_trusted_active_web_run(
    gateway: ToolGateway, tool: str
) -> None:
    from limbowave.application.services.run_origin import remote_tools_denied
    from limbowave.domain.permissions import Decision

    # Simulates coordinator-owned immutable run data read by app assembly.
    active = {"origin": "desktop"}
    gateway.origin_provider = lambda: active["origin"]
    authorize = Mock(return_value=Mock(decision=Decision.ALLOW))
    gateway._permissions.authorize = authorize
    start = asyncio.Event()

    async def ipc_reader():
        await start.wait()
        assert RUN_ORIGIN.get() == "desktop"  # spawned before web send
        assert remote_tools_denied(gateway.origin_provider)  # Pi permission handler
        return await asyncio.to_thread(
            gateway.invoke, tool, TOOLS[tool], "conversation", user_confirmed=True
        )

    reader = asyncio.create_task(ipc_reader())
    with remote_origin():
        active["origin"] = RUN_ORIGIN.get()
    start.set()
    result = await reader
    assert not result.ok
    assert "web" in (result.error or "")
    authorize.assert_not_called()
    # After settled the local desktop run retains its original behavior.
    active["origin"] = "desktop"
    assert gateway.invoke("stat_file", {"path": "notes.txt"}, "conversation").ok
    authorize.assert_called_once()


@pytest.mark.parametrize("entry", ["prepare_request", "build_request", "_execute"])
def test_active_provider_protects_direct_entries(gateway: ToolGateway, entry: str) -> None:
    gateway.origin_provider = lambda: "web"
    with pytest.raises(ToolDenied, match="web"):
        getattr(gateway, entry)("stat_file", {"path": "notes.txt"})


def test_active_provider_protects_direct_document_path(gateway: ToolGateway) -> None:
    gateway.origin_provider = lambda: "web"
    with pytest.raises(ToolDenied, match="web"):
        gateway._read_document({"file_id": "attachment"})


@pytest.mark.parametrize("origin", ["web", "", "unknown", None])
def test_provider_unknown_origin_fails_closed(gateway: ToolGateway, origin) -> None:
    gateway.origin_provider = lambda: origin
    assert not gateway.invoke("stat_file", {"path": "notes.txt"}, "conversation").ok


def test_provider_failure_fails_closed(gateway: ToolGateway) -> None:
    def unavailable():
        raise RuntimeError("private runtime details")

    gateway.origin_provider = unavailable
    result = gateway.invoke("stat_file", {"path": "notes.txt"}, "conversation")
    assert not result.ok
    assert "private runtime details" not in (result.error or "")


def test_local_provider_cannot_override_inherited_remote_task(gateway: ToolGateway) -> None:
    gateway.origin_provider = lambda: "desktop"
    with remote_origin():
        assert not gateway.invoke("stat_file", {"path": "notes.txt"}, "conversation").ok


def test_constructor_accepts_trusted_origin_provider(gateway: ToolGateway, tmp_path: Path) -> None:
    guarded = ToolGateway(gateway._permissions, tmp_path, origin_provider=lambda: "web")
    assert not guarded.invoke("stat_file", {"path": "notes.txt"}, "conversation").ok

# Exercise the actual desktop permission callback, including Pi-only tool gates
# which never pass through ToolGateway.invoke.
PERMISSION_GATES = [
    ("stat_file", {"tool": "stat_file", "input": {"path": "notes.txt"}}),
    ("user_bash", {"tool": "user_bash", "input": {"command": "whoami"},
                   "command": "whoami"}),
    ("unknown", {"tool": "unknown", "input": {"path": "notes.txt"}}),
    ("add_session_memory", {"tool": "add_session_memory", "input": {"content": "note"},
                            "run_id": "run", "call_id": "call"}),
    ("malformed", "not json"),
    ("missing_tool", {"input": {}}),
    ("unstructured", "ordinary extension confirmation"),
]


@pytest.fixture
def permission_callback_env(gateway: ToolGateway, monkeypatch):
    from unittest.mock import AsyncMock

    from limbowave import app
    from limbowave.domain.memory import MemoryPolicy, MemoryRunContext
    from limbowave.domain.permissions import Decision, PermissionPreset

    # No QWidget / QApplication is needed: if a prompt is attempted the test fails.
    prompts = [AsyncMock(return_value=True) for _ in range(3)]
    for name, prompt in zip(
        ("_ask_user", "_ask_memory_user", "_ask_permission"), prompts, strict=True
    ):
        monkeypatch.setattr(app, name, prompt)
    parse_gate = Mock(wraps=app._parse_gate_request)
    monkeypatch.setattr(app, "_parse_gate_request", parse_gate)
    permissions = Mock()
    permissions.authorize.return_value = Mock(decision=Decision.ALLOW)
    conversation = Mock(return_value="conversation")
    preset = Mock(return_value=PermissionPreset.FULL_ACCESS)
    memory = Mock()
    memory.validate_content.side_effect = lambda content: content
    memory.effective_policy.return_value = MemoryPolicy.ALLOW
    context = Mock(return_value=MemoryRunContext("run", "conversation", "branch", "message"))
    handler = app._make_permission_handler(
        None, permissions, conversation, gateway,
        preset_provider=preset, memory_service=memory, memory_context=context,
    )
    untouched = [*prompts, parse_gate, permissions, conversation, preset, memory, context]
    return handler, untouched, memory


def _permission_gate_payload(case):
    import json

    tool, payload = case
    title = "extension confirmation" if tool == "unstructured" else f"limbowave.gate:{tool}"
    return title, json.dumps(payload) if isinstance(payload, dict) else payload


@pytest.mark.parametrize("case", PERMISSION_GATES, ids=[row[0] for row in PERMISSION_GATES])
@pytest.mark.parametrize("source", ["context", "independent_task"])
@pytest.mark.parametrize("memory_policy", ["allow", "ask"])
async def test_app_permission_callback_hard_denies_before_prompts_and_full_trust(
    gateway: ToolGateway, permission_callback_env, case, source, memory_policy
) -> None:
    from limbowave.domain.memory import MemoryPolicy

    handler, untouched, memory = permission_callback_env
    memory.effective_policy.return_value = MemoryPolicy(memory_policy)
    active = {"origin": "desktop"}
    gateway.origin_provider = lambda: active["origin"]
    title, detail = _permission_gate_payload(case)
    if source == "context":
        # A desktop provider must not downgrade an inherited web context.
        with remote_origin():
            allowed = await handler(title, detail)
    else:
        wake = asyncio.Event()

        async def pi_ui_reader():
            await wake.wait()
            assert RUN_ORIGIN.get() == "desktop"
            return await handler(title, detail)

        reader = asyncio.create_task(pi_ui_reader())
        with remote_origin():
            active["origin"] = RUN_ORIGIN.get()
        wake.set()
        allowed = await reader
    assert allowed is False
    # Stronger than "did not await": no parser, policy, approval, or UI call at all.
    for dependency in untouched:
        assert dependency.mock_calls == []


async def test_app_permission_callback_provider_failure_denies_without_prompt(
    gateway: ToolGateway, permission_callback_env
) -> None:
    handler, untouched, _memory = permission_callback_env

    def unavailable():
        raise RuntimeError("active runtime unavailable")

    gateway.origin_provider = unavailable
    assert await handler("extension confirmation", "detail") is False
    for dependency in untouched:
        assert dependency.mock_calls == []


async def test_app_permission_callback_web_without_gateway_still_denies(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from limbowave import app

    prompt = AsyncMock(return_value=True)
    monkeypatch.setattr(app, "_ask_user", prompt)
    handler = app._make_permission_handler(None)
    with remote_origin():
        allowed = await handler("extension confirmation", "detail")
    assert allowed is False
    prompt.assert_not_called()
