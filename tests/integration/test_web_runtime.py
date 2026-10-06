"""Real TCP/HTTP -> authentication -> RuntimeFacade -> RunCoordinator -> FakeKernel.

No facade methods, command receipts or history responses are mocked. The sole fake
is the existing programmable kernel; the real in-memory repository backs history.
"""

from __future__ import annotations

import asyncio
import json
import runpy
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.application.services.runtime_facade import RuntimeFacade
from limbowave.application.services.session_controller import SessionController
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.web.server import WebConfig, WebServer

# Tests are not an importable package in this checkout. Reuse its canonical
# FakeKernel explicitly rather than depending on pytest's collection/import order.
FakeKernel = runpy.run_path(str(Path(__file__).parents[1] / "unit" / "test_run_coordinator.py"))[
    "FakeKernel"
]

SECRET = "sk-model-secret-DO-NOT-EXPOSE"
PRIVATE_PATH = "C:/private/provider-credentials.json"


@dataclass
class RuntimeHTTP:
    web: WebServer
    facade: RuntimeFacade
    session: SessionController
    kernel: Any
    store: InMemoryStore
    clients: list[httpx.AsyncClient]
    releases: list[asyncio.Event] = field(default_factory=list)
    model_calls: list[str] = field(default_factory=list)

    async def command(
        self, client: httpx.AsyncClient, kind: str, command_id: str, **fields: Any
    ) -> dict[str, Any]:
        response = await client.get("/api/v1/state")
        assert response.status_code == 200, response.text
        state = response.json()
        return {
            "type": kind,
            "server_epoch": state["server_epoch"],
            "expected_revision": state["revision"],
            "client_command_id": command_id,
            **fields,
        }

    async def draft(self, client: httpx.AsyncClient, identity: str) -> dict[str, str]:
        command = await self.command(client, "new_session", identity)
        response = await client.post("/api/v1/commands", json=command)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"
        return {key: response.json()[key] for key in ("conversation_id", "branch_id")}

    def hold_prompt(self) -> tuple[asyncio.Event, asyncio.Event]:
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)

        async def before_prompt() -> None:
            entered.set()
            await release.wait()

        self.session.coordinator().before_prompt = before_prompt
        return entered, release

    async def settle(self, text: str) -> None:
        self.kernel.say(text)
        self.kernel.emit("run.settled", {})
        await self.session.wait_idle(timeout=2)


@pytest.fixture
async def runtime_http() -> AsyncIterator[RuntimeHTTP]:
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = FakeKernel()
    coordinator = RunCoordinator(
        kernel, factory, context=lambda: RunContext("fake", "fake", "test")
    )
    session = SessionController(kernel, coordinator)
    model_calls: list[str] = []

    async def select_model(model_id: str) -> None:
        model_calls.append(model_id)
        raise RuntimeError(f"provider rejected {SECRET}; credentials: {PRIVATE_PATH}")

    facade = RuntimeFacade(
        session,
        factory,
        models_provider=lambda: [
            {"id": "broken", "name": "Configured model", "api_key": SECRET, "url": PRIVATE_PATH}
        ],
        select_model=select_model,
    )
    web = WebServer(facade, WebConfig(port=0))
    harness = RuntimeHTTP(web, facade, session, kernel, store, [], model_calls=model_calls)
    try:
        await web.start()
        async with AsyncExitStack() as stack:
            for index in range(2):
                client = await stack.enter_async_context(
                    httpx.AsyncClient(base_url=web.url, timeout=3, trust_env=False)
                )
                bootstrap = await client.get("/api/v1/pair/status")
                assert bootstrap.status_code == 200
                ticket = web.open_pairing()["ticket"]
                paired = await client.post(
                    "/api/v1/pair",
                    json={"ticket": ticket, "name": f"phone-{index}"},
                    headers={"Origin": web.url, "X-CSRF-Token": bootstrap.json()["csrf_token"]},
                )
                assert paired.status_code == 200, paired.text
                assert "token" not in paired.json()
                client.headers.update(
                    {"Origin": web.url, "X-CSRF-Token": paired.json()["csrf_token"]}
                )
                harness.clients.append(client)
            assert len(web.list_devices()) == 2
            yield harness
    finally:
        # Release blocked starts even if an assertion failed, before stopping the
        # HTTP service; model ownership and finalization remain the desktop's.
        for release in harness.releases:
            release.set()
        await web.stop()
        if session.busy:
            await session.mark_interrupted()
        await session.wait_idle(timeout=2)
        await facade.close()


