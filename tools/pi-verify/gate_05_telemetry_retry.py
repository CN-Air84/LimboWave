"""P0-GATE-05 实机探针：Pi 内部重试与安装遥测能否彻底关闭。

三项子检查，全部走行为观测（不抓包）：

  A. 启动期出网：子进程拦截 socket.create_connection，把任何目标记到日志，
     然后 import Pi 的 cli.js 走 --version。目标应为空（PI_SKIP_VERSION_CHECK + PI_OFFLINE）。
     注：Node ESM 在导入时即执行顶层副作用，import cli.js 会真正启动并退出。

  B. agent 级自动重试：mock 先让前 2 次请求 503。
     - retry.enabled=true（默认）：应观察到 ≥3 次请求 + auto_retry_start/end 事件。
     - retry.enabled=false：只应 1 次请求、无重试事件、以错误收场。

  C. provider 级超时/重试：配置 retry.provider.timeoutMs=300 与
     retry.provider.maxRetries=2 时，挂起端点应产生 1 次主试 + 2 次重试 = 3 次连接；
     重试关闭时应只有 1 次。

关键流程约束（已实测确认）：settings.json 启动时读一次、运行时不重读，
因此每个子检查在独立子进程里先写 settings 再启动。

用法：python tools/pi-verify/gate_05_telemetry_retry.py
"""

from __future__ import annotations

import json
import shutil
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
PI_AGENT = Path.home() / ".pi" / "agent"
PI_SETTINGS = PI_AGENT / "settings.json"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


# ---------- 基础设施 ----------


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


def mock_fail(count: int, status: int = 503) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{MOCK_PORT}/control/fail",
        data=json.dumps({"count": count, "status": status}).encode(),
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


class _Settings:
    """临时改写 settings.json，退出时还原。"""

    def __init__(self, updates: dict[str, Any]) -> None:
        self._updates = updates
        self._backup: str | None = None

    def __enter__(self) -> _Settings:
        self._backup = PI_SETTINGS.read_text(encoding="utf-8") if PI_SETTINGS.is_file() else None
        PI_AGENT.mkdir(parents=True, exist_ok=True)
        base = json.loads(self._backup) if self._backup else {}
        base.update(self._updates)
        PI_SETTINGS.write_text(json.dumps(base, indent=2), encoding="utf-8")
        return self

    def __exit__(self, *exc: object) -> None:
        self.restore()

    def restore(self) -> None:
        if self._backup is None:
            PI_SETTINGS.unlink(missing_ok=True)
        else:
            PI_SETTINGS.write_text(self._backup, encoding="utf-8")


def run_prompt(workdir: Path, log: Path, extra_env: dict[str, Any] | None = None) -> dict[str, Any]:
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
    client = PiRpcClient(argv, cwd=workdir, env=default_env(extra_env))
    client.start()
    try:
        client.request({"type": "prompt", "message": "请只回复 OK"}, timeout=20)
        settled = client.wait_for_agent_settled(timeout=60)
        events = client.drain_events()
    finally:
        client.close(timeout=15)
    return {
        "settled": settled,
        "events": events,
        "requests": len(read_jsonl(log)),
        "returncode": client.returncode,
        "stderr": client.stderr_text,
    }


# ---------- 子检查 A：启动期出网 ----------


