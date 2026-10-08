"""本地 mock provider：实现 OpenAI Chat Completions 兼容接口，供离线验证 Pi 集成。

存在意义：Pi 需要一个真实模型端点才能跑通多轮。用 mock 可以做到零密钥、零成本、
完全确定性地验证，而且**它记录下来的请求体就是铁证**——第二轮请求里到底有没有
第一轮的对话，直接读日志即可判定，不需要依赖模型的自述。

用法：
    python tools/pi-verify/mock_provider.py --port 8787 --log requests.jsonl

日志每行一个 JSON：{"turn": n, "path": ..., "body": {...}}
助手回复固定为 "ACK turn=<n> messages=<m>"，其中 m 是本次收到的消息条数，
便于从 Pi 侧直接观察它究竟送出了多少条消息。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_turn = 0
_log_path: Path | None = None
_pending_tool_calls: list[dict[str, Any]] = []
# 失败注入：_fail_next > 0 时，接下来的请求返回错误（默认连接重置式失败用 503）。
_fail_next = 0
_fail_status = 503
_fail_message: str | None = None


def _tool_call_chunk(model: str, *, index: int, call_id: str, name: str, args_json: str) -> bytes:
    payload = {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": args_json},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _record(entry: dict[str, Any]) -> None:
    if _log_path is None:
        return
    with _lock, _log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _completion_chunk(model: str, *, delta: dict[str, Any], finish_reason: str | None) -> bytes:
    payload = {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 保持 stderr 干净
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(
                200, {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}
            )
            return
        self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})

    def do_PUT(self) -> None:
        # 控制通道：PUT /control/toolcall 注册一次"下一个助手响应发出该工具调用"
        if self.path.rstrip("/").endswith("/control/toolcall"):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                spec = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "invalid json"}})
                return
            with _lock:
                _pending_tool_calls.append(spec)
            self._send_json(200, {"ok": True})
            return
        # 控制通道：PUT /control/fail，body {"count": n, "status": 503}，接下来 n 次请求失败
        if self.path.rstrip("/").endswith("/control/fail"):
            global _fail_next, _fail_status, _fail_message
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                spec = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "invalid json"}})
                return
            with _lock:
                _fail_next = int(spec.get("count", 1))
                _fail_status = int(spec.get("status", 503))
                _fail_message = spec.get("message")
            self._send_json(200, {"ok": True, "fail_next": _fail_next, "status": _fail_status})
            return
        self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})

    def do_POST(self) -> None:
        global _turn, _fail_next

        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json"}})
            return

        # 先记录每一条请求（含失败注入的），再决定是否返回错误。
        # 失败注入的请求也必须入日志，否则探针会把"发了但被 503"误判成"没发"。
        with _lock:
            _turn += 1
            turn = _turn
            if _fail_next > 0:
                _fail_next -= 1
                status = _fail_status
            else:
                status = 200

        messages = body.get("messages") or []
        tools = body.get("tools") or []
        _record(
            {
                "turn": turn,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
                "message_count": len(messages),
                "tool_count": len(tools),
                "http_status": status,
            }
        )

        if status != 200:
            self._send_json(
                status,
                { "error": {
                    "message": _fail_message or f"mock injected failure ({status})",
                    "type": "server_error",
                }},
            )
            return

        model = body.get("model", "mock-model")

        # 有排队的工具调用时，助手响应发出该工具调用（流式 finish_reason=tool_calls）。
        # 探针用此让模型真正调用工具，以走标准 tool_call 钩子路径。
        with _lock:
            tool_call = _pending_tool_calls.pop(0) if _pending_tool_calls else None

        if tool_call is not None:
            call_id = tool_call.get("id", f"call_{uuid.uuid4().hex[:8]}")
            name = tool_call["name"]
            args_json = json.dumps(tool_call.get("arguments", {}), ensure_ascii=False)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(
                    _completion_chunk(model, delta={"role": "assistant"}, finish_reason=None)
                )
                self.wfile.write(
                    _tool_call_chunk(
                        model, index=0, call_id=call_id, name=name, args_json=args_json
                    )
                )
                self.wfile.write(_completion_chunk(model, delta={}, finish_reason="tool_calls"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self._send_json(
                    200,
                    {
                        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": call_id,
                                            "type": "function",
                                            "function": {"name": name, "arguments": args_json},
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )
            return

        reply = f"ACK turn={turn} messages={len(messages)}"

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(
                _completion_chunk(model, delta={"role": "assistant"}, finish_reason=None)
            )
            self.wfile.write(_completion_chunk(model, delta={"content": reply}, finish_reason=None))
            self.wfile.write(_completion_chunk(model, delta={}, finish_reason="stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )


def main() -> int:
    global _log_path

    parser = argparse.ArgumentParser(description="Pi 离线验证用 mock provider")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", type=Path, default=Path("mock-requests.jsonl"))
    args = parser.parse_args()

    _log_path = args.log.resolve()
    _log_path.write_text("", encoding="utf-8")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock provider 就绪：http://{args.host}:{args.port}/v1  日志={_log_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
