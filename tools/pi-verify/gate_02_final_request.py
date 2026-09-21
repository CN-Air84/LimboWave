"""P0-GATE-02 实机探针：before_provider_request 能否稳定捕获并改写最终请求体。

判定：GATE02_LOG 收到完整 payload 即"捕获"；改写后 mock 收到 2048 而非默认 4096 即"改写生效"。
对照两个来源：扩展捕获的 payload 与 mock 侧实际收到的 body 必须一致（字段与值逐条对）。

用法：python tools/pi-verify/gate_02_final_request.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import PiRpcClient, default_env, resolve_pi_command

MOCK_PORT = 8787
EXT = Path(__file__).resolve().parent / "extensions" / "gate-02-request-hook.ts"

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


def run_round(cwd: Path, rewrite: bool, log_path: Path) -> dict[str, Any]:
    env_extra = {"GATE02_LOG": str(log_path)}
    if rewrite:
        env_extra["GATE02_REWRITE"] = "1"
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
    client = PiRpcClient(argv, cwd=cwd, env=default_env(env_extra))
    client.start()
    try:
        client.request({"type": "prompt", "message": "请只回复 OK"}, timeout=30)
        client.wait_for_agent_settled(timeout=90)
    finally:
        client.close(timeout=15)
    time.sleep(0.5)  # 让扩展写文件落盘
    return {"returncode": client.returncode, "stderr": client.stderr_text}


def read_payloads(path: Path) -> list[dict]:
    """读扩展日志里的 payload（以 `{` 开头的 JSONL），跳过 after_provider_response 标记行。"""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("{"):
            out.append(json.loads(line))
    return out


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate02-"))
    mock_log = workdir / "mock-requests.jsonl"
    ext_log = workdir / "ext-payloads.jsonl"

    print("=== 启动 mock provider ===")
    mock = start_mock(mock_log)
    time.sleep(1.5)

    try:
        # --- 阶段 1：只观察，不改写；捕获值应与 mock 实际收到完全一致 ---
        print("=== 阶段 1：观察模式（不改写）===")
        run_round(workdir, rewrite=False, log_path=ext_log)
        payloads = read_payloads(ext_log)
        mock_entries = read_jsonl(mock_log)
        check("钩子被触发并记录 payload", len(payloads) >= 1, f"{len(payloads)} 条")
        if payloads:
            p = payloads[0]
            for field in ("messages", "model", "tools", "max_completion_tokens", "stream"):
                check(f"payload 含字段 {field}", field in p, str(p.get(field))[:40])
            check("payload.model 正确", p.get("model") == "mock-model", str(p.get("model")))
            check("payload.messages 非空", isinstance(p.get("messages"), list) and p["messages"])
        if payloads and mock_entries:
            cap, got = payloads[0], mock_entries[0]["body"]
            mismatch = [
                k
                for k in ("model", "messages", "tools", "max_completion_tokens", "stream")
                if json.dumps(cap.get(k), sort_keys=True) != json.dumps(got.get(k), sort_keys=True)
            ]
            check(
                "观察模式下：扩展捕获值 == mock 实际收到值",
                not mismatch,
                f"不一致字段={mismatch}" if mismatch else "逐条相同",
            )

        # --- 阶段 2：改写模式 ---
        print("\n=== 阶段 2：改写模式（max_completion_tokens → 2048）===")
        ext_log.write_text("", encoding="utf-8")
        mock_log.write_text("", encoding="utf-8")
        run_round(workdir, rewrite=True, log_path=ext_log)
        mock_entries = read_jsonl(mock_log)
        check("mock 收到请求", len(mock_entries) >= 1, f"{len(mock_entries)} 次")
        if mock_entries:
            body = mock_entries[0]["body"]
            check(
                "改写生效：mock 收到 2048",
                body.get("max_completion_tokens") == 2048,
                f"实际={body.get('max_completion_tokens')}",
            )

        # --- 阶段 3：改写边界——扩展捕获的是改写前，mock 收到的是改写后 ---
        print("\n=== 阶段 3：改写边界（捕获=改写前，mock=改写后）===")
        payloads = read_payloads(ext_log)
        mock_entries = read_jsonl(mock_log)
        if payloads and mock_entries:
            cap, got = payloads[-1], mock_entries[0]["body"]
            check(
                "扩展捕获的是改写前 payload（4096）",
                cap.get("max_completion_tokens") == 4096,
                str(cap.get("max_completion_tokens")),
            )
            rest = [
                k
                for k in ("model", "messages", "tools", "stream")
                if json.dumps(cap.get(k), sort_keys=True) != json.dumps(got.get(k), sort_keys=True)
            ]
            check(
                "除被改写字段外，其余字段捕获值与实际上线值一致",
                not rest,
                f"不一致={rest}" if rest else "仅 max_completion_tokens 被改写",
            )

        # --- 4. after_provider_response ---
        print("\n=== 4. after_provider_response ===")
        after_lines = [
            e
            for e in ext_log.read_text(encoding="utf-8").splitlines()
            if "after_provider_response" in e
        ]
        check("收到 after_provider_response", len(after_lines) >= 1, f"{len(after_lines)} 条")
        if after_lines:
            check("含 HTTP status", "status=200" in after_lines[-1], after_lines[-1][:60])
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
    capture_ok = all(
        ok for n, ok, _ in checks if any(k in n for k in ("钩子", "payload 含", "观察模式下"))
    )
    rewrite_ok = all(
        ok for n, ok, _ in checks if any(k in n for k in ("改写生效", "改写前", "除被改写字段外"))
    )
    if capture_ok and rewrite_ok and not failed:
        print("P0-GATE-02 = 通过（可稳定捕获并可改写最终请求体）")
    elif capture_ok and not rewrite_ok:
        print("P0-GATE-02 = 可绕过（能观测但改写不可靠，§13.1 观测成立、改写另寻路径）")
    else:
        print("P0-GATE-02 = 不通过")
    print(f"\n工作目录：{workdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
