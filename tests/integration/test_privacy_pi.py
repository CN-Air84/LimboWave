"""Inspect real provider payloads after restoring/reusing a Pi session."""

import json

from limbowave.infrastructure.pi_adapter import PiKernelAdapter, build_spawn_spec
from tests.integration.test_memory_pi import _context
from tests.integration.test_pi_adapter import _wait_settled
from tests.integration.test_pi_adapter import mock_models as mock_models
from tests.unit.test_memory_service import memory_stack as memory_stack


async def test_actual_pi_replaces_old_context_and_preserves_current_input(
    mock_models, mock_provider, tmp_path,
):
    _, log = mock_provider
    settings_file = mock_models / ".pi" / "agent" / "settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    settings["compaction"] = {"enabled": False, "reserveTokens": 100, "keepRecentTokens": 200}
    settings_file.write_text(json.dumps(settings), encoding="utf-8")
    adapter = PiKernelAdapter(
        build_spawn_spec(provider="mock", model_id="mock-model", cwd=tmp_path)
    )
    try:
        await adapter.start()
        await adapter.set_memory_context(_context(1))
        await adapter.send_message("OLD_PRIVATE_RUNTIME_CONTEXT " + "old phrase " * 1000)
        await _wait_settled(adapter)
        await adapter.set_memory_context(_context(2, **{
            "global": ["FORBIDDEN_private_GLOBAL"], "session": ["NEW_SAFE_MEMORY"],
            "privacy": {"history": "[user]\nSAFE_CANONICAL_HISTORY", "terms": ["private"]},
        }))
        # Explicit current user text must not be silently censored.
        await adapter.send_message("CURRENT_USER_private_IS_EXPLICIT " + "current padding " * 300)
        await _wait_settled(adapter)
        # Native summarization must never see the raw, excluded runtime history.
        try:
            compacted = await adapter.compact()
            assert not compacted.summary
        except RuntimeError as exc:
            assert "cancel" in str(exc).lower()
        await adapter.new_session()
        await adapter.set_memory_context(_context(3, **{
            "global": [], "session": [], "privacy": {"history": "", "terms": []},
        }))
        await adapter.send_message("FRESH_CHAT")
        await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
    bodies = [json.loads(line)["body"] for line in log.read_text(encoding="utf-8").splitlines()
              if "body" in json.loads(line)]
    assert len(bodies) == 3
    second = json.dumps(bodies[1]["messages"], ensure_ascii=False)
    assert "OLD_PRIVATE_RUNTIME_CONTEXT" not in second
    assert "FORBIDDEN_private_GLOBAL" not in second
    assert "GLOBAL_MEMORY_TEST" not in second and "BRANCH_MEMORY_TEST" not in second
    assert "SAFE_CANONICAL_HISTORY" in second and "NEW_SAFE_MEMORY" in second
    assert "CURRENT_USER_private_IS_EXPLICIT" in second
    third = json.dumps(bodies[2]["messages"])
    assert "FRESH_CHAT" in third
    assert "SAFE_CANONICAL_HISTORY" not in third and "CURRENT_USER" not in third

async def test_privacy_survives_compression_fork_retry_and_cold_restart(
    tmp_path, mock_provider, vault_key,
):
    import asyncio

    from limbowave.application.services.compression_service import CompressionService
    from limbowave.application.services.history_service import HistoryService
    from limbowave.application.services.privacy_service import PrivacyService
    from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
    from limbowave.composition import build_kernel
    from limbowave.domain.retry import RetryPolicy
    from limbowave.infrastructure.database.sqlite_repositories import sqlite_uow_factory
    from tests.integration.test_recovery_e2e import _paths, _seed, _send_and_wait

    port, log = mock_provider
    paths = _paths(tmp_path)
    _seed(paths, f"http://127.0.0.1:{port}/v1", vault_key)

    def runtime():
        setup = build_kernel(paths, vault_key)
        factory = sqlite_uow_factory(paths.data_root / "privacy.db", vault_key)
        context = RunContext(setup.logical_model_id, setup.endpoint_id, setup.routing_reason,
                             app_params=dict(setup.app_params),
                             retry_policy=RetryPolicy(max_attempts=2, base_delay_ms=0))
        return RunCoordinator(setup.kernel, factory, context=lambda: context), factory

    def last_body():
        return json.dumps([
            json.loads(line)["body"] for line in log.read_text(encoding="utf-8").splitlines()
            if "body" in json.loads(line)
        ][-1]["messages"])

    coord, factory = runtime()
    try:
        await coord.start()
        await _send_and_wait(coord, "OLD_EXCLUDED_CANARY")
        await coord.wait_idle()
        cid, bid = coord.conversation_id, coord.branch_id
        compression = CompressionService(factory)
        version = compression.create_version(cid, bid, tokens_before=1000,
                                             compression_model_id="mock-model",
                                             compression_endpoint_id="mock")
        compression.record_result(version.id, "OPAQUE_OLD_SUMMARY", 10)
        assert await coord.apply_compression(version.id)
        PrivacyService(factory).add("OLD_EXCLUDED_CANARY")
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.put(f"http://127.0.0.1:{port}/control/fail", json={
                "count": 1, "status": 503, "message": "ECONNRESET connection reset",
            })
            response.raise_for_status()
        await _send_and_wait(coord, "SAFE_NEXT_TURN")
        await coord.wait_idle()
        assert "OLD_EXCLUDED_CANARY" not in last_body()
        assert "OPAQUE_OLD_SUMMARY" not in last_body()
        messages = HistoryService(factory).branch_messages(bid)
        assert await coord.fork_message(messages[-1].id)
        await _send_and_wait(coord, "SAFE_CHILD_TURN")
        await coord.wait_idle()
        child = coord.branch_id
        assert "OLD_EXCLUDED_CANARY" not in last_body()
        assert "OPAQUE_OLD_SUMMARY" not in last_body()
        user = HistoryService(factory).branch_messages(child)[-2]
        assert await coord.retry_user_message(user.id)
        async with asyncio.timeout(60):
            while coord.busy:
                await asyncio.sleep(0.05)
        await coord.wait_idle()
        assert "OLD_EXCLUDED_CANARY" not in last_body()
        assert "OPAQUE_OLD_SUMMARY" not in last_body()
    finally:
        await coord.shutdown()
        factory.close()
    coord, factory = runtime()
    try:
        await coord.start()
        assert await coord.resume(cid, child)
        await _send_and_wait(coord, "AFTER_COLD_RESTART")
        await coord.wait_idle()
        assert "OLD_EXCLUDED_CANARY" not in last_body()
        assert "OPAQUE_OLD_SUMMARY" not in last_body()
        assert "SAFE_NEXT_TURN" in last_body()
    finally:
        await coord.shutdown()
        factory.close()