@pytest.mark.parametrize("same_target", [True, False])
async def test_two_devices_race_only_one_run_and_each_receipt_is_private(runtime_http, same_target):
    h = runtime_http
    first, second = h.clients
    target = await h.draft(first, "draft")
    other_target = target if same_target else await h.draft(second, "other-draft")
    entered, release = h.hold_prompt()
    common = await h.command(first, "send", "same-command-id", **target)
    commands = [
        {**common, **command_target, "text": text}
        for command_target, text in ((target, "phone one"), (other_target, "phone two"))
    ]
    tasks = [
        asyncio.create_task(client.post("/api/v1/commands", json=command))
        for client, command in zip(h.clients, commands, strict=True)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        done, pending = await asyncio.wait(tasks, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == len(pending) == 1, "the loser must reject while the winner is held"
        rejected = next(iter(done)).result()
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "runtime_busy"
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(i for i, response in enumerate(responses) if response.status_code == 200)
        accepted = responses[winner].json()
        assert accepted["status"] == "accepted"
        assert h.kernel.sent == [commands[winner]["text"]]
        assert len(h.store.runs) == 1
        assert len(h.store.messages) == 1
        states = [(await client.get("/api/v1/state")).json() for client in h.clients]
        assert states[0]["run_id"] == states[1]["run_id"] == accepted["run_id"]
        assert all(state["busy"] for state in states)
        # Same client_command_id is deliberately used by both devices. A receipt
        # is scoped to the authenticated cookie, not to a caller-supplied device.
        receipts = [
            (await client.get("/api/v1/commands/same-command-id")).json() for client in h.clients
        ]
        assert receipts[winner] == accepted
        assert receipts[1 - winner]["status"] == "rejected"
        assert receipts[1 - winner]["error"]["code"] == "runtime_busy"
        replay = await h.clients[winner].post("/api/v1/commands", json=commands[winner])
        assert replay.json() == accepted and len(h.kernel.sent) == 1
        await h.settle("shared answer")
        for client in (first, second):
            state = (await client.get("/api/v1/state")).json()
            assert not state["busy"]
            assert state["run_id"] == accepted["run_id"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("disconnect_at", ["pending", "accepted_headers"])
async def test_lost_http_response_recovers_by_get_without_second_execution(
    runtime_http, disconnect_at
):
    h = runtime_http
    client, other_device = h.clients
    target = await h.draft(client, "draft")
    entered, release = h.hold_prompt()
    command = await h.command(client, "send", "lost-response", text="send exactly once", **target)
    body = json.dumps(command).encode()
    url = urlsplit(h.web.url)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    request = (
        f"POST /api/v1/commands HTTP/1.1\r\nHost: {url.netloc}\r\n"
        f"Origin: {h.web.url}\r\nCookie: lw_session={client.cookies.get('lw_session')}\r\n"
        f"X-CSRF-Token: {client.headers['X-CSRF-Token']}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    try:
        writer.write(request + body)
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 2)
        pending = await client.get("/api/v1/commands/lost-response")
        assert pending.status_code == 200 and pending.json()["status"] == "pending"
        assert (await other_device.get("/api/v1/commands/lost-response")).status_code == 404
        if disconnect_at == "accepted_headers":
            release.set()
            # Acceptance has reached the wire; lose the connection without ever
            # consuming/deserializing its receipt body.
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert headers.startswith(b"HTTP/1.1 200")
        writer.close()
        await writer.wait_closed()
        release.set()
        async with asyncio.timeout(2):
            while True:
                recovered = await client.get("/api/v1/commands/lost-response")
                assert recovered.status_code == 200
                if recovered.json()["status"] != "pending":
                    break
                await asyncio.sleep(0.01)
        receipt = recovered.json()
        assert receipt["status"] == "accepted"
        assert receipt["run_id"] == h.session.run_id
        assert h.kernel.sent == ["send exactly once"]
        assert len(h.store.messages) == len(h.store.runs) == 1
        assert h.session.busy and not h.kernel.aborted
        # Recovery above used GET only. An explicit later retry remains idempotent.
        retry = await client.post("/api/v1/commands", json=command)
        assert retry.json() == receipt and h.kernel.sent == ["send exactly once"]
    finally:
        release.set()
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("kind", ["select_model", "send"])
async def test_model_selection_exception_is_sanitized_and_receipted(runtime_http, caplog, kind):
    h = runtime_http
    client, other_device = h.clients
    target = await h.draft(client, "draft")
    models = await client.get("/api/v1/models")
    assert models.json() == [{"id": "broken", "name": "Configured model"}]
    fields = {"model_id": "broken"}
    if kind == "send":
        fields.update(target, text="must not reach model")
    command = await h.command(client, kind, "failing-model", **fields)
    response = await client.post("/api/v1/commands", json=command)
    assert response.status_code == 500
    error = response.json()
    assert set(error) == {"code", "message", "request_id", "retryable"}
    assert error["code"] == "command_failed" and not error["retryable"]
    receipt = await client.get("/api/v1/commands/failing-model")
    assert receipt.status_code == 200
    assert receipt.json()["status"] == "failed"
    assert receipt.json()["error"] == {"code": "command_failed", "status": 500}
    assert (await other_device.get("/api/v1/commands/failing-model")).status_code == 404
    repeated = await client.post("/api/v1/commands", json=command)
    assert repeated.status_code == 500
    assert h.model_calls == ["broken"]
    assert not h.kernel.sent and not h.store.runs and not h.store.messages
    assert not h.session.busy
    assert h.kernel.model_id == "fake"
    # Failure must release the coordinator reservation for the next valid command.
    valid = await h.command(client, "send", "after-failure", text="recovered", **target)
    assert (await client.post("/api/v1/commands", json=valid)).status_code == 200
    assert h.kernel.sent == ["recovered"]
    for text in (models.text, response.text, receipt.text, repeated.text, caplog.text):
        assert SECRET not in text and PRIVATE_PATH not in text
        assert "provider rejected" not in text


async def test_history_pagination_during_live_run_does_not_switch_execution_target(runtime_http):
    h = runtime_http
    client, observer = h.clients
    historical = await h.draft(client, "historical")
    for index in range(3):
        command = await h.command(
            client, "send", f"history-{index}", text=f"question {index}", **historical
        )
        response = await client.post("/api/v1/commands", json=command)
        assert response.status_code == 200, response.text
        await h.settle(f"answer {index}")
    active = await h.draft(client, "active")
    command = await h.command(client, "send", "live", text="keep generating", **active)
    assert (await client.post("/api/v1/commands", json=command)).status_code == 200
    before = (await client.get("/api/v1/state")).json()
    sent = list(h.kernel.sent)
    assert before["busy"] and before["conversation_id"] == active["conversation_id"]

    conversations, cursor = [], None
    for _ in range(10):
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = await observer.get("/api/v1/conversations", params=params)
        assert page.status_code == 200, page.text
        data = page.json()
        assert len(data["items"]) <= 1
        conversations.extend(data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert cursor is None, "conversation pagination did not terminate"
    assert {item["id"] for item in conversations} == {
        historical["conversation_id"],
        active["conversation_id"],
    }
    assert len(conversations) == 2

    messages, cursor = [], None
    page_count = 0
    for _ in range(10):
        params = {
            "branch_id": historical["branch_id"],
            "limit": 2,
            **({"cursor": cursor} if cursor else {}),
        }
        page = await observer.get(
            f"/api/v1/conversations/{historical['conversation_id']}/messages", params=params
        )
        assert page.status_code == 200, page.text
        data = page.json()
        assert 0 < len(data["items"]) <= 2
        messages = data["items"] + messages  # each page old->new, cursor goes further back
        cursor = data["next_cursor"]
        page_count += 1
        if cursor is None:
            break
    assert cursor is None and page_count == 3
    assert len({message["id"] for message in messages}) == 6
    assert [message["content"] for message in messages] == [
        "question 0",
        "answer 0",
        "question 1",
        "answer 1",
        "question 2",
        "answer 2",
    ]
    after = (await observer.get("/api/v1/state")).json()
    assert after == before
    assert h.session.conversation_id == active["conversation_id"]
    assert h.session.branch_id == active["branch_id"]
    assert h.kernel.sent == sent and not h.kernel.aborted
