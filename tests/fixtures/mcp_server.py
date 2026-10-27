"""Local test-only MCP server. No real data or credentials."""

import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP(
    "limbowave-test",
    host="127.0.0.1",
    port=int(os.getenv("MCP_TEST_PORT", "8000")),
    stateless_http=True,
    json_response=True,
)


@server.tool()
def echo(text: str) -> str:
    return text


@server.tool()
def fail() -> str:
    raise ValueError("test tool failure")


@server.tool()
async def slow() -> str:
    await asyncio.sleep(60)
    return "late"


if __name__ == "__main__":
    server.run(transport="streamable-http" if "--http" in sys.argv else "stdio")