async def test_actual_pi_tool_gateway_blocks_external_params_and_nested_source_results(
    mock_models, mock_provider, tmp_path, memory_stack,
):
    import urllib.request
    from dataclasses import asdict
    from datetime import UTC, datetime

    from limbowave.application.services.permission_service import PermissionService
    from limbowave.application.services.tool_gateway import NetworkTools, ToolGateway
    from limbowave.domain.conversation import Message, MessageRole
    from limbowave.domain.permissions import PermissionPreset
    from limbowave.infrastructure.tools.ipc_server import IPC_ENV, ToolIpcServer

    service, factory = memory_stack
    source_text = "EXACT_PRIVATE_SOURCE_TOOL_RESULT"
    with factory() as uow:
        uow.messages.add(Message("source-tool", "c", "b", MessageRole.USER,
                                 source_text, datetime.now(UTC)))
        uow.commit()
    service.privacy.add("FORBIDDEN_NETWORK_CANARY", source_ids=["source-tool"])
    permissions = PermissionService(factory)
    permissions.set_preset("c", PermissionPreset.FULL_ACCESS)
    external_calls, results = [], []

    def search(query):
        external_calls.append(query)
        return {"count": 1, "results": [{"url": "https://example.com", "snippet": source_text}]}

    gateway = ToolGateway(permissions, tmp_path, network=NetworkTools(searcher=search))
    gateway.memory_service = service

    async def dispatch(tool, params, conversation_id, confirmed):
        result = gateway.invoke(tool, params, conversation_id, user_confirmed=confirmed)
        results.append(result)
        return asdict(result)

    async def consent(_title, _detail):
        return True  # Ordinary network consent is NOT privacy-rule removal.

    server = ToolIpcServer(dispatch, conversation_provider=lambda: "c")
    session = await server.start()
    adapter = PiKernelAdapter(build_spawn_spec(
        provider="mock", model_id="mock-model", cwd=tmp_path,
        env={IPC_ENV: session.to_env_value()},
    ))
    adapter.set_permission_handler(consent)
    port, log = mock_provider
    try:
        await adapter.start()
        for number, query in enumerate(["FORBIDDEN_NETWORK_CANARY", "safe query"], 1):
            await adapter.set_memory_context(_context(number, **{
                "global": [], "session": [], "privacy": {"history": "", "terms": []},
            }))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/toolcall", method="PUT",
                data=json.dumps({"name": "web_search", "id": f"call{number}",
                                 "arguments": {"query": query}}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            await adapter.send_message(f"SAFE_TOOL_REQUEST_{number}")
            await _wait_settled(adapter)
    finally:
        await adapter.shutdown()
        await server.stop()
    assert external_calls == ["safe query"]
    assert len(results) == 2 and all(not result.ok and result.data == {} for result in results)
    bodies = [json.loads(line)["body"] for line in log.read_text(encoding="utf-8").splitlines()
              if "body" in json.loads(line)]
    tool_messages = [message for body in bodies for message in body["messages"]
                     if message["role"] == "tool"]
    assert tool_messages
    assert source_text not in json.dumps(bodies)
    assert all("禁区" in json.dumps(message, ensure_ascii=False) for message in tool_messages)
