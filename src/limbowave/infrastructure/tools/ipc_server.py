"""工具 IPC 服务端：把应用侧工具网关暴露给 Pi 扩展（§九.1）。

**为什么需要它**：模型发起的工具调用发生在 Pi 进程里。我们已经有应用侧的执行器
（PowerShell 执行器、文件工具、联网工具）与唯一的权限网关，**不希望在 TypeScript
里再实现一遍**（两份实现必然漂移）。所以扩展只做**薄代理**：把 `{tool, params}`
经本通道送给应用，拿回结构化结果原样返回。

    Pi 扩展（薄代理） ──TCP/JSONL──▶ 本服务 ──▶ ToolGateway ──▶ 执行器
                                          └─ 校验/权限/审计/输出限制全在应用侧

**通道形态与取舍**：

- **回环 TCP + 临时端口**，不用命名管道：Python 标准库没有命名管道支持，
  自己用 ctypes 包一层 CreateNamedPipe 的复杂度与风险都不小；而绑定 `127.0.0.1`
  的临时端口在本项目的威胁模型下**安全性等价**（见下）。**绝不绑 0.0.0.0**。
- **令牌经环境变量下发**。这里必须如实说明它的作用：同用户进程能读到彼此的
  环境变量，所以令牌**不是**针对同用户攻击者的安全边界——它防的是**误连**
  （另一个实例、乱跑的脚本打到这个端口）。这与合同 §七「同用户可写的文件视为
  同一本地信任边界」的设定一致，不额外承诺。
- 令牌比较用 :func:`secrets.compare_digest`（避免时序侧信道）——成本几乎为零。

**权限语义（重要）**：模型工具调用在到达这里之前，**已经过扩展的 `tool_call`
闸门**（应用侧策略引擎 + 用户确认）。因此本通道：

- 带 ``confirmed=true`` 的请求按「已同意」执行；
- 未带的请求**照常跑策略判定**，需要确认时**拒绝**——本通道**不能自行铸造授权**。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# 单条请求/响应的上限：工具参数可能带长文本，但不能无限大
MAX_FRAME_BYTES = 1024 * 1024

# 环境变量名：把 {host, port, token} 交给扩展（与 Python 侧同源）
IPC_ENV = "LIMBOWAVE_TOOL_IPC"
RATE_LIMIT_IPC_ENV = "LIMBOWAVE_RATE_LIMIT_IPC"


@dataclass(frozen=True, slots=True)
class ToolIpcSession:
    """一次通道会话的凭据。随内核一起创建，随内核关闭作废。

    **只有连接信息与令牌**——会话标识不在这里：用户会在运行中切换会话，
    把它烘进 session 会立刻过期。会话标识由调用时的 provider 提供（服务端权威）。
    """

    host: str
    port: int
    token: str

    def to_env_value(self) -> str:
        """放进环境变量的 JSON（**不含**密钥或其他敏感内容）。"""
        return json.dumps(
            {"host": self.host, "port": self.port, "token": self.token},
            ensure_ascii=False,
        )


@dataclass
class _Stats:
    """通道使用统计（供诊断；不记录参数内容）。"""

    requests: int = 0
    rejected: int = 0
    errors: int = 0
    tools: dict[str, int] = field(default_factory=dict)


class ToolIpcServer:
    """回环 TCP + JSONL 的工具调用服务端。

    ``dispatch`` 由调用方注入（通常是 ``ToolGateway.invoke`` 的包装），
    因此本类**不依赖网关实现**，可以独立测试。
    """

    def __init__(
        self,
        dispatch: Callable[[str, dict[str, Any], str, bool], Awaitable[dict[str, Any]]],
        *,
        conversation_provider: Callable[[], str | None] | None = None,
        host: str = "127.0.0.1",
        rate_limit: Callable[[str], float] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._rate_limit = rate_limit
        self._rate_session: ToolIpcSession | None = None
        # 会话标识**只由服务端提供**（运行时可能切换，不能烘进 session）
        self._conversation_provider = conversation_provider or (lambda: None)
        self._host = host
        self._server: asyncio.AbstractServer | None = None
        self._session: ToolIpcSession | None = None
        self.stats = _Stats()

    # ---------- 生命周期 ----------

    async def start(self) -> ToolIpcSession:
        """启动监听。端口由系统分配（避免与用户其他服务撞号）。

        ``limit`` 设为帧上限 + 余量：否则 ``readline()`` 会在默认 64KB 处先抛
        ``ValueError``，我们的「请求过大」检查根本没机会跑，连接直接崩。
        """
        self._server = await asyncio.start_server(
            self._handle, self._host, 0, limit=MAX_FRAME_BYTES + 4096
        )
        port = self._server.sockets[0].getsockname()[1] if self._server.sockets else 0
        self._session = ToolIpcSession(
            host=self._host, port=int(port), token=secrets.token_urlsafe(32)
        )
        if self._rate_limit is not None:
            self._rate_session = ToolIpcSession(
                host=self._host, port=int(port), token=secrets.token_urlsafe(32)
            )
        return self._session

    @property
    def rate_limit_session(self) -> ToolIpcSession | None:
        """独立令牌只可请求限流额度，绝不能用于调用工具。"""
        return self._rate_session

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        # 令牌作废：旧会话不能再连（新内核会拿到新令牌）
        self._session = None
        self._rate_session = None

    @property
    def session(self) -> ToolIpcSession | None:
        return self._session

    # ---------- 连接处理 ----------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await self._reply(writer, {"ok": False, "error": "请求过大"})
                    break
                response = await self._process(line)
                await self._reply(writer, response)
                if response.get("_close"):
                    break
        except ValueError:
            # 单行超过 stream limit：明确拒绝，而不是让连接带着异常崩掉
            with contextlib.suppress(Exception):
                await self._reply(writer, {"ok": False, "error": "请求过大"})
            self.stats.rejected += 1
        except (OSError, asyncio.IncompleteReadError):
            pass  # 对端断开：正常结束
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _process(self, line: bytes) -> dict[str, Any]:
        """处理一条请求。**认证失败不区分原因**（不给探测者额外信息）。"""
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.stats.rejected += 1
            return {"ok": False, "error": "请求不是合法 JSON", "_close": True}

        if not isinstance(request, dict):
            self.stats.rejected += 1
            return {"ok": False, "error": "请求格式错误", "_close": True}

        session = self._session
        if session is None:
            self.stats.rejected += 1
            return {"ok": False, "error": "通道未启用", "_close": True}

        token = request.get("token")
        if request.get("operation") == "rate_limit":
            rate_session = self._rate_session
            if (rate_session is None or not isinstance(token, str)
                    or not secrets.compare_digest(token, rate_session.token)):
                self.stats.rejected += 1
                return {"ok": False, "error": "认证失败", "_close": True}
            endpoint_id = request.get("endpoint_id")
            if not isinstance(endpoint_id, str) or not endpoint_id:
                return {"ok": False, "error": "缺少 endpoint_id"}
            assert self._rate_limit is not None
            try:
                delay = self._rate_limit(endpoint_id)
            except Exception:
                return {"ok": False, "error": "站点请求已停止"}
            return {"ok": True, "data": {"delay_seconds": delay}}
        if not isinstance(token, str) or not secrets.compare_digest(token, session.token):
            self.stats.rejected += 1
            return {"ok": False, "error": "认证失败", "_close": True}

        tool = request.get("tool")
        params = request.get("params")
        if not isinstance(tool, str) or not isinstance(params, dict):
            self.stats.errors += 1
            return {"ok": False, "error": "缺少 tool 或 params"}

        # 会话标识**一律取服务端当前会话**：客户端自报的值不采信，
        # 否则审计会串台（谁都能声称自己在别的会话里）
        conversation_id = self._conversation_provider() or ""

        self.stats.requests += 1
        self.stats.tools[tool] = self.stats.tools.get(tool, 0) + 1
        try:
            result = await self._dispatch(
                tool, params, conversation_id, bool(request.get("confirmed", False))
            )
        except Exception as exc:  # 派发实现不该把异常抛到通道上
            self.stats.errors += 1
            return {"ok": False, "error": f"工具执行失败：{exc}"}
        if not result.get("ok", False):
            self.stats.errors += 1
        return result

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body.pop("_close", None)  # 内部控制字段不进协议
        writer.write((json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8"))
        with contextlib.suppress(Exception):
            await writer.drain()


def parse_session(value: str | None) -> ToolIpcSession | None:
    """解析环境变量里的会话凭据。扩展侧同样按这个结构发请求。"""
    if not value:
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    host = data.get("host")
    port = data.get("port")
    token = data.get("token")
    if not isinstance(host, str) or not isinstance(port, int) or not isinstance(token, str):
        return None
    return ToolIpcSession(host=host, port=port, token=token)
