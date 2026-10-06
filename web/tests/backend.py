"""Real WebServer + RuntimeFacade + RunCoordinator browser-test fixture.

Only the model kernel is fake. HTTP, cookies, CSRF, SSE, commands, receipts,
repository reads and history pagination use the actual application code.
The test process binds loopback only; stdin EOF performs graceful shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys

from limbowave.application.services.run_coordinator import RunContext, RunCoordinator
from limbowave.application.services.runtime_facade import RuntimeFacade
from limbowave.application.services.session_controller import SessionController
from limbowave.infrastructure.memory_repositories import InMemoryStore, in_memory_uow_factory
from limbowave.web.password_gate import PasswordGate
from limbowave.web.security import WebConfig
from limbowave.web.server import WebServer
from tests.unit.test_run_coordinator import FakeKernel


class StreamingKernel(FakeKernel):
    """Deterministic fake upstream, with real kernel events and cancellation."""

    def __init__(self) -> None:
        super().__init__()
        self.response_task: asyncio.Task[None] | None = None
        self.response_text = ""

    async def send_message(self, text: str, *, images=None) -> None:
        await super().send_message(text, images=images)
        self.aborted = False
        self.response_text = ""

        async def respond() -> None:
            await asyncio.sleep(0.2)
            self.emit("message.start", {"message": {"role": "assistant"}})
            self.emit(
                "message.update",
                {"assistantMessageEvent": {"type": "thinking_delta", "delta": "Thinking safely"}},
            )
            # Emit metadata only; no tool is actually executed by this fake kernel.
            self.emit("tool.start", {"toolName": "Fixture lookup", "toolCallId": "fixture-tool"})
            self.emit(
                "tool.end",
                {
                    "toolName": "Fixture lookup",
                    "toolCallId": "fixture-tool",
                    "isError": False,
                },
            )
            for word in ("Hello ", "from ", "LimboWave."):
                self.response_text += word
                self.emit(
                    "message.update",
                    {"assistantMessageEvent": {"type": "text_delta", "delta": word}},
                )
                await asyncio.sleep(0.4)
            if text == "slow":
                await asyncio.sleep(20)
            self.emit(
                "message.end",
                {
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "Hello from LimboWave."}],
                    }
                },
            )
            self.emit("run.settled", {})

        self.response_task = asyncio.create_task(respond())

    async def abort(self) -> None:
        await super().abort()
        if self.response_task:
            self.response_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.response_task
        self.emit(
            "message.end",
            {
                "message": {
                    "role": "assistant",
                    "stopReason": "aborted",
                    "content": [{"type": "text", "text": self.response_text}],
                }
            },
        )
        self.emit("run.settled", {})


async def main(port: int) -> None:
    store = InMemoryStore()
    factory = in_memory_uow_factory(store)
    kernel = StreamingKernel()
    coordinator = RunCoordinator(
        kernel, factory, context=lambda: RunContext("fake", "fake", "test")
    )
    session = SessionController(kernel, coordinator)

    async def select(identity: str) -> None:
        await kernel.set_model("fake", identity)

    facade = RuntimeFacade(
        session,
        factory,
        models_provider=lambda: [{"id": "fake", "name": "Test model"}],
        select_model=select,
    )
    web = WebServer(
        facade,
        WebConfig(host="127.0.0.1", port=port, requests_per_minute=10000),
        password_gate=PasswordGate(lambda p: p == "test-vault-password"),
    )

    def announce() -> None:
        pairing = web.open_pairing()
        print(
            json.dumps({"url": web.url, "ticket": pairing["ticket"], "code": pairing["code"]}),
            flush=True,
        )

    try:
        await web.start()
        announce()
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = line.strip()
            if command == "stop":
                break
            if command == "restart":
                await web.stop()
                await web.start()
                announce()
            elif command == "approve":
                for pending in web.list_pending():
                    web.approve(pending["request_id"])
            elif command == "pair":
                announce()
    finally:
        await web.stop()
        if session.busy:
            await session.abort()
        await session.wait_idle()
        await facade.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    asyncio.run(main(parser.parse_args().port))
