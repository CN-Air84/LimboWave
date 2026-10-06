from types import SimpleNamespace

import httpx

from limbowave.web.app import create_app
from limbowave.web.auth import AuthStore
from limbowave.web.security import WebConfig


class FakeFacade:
    epoch = "test"

    def __init__(self):
        self.executed = []

    async def state(self):
        return {
            "server_epoch": self.epoch,
            "seq": 0,
            "available": True,
            "stream": None,
            "api_key": "SECRET",
        }

    async def models(self):
        return [{"id": "model", "name": "Model", "api_key": "SECRET", "url": "SECRET"}]

    async def conversations(self, **kwargs):
        return {
            "items": [
                {
                    "id": "c",
                    "title": "Chat",
                    "branches": [{"id": "b", "title": "Main"}],
                    "key": "SECRET",
                }
            ],
            "next_cursor": None,
        }

    async def messages(self, conversation_id, branch_id, **kwargs):
        return {
            "items": [{"id": "m", "role": "user", "content": "hi", "secret": "SECRET"}],
            "next_cursor": None,
        }

    async def execute(self, device_id, command):
        self.executed.append((device_id, command))
        return {
            "status": "accepted",
            "client_command_id": command["client_command_id"],
            "server_epoch": self.epoch,
            "secret": "SECRET",
        }

    def receipt(self, device_id, command_id):
        return {"status": "accepted", "client_command_id": command_id}


async def test_real_endpoints_and_strict_commands():
    facade, auth = FakeFacade(), AuthStore()
    issued = auth.issue("phone")
    app = create_app(facade, WebConfig(), auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as c:
        c.cookies.set("lw_session", issued["token"])
        for path in ("state", "models", "conversations", "conversations/c/messages?branch_id=b"):
            response = await c.get("/api/v1/" + path)
            assert response.status_code == 200 and "SECRET" not in response.text
        assert (await c.get("/api/v1/conversations?limit=101")).status_code == 400
        cmd = {
            "type": "send",
            "server_epoch": "test",
            "client_command_id": "cmd",
            "expected_revision": 0,
            "conversation_id": "c",
            "branch_id": "b",
            "text": "hi",
        }
        headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": issued["csrf_token"]}
        for extra in ({"confirmed": True}, {"run_origin": "desktop"}, {"tools": True}):
            response = await c.post("/api/v1/commands", json={**cmd, **extra}, headers=headers)
            assert response.status_code == 422
            assert "hi" not in response.text
        assert not facade.executed
        assert (await c.post("/api/v1/commands", json=cmd, headers=headers)).status_code == 200
        assert facade.executed[0][0] == issued["device_id"]
        assert (await c.get("/api/v1/commands/cmd")).json()["status"] == "accepted"


async def test_internal_error_does_not_echo_secret_or_path(caplog):
    async def broken():
        raise ValueError("SECRET C:/private/key.pem")

    facade = SimpleNamespace(state=broken)
    auth = AuthStore()
    issued = auth.issue("phone")
    app = create_app(facade, WebConfig(), auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as c:
        c.cookies.set("lw_session", issued["token"])
        response = await c.get("/api/v1/state")
        assert response.status_code == 500
        assert "SECRET" not in response.text and "private" not in response.text
        assert "SECRET" not in caplog.text


async def test_real_facade_http_dedup_conflict_and_epoch_restart():
    import runpy
    from pathlib import Path

    from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
    from limbowave.application.services.runtime_facade import RuntimeFacade
    from limbowave.application.services.session_controller import SessionController
    from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
    from limbowave.web.server import WebServer

    FakeKernel = runpy.run_path(
        str(Path(__file__).parents[1] / "unit" / "test_run_coordinator.py")
    )["FakeKernel"]

    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = FakeKernel()
    coordinator = RunCoordinator(
        kernel, factory, context=lambda: RunContext("fake", "fake", "test")
    )
    session = SessionController(kernel, coordinator)
    facade = RuntimeFacade(session, factory)
    web = WebServer(facade, WebConfig(port=0))
    try:
        await web.start()
        first_epoch = facade.epoch
        issued = web.auth.issue("phone")
        async with httpx.AsyncClient(base_url=web.url) as c:
            c.cookies.set("lw_session", issued["token"])
            headers = {"Origin": web.url, "X-CSRF-Token": issued["csrf_token"]}
            command = {
                "type": "new_session",
                "server_epoch": facade.epoch,
                "client_command_id": "draft",
                "expected_revision": facade.revision,
            }
            response = await c.post("/api/v1/commands", json=command, headers=headers)
            assert response.status_code == 200, response.text
            target = response.json()
            command = {
                "type": "send",
                "server_epoch": facade.epoch,
                "client_command_id": "send",
                "expected_revision": facade.revision,
                "conversation_id": target["conversation_id"],
                "branch_id": target["branch_id"],
                "text": "hello",
            }
            accepted = await c.post("/api/v1/commands", json=command, headers=headers)
            assert accepted.status_code == 200, accepted.text
            repeated = await c.post("/api/v1/commands", json=command, headers=headers)
            assert repeated.json() == accepted.json()
            assert kernel.sent == ["hello"]
            conflict = await c.post(
                "/api/v1/commands", json={**command, "client_command_id": "other"}, headers=headers
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "runtime_busy"
            await web.stop()
            assert not kernel.aborted
            assert session.busy
            await web.start()
            assert facade.epoch != first_epoch
            # A new pairing does not authorize replay of an old epoch's command.
            newer = web.auth.issue("phone")
            async with httpx.AsyncClient(base_url=web.url) as new_client:
                new_client.cookies.set("lw_session", newer["token"])
                result = await new_client.post(
                    "/api/v1/commands",
                    json=command,
                    headers={"Origin": web.url, "X-CSRF-Token": newer["csrf_token"]},
                )
                assert result.status_code == 409 and result.json()["code"] == "epoch_mismatch"
            assert kernel.sent == ["hello"]
    finally:
        await web.stop()
        if session.busy:
            await session.mark_interrupted()
        await session.wait_idle()
        await facade.close()