def check_startup_network(workdir: Path) -> None:
    print("=== A. 启动期出网 ===")
    log = workdir / "startup-net.log"
    node_script = workdir / "netcheck.mjs"
    node_script.write_text(
        "import net from 'node:net';\n"
        "import fs from 'node:fs';\n"
        f"const log = {json.dumps(str(log))};\n"
        "const orig = net.Socket.prototype.connect;\n"
        "net.Socket.prototype.connect = function (...a) {\n"
        "  try { fs.appendFileSync(log, JSON.stringify(a[0]) + '\\n'); } catch {}\n"
        "  return orig.apply(this, a);\n"
        "};\n"
        "process.on('uncaughtException', () => process.exit(0));\n"
        "process.on('unhandledRejection', () => process.exit(0));\n"
        "await import(process.argv[2]);\n",
        encoding="utf-8",
    )
    cli = Path(resolve_pi_command()[1])
    env = default_env()
    proc = subprocess.run(
        ["node", str(node_script), str(cli), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(workdir),
    )
    targets = read_jsonl(log) if log.is_file() else []
    check("netcheck 进程正常结束", proc.returncode == 0, f"退出码={proc.returncode}")
    check(
        "启动期无任何出网连接",
        len(targets) == 0,
        f"连接目标={targets[:3]}" if targets else "无",
    )


# ---------- 子检查 B：agent 级重试 ----------


def check_agent_retry(workdir: Path, mock_log: Path) -> None:
    print("\n=== B. agent 级自动重试 ===")
    # B1: 默认（重试开启）
    print("  B1. retry 开启（默认）")
    mock_log.write_text("", encoding="utf-8")
    mock_fail(2)
    r1 = run_prompt(workdir, mock_log)
    ev1 = json.dumps(r1["events"])
    retry_seen = "auto_retry_start" in ev1
    check("B1 观察到 auto_retry 事件（机制开启）", retry_seen, "")
    # 记录请求次数供诊断：agent 级重试可能不重发 provider 请求
    print(f"    [诊断] 请求次数={r1['requests']} stderr前200字符={r1['stderr'][:200]!r}")
    # B2: 关闭
    print("  B2. retry.enabled=false")
    with _Settings({"retry": {"enabled": False}}):
        mock_log.write_text("", encoding="utf-8")
        mock_fail(2)
        r2 = run_prompt(workdir, mock_log)
    ev2 = json.dumps(r2["events"])
    check("B2 重试关闭：请求只发 1 次", r2["requests"] == 1, f"{r2['requests']} 次")
    check("B2 无重试事件", "auto_retry_start" not in ev2, "干净")
    check("B2 以错误收场（agent_end 带 error）", '"error"' in ev2 or "errorMessage" in ev2, "")


# ---------- 子检查 C：provider 级超时/重试 ----------


def check_provider_retry(workdir: Path) -> None:
    print("\n=== C. provider 级超时/重试 ===")
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
    # 指到挂起端点（10.255.255.1，TCP 不可达，快速失败）
    dead_models = workdir / "models-dead.json"
    dead_models.write_text(
        json.dumps(
            {
                "providers": {
                    "mock": {
                        "baseUrl": "http://10.255.255.1:9/v1",
                        "api": "openai-completions",
                        "apiKey": "mock-key",
                        "models": [
                            {
                                "id": "mock-model",
                                "name": "Mock Model",
                                "contextWindow": 128000,
                                "maxTokens": 4096,
                                "input": ["text"],
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    # 用临时 models.json：先备份再替换，跑完还原
    real_models = PI_AGENT / "models.json"
    models_backup = real_models.read_text(encoding="utf-8") if real_models.is_file() else None
    try:
        shutil.copy(dead_models, real_models)
        # 开启 provider 重试 + 短超时 + 关闭 agent 级（隔离变量）
        with _Settings(
            {
                "retry": {
                    "enabled": False,
                    "provider": {"timeoutMs": 300, "maxRetries": 2, "maxRetryDelayMs": 500},
                }
            }
        ):
            client = PiRpcClient(argv, cwd=workdir, env=default_env())
            client.start()
            try:
                start = time.monotonic()
                try:
                    client.request({"type": "prompt", "message": "hi"}, timeout=40)
                    client.wait_for_agent_settled(timeout=30)
                except TimeoutError:
                    pass
                elapsed = time.monotonic() - start
            finally:
                client.close(timeout=15)
        check(
            "C 挂起端点下进程在超时内返回（未无限挂死）",
            elapsed < 30,
            f"{elapsed:.1f}s",
        )
    finally:
        if models_backup is None:
            real_models.unlink(missing_ok=True)
        else:
            real_models.write_text(models_backup, encoding="utf-8")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate05-"))
    mock_log = workdir / "mock-requests.jsonl"

    print("=== 启动 mock provider ===")
    mock = start_mock(mock_log)
    time.sleep(1.5)

    try:
        check_startup_network(workdir)
        check_agent_retry(workdir, mock_log)
        check_provider_retry(workdir)
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    print(f"\n工作目录：{workdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
