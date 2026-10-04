"""P0-GATE-04 实机探针：tool_call 能否阻断全部实际工具执行。

策略：直接用 RPC `bash` 命令发起一个带可观测副作用的命令（touch 哨兵文件）。
这绕过了"模型是否调用工具"的随机性——`bash` 命令是 Pi 侧的 shell 执行路径，
其执行与内置 bash 工具共享同一条 exec 链。

三阶段：
  1. 不加载扩展      → 哨兵文件应被创建（证明执行路径本身有效，无副作用是执行器问题）。
  2. block-all 扩展  → 哨兵文件不应被创建（证明钩子阻断了执行）。
  3. no-response 扩展 → 哨兵文件不应被创建（证明无响应即默认不执行），随后强杀进程。

用法：python tools/pi-verify/gate_04_tool_block.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import PiRpcClient, default_env, resolve_pi_command

EXT = Path(__file__).resolve().parent / "extensions" / "gate-04-tool-gate.ts"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def run_bash_probe(
    workdir: Path, tag: str, mode: str | None, canary: Path, *, timeout: float = 20.0
) -> dict[str, Any]:
    """发起一轮 RPC bash 命令，返回执行结果摘要。"""
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
    ]
    env_extra: dict[str, str] = {}
    if mode is not None:
        argv += ["-e", str(EXT)]
        env_extra["GATE04_MODE"] = mode
        env_extra["GATE04_LOG"] = str(workdir / f"gate04-{tag}.jsonl")

    client = PiRpcClient(argv, cwd=workdir, env=default_env(env_extra))
    client.start()
    out: dict[str, Any] = {"returncode": None, "response": None, "canary": False, "killed": False}
    try:
        cmd = f"python -c \"from pathlib import Path; Path(r'{canary}').write_text('x')\""
        if mode == "no-response":
            # no-response 模式会无限等待，改为在后台发起，超时后强杀
            client.send({"type": "bash", "command": cmd})
            time.sleep(timeout)
            out["canary"] = canary.exists()
            client.kill()
            out["killed"] = True
            return out

        response = client.request({"type": "bash", "command": cmd}, timeout=timeout)
        out["response"] = response
        time.sleep(0.3)  # 留出文件落盘时间
        out["canary"] = canary.exists()
    except TimeoutError as exc:
        out["response"] = {"timeout": str(exc)}
        out["canary"] = canary.exists()
    finally:
        client.close(timeout=10)
        out["returncode"] = client.returncode
    return out


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate04-"))

    # --- 阶段 1：无扩展，确认执行路径有效 ---
    print("=== 阶段 1：无扩展（应执行，创建哨兵文件）===")
    c1 = workdir / "canary-1.txt"
    r1 = run_bash_probe(workdir, "none", None, c1)
    ok1 = r1["canary"]
    check("无扩展时命令被执行（基线）", ok1, f"canary 存在={ok1}")
    resp1 = r1["response"] or {}
    data1 = resp1.get("data") or {}
    check(
        "无扩展时返回执行结果",
        resp1.get("success") is True,
        f"exitCode={data1.get('exitCode')}",
    )

    # --- 阶段 2：allow-all 扩展，确认钩子放行 ---
    print("\n=== 阶段 2：allow-all 扩展（应执行）===")
    c2 = workdir / "canary-2.txt"
    r2 = run_bash_probe(workdir, "allow", "allow-all", c2)
    check("allow-all 时命令被执行", r2["canary"], f"canary 存在={r2['canary']}")

    # --- 阶段 3：block-all 扩展，应阻断 ---
    print("\n=== 阶段 3：block-all 扩展（应阻断，不创建哨兵文件）===")
    c3 = workdir / "canary-3.txt"
    r3 = run_bash_probe(workdir, "block", "block-all", c3)
    check("block-all 时命令未执行", not r3["canary"], f"canary 存在={r3['canary']}")
    resp3 = r3["response"] or {}
    data3 = resp3.get("data") or {}
    blocked_marker = json.dumps(resp3)
    check(
        "阻断返回可辨识（error/cancelled/reason）",
        ("block" in blocked_marker.lower())
        or (data3.get("cancelled") is True)
        or (data3.get("isError") is True),
        str(resp3)[:80],
    )

    # --- 阶段 4：no-response 扩展，模拟断连 ---
    print("\n=== 阶段 4：no-response 扩展（模拟断连，应不执行）===")
    c4 = workdir / "canary-4.txt"
    r4 = run_bash_probe(workdir, "noresp", "no-response", c4, timeout=12.0)
    check("无响应时命令未执行", not r4["canary"], f"canary 存在={r4['canary']}")
    check("no-response 后进程被强杀", r4["killed"])

    # --- 汇总 ---
    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")

    print("\n=== 建议结论 ===")
    baseline_ok = r1["canary"]
    block_ok = not r3["canary"]
    noresp_ok = not r4["canary"]
    if baseline_ok and block_ok and noresp_ok and not failed:
        print("P0-GATE-04 = 通过（block-all 与断连默认拒绝均被验证）")
    elif block_ok and not noresp_ok:
        print("P0-GATE-04 = 可绕过（block-all 有效，但断连时行为未达默认拒绝）")
    elif not baseline_ok:
        print("P0-GATE-04 = 不通过（基线都未执行，执行路径本身有问题，结论无效）")
    else:
        print("P0-GATE-04 = 不通过（存在无法阻断的执行路径）")
    print(f"\n工作目录：{workdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
