"""P0-GATE-04b 实机探针：模型真正调用工具时，tool_call 钩子能否阻断。

与 gate_04_tool_block.py 的区别：那里用 RPC `bash` 命令（已知会绕过 tool_call 钩子——
那是一个**独立的发现**）；这里让**模型自己发起工具调用**，走标准 tool_call 路径。

流程：
  1. 在工作目录放一个含机密的 canary.txt。
  2. 通过 mock 的 PUT /control/toolcall 注册：下一个助手响应发出 read(canary.txt)。
  3. prompt → mock 返回 read 工具调用 → Pi 拦截（tool_call）→ 执行或阻断。
  4. 看下一轮请求（含 tool 结果）里有没有机密内容。

判定：
  - allow-all：机密应出现在后续请求（基线，证明 read 真的能读到文件）。
  - block-all：机密不应出现，且 tool 结果应含阻断原因。

用法：python tools/pi-verify/gate_04b_model_tool_block.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import PiRpcClient, default_env, resolve_pi_command

MOCK_PORT = 8787
EXT = Path(__file__).resolve().parent / "extensions" / "gate-04-tool-gate.ts"
SECRET = "CANARY-SECRET-9f3e"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def start_mock(log_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "mock_provider.py"),
            "--port",
            str(MOCK_PORT),
            "--log",
            str(log_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def register_tool_call(name: str, arguments: dict[str, Any]) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{MOCK_PORT}/control/toolcall",
        data=json.dumps({"name": name, "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def bodies_blob(log_path: Path) -> str:
    return json.dumps([e["body"] for e in read_jsonl(log_path)], ensure_ascii=False)


def run_mode(workdir: Path, tag: str, mode: str, canary: Path, mock_log: Path) -> dict[str, Any]:
    argv = [
        *resolve_pi_command(),
        "--mode",
        "rpc",
        "--no-session",
        "--provider",
        "mock",
        "--model",
        "mock/mock-model",
        "--no-approve",
        "--no-context-files",
        "-e",
        str(EXT),
    ]
    ext_log = workdir / f"gate04b-{tag}.jsonl"
    env_extra = {"GATE04_MODE": mode, "GATE04_LOG": str(ext_log)}

    client = PiRpcClient(argv, cwd=workdir, env=default_env(env_extra))
    client.start()
    try:
        register_tool_call("read", {"path": canary.name})
        client.request({"type": "prompt", "message": "请读取文件"}, timeout=30)
        settled = client.wait_for_agent_settled(timeout=90)
    finally:
        client.close(timeout=15)
    time.sleep(0.5)

    ext_entries = read_jsonl(ext_log)
    tool_calls = [e for e in ext_entries if e.get("phase") == "tool_call"]
    blob = bodies_blob(mock_log)
    return {
        "settled": settled,
        "hook_fired": any(e.get("toolName") == "read" for e in tool_calls),
        "secret_leaked": SECRET in blob,
        "block_reason_present": "GATE04 blocked" in blob,
        "tool_result_seen": '"role": "tool"' in blob or '"role":"tool"' in blob,
    }


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate04b-"))
    mock_log = workdir / "mock-requests.jsonl"
    canary = workdir / "canary.txt"
    canary.write_text(SECRET, encoding="utf-8")

    print("=== 启动 mock provider ===")
    mock = start_mock(mock_log)
    time.sleep(1.5)

    try:
        # --- allow-all：基线，read 应真的读到文件 ---
        print("=== 阶段 1：allow-all（基线，机密应进入上下文）===")
        mock_log.write_text("", encoding="utf-8")
        a = run_mode(workdir, "allow", "allow-all", canary, mock_log)
        check("allow-all：tool_call 钩子被触发", a["hook_fired"])
        check("allow-all：agent_settled", a["settled"])
        check("allow-all：read 真正执行（机密进入上下文）", a["secret_leaked"])

        # --- block-all：应阻断，机密不得进入上下文 ---
        print("\n=== 阶段 2：block-all（机密不得进入上下文）===")
        mock_log.write_text("", encoding="utf-8")
        b = run_mode(workdir, "block", "block-all", canary, mock_log)
        check("block-all：tool_call 钩子被触发", b["hook_fired"])
        check("block-all：agent_settled", b["settled"])
        check("block-all：机密未进入上下文（执行被阻断）", not b["secret_leaked"])
        check("block-all：tool 结果含阻断原因", b["block_reason_present"])
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    print("\n=== 建议结论 ===")
    if a["secret_leaked"] and not b["secret_leaked"] and b["hook_fired"] and not failed:
        print("模型调用路径：tool_call 钩子可有效阻断（机密未泄露）")
    elif not a["secret_leaked"]:
        print("基线不成立：allow-all 下 read 都没读到文件，结论无效，需先修 mock")
    else:
        print("模型调用路径：tool_call 钩子无法阻断，机密泄露")
    print(f"\n工作目录：{workdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
