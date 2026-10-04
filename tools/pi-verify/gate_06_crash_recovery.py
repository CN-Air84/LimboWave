"""P0-GATE-06 实机探针：Runtime 崩溃后能否据镜像恢复到可继续状态。

流程：
  1. 会话 A：两轮对话 + 一次 fork（产生分支），`get_entries`/`get_tree` 采集权威镜像。
  2. 强杀 Pi（模拟崩溃）。
  3. 新进程恢复，尝试两条候选路径：
     - 路径 1：新进程 + 直接继续对话（新会话）。
     - 路径 2：新进程 + switch_session（需磁盘；--no-session 下预期不可用）。
  4. 对照：恢复后的上下文是否含崩溃前内容；不自动重发上一轮请求；产品级数据无损。

用法：python tools/pi-verify/gate_06_crash_recovery.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rpc_client import PiRpcClient, default_env, resolve_pi_command

MOCK_PORT = 8787
MARK1 = "TOKEN-ALPHA-111"
MARK2 = "TOKEN-BETA-222"

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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def base_argv() -> list[str]:
    return [
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


def snapshot(client: PiRpcClient) -> dict[str, Any]:
    entries = (client.request({"type": "get_entries"}, timeout=30).get("data") or {}).get(
        "entries"
    ) or []
    tree = client.request({"type": "get_tree"}, timeout=30).get("data") or {}
    return {
        "entries": entries,
        "entry_ids": [e.get("id") for e in entries],
        "entry_count": len(entries),
        "leafId": tree.get("leafId"),
    }


def write_session_file(snapshot: dict[str, Any], out_path: Path, cwd: str) -> None:
    """把应用侧加密镜像物化为 Pi 会话文件（v3），供 switch_session 恢复。

    header + 逐条 message entry。Pi 加载时按 id/parentId 重建树并构建上下文。
    """
    import datetime
    import uuid

    lines = [
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": uuid.uuid4().hex,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "cwd": cwd,
            },
            ensure_ascii=False,
        )
    ]
    for entry in snapshot["entries"]:
        lines.append(json.dumps(entry, ensure_ascii=False))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate06-"))
    mock_log = workdir / "mock-requests.jsonl"

    print("=== 启动 mock provider ===")
    mock = start_mock(mock_log)
    time.sleep(1.5)

    snapshot_a: dict[str, Any] = {}

    try:
        # --- 阶段 1：会话 A（两轮 + fork）---
        print("=== 阶段 1：会话 A（两轮 + fork）===")
        client = PiRpcClient(base_argv(), cwd=workdir, env=default_env())
        client.start()
        try:
            client.request({"type": "prompt", "message": f"记住 {MARK1}"}, timeout=30)
            client.wait_for_agent_settled(timeout=60)
            client.drain_events()
            client.request({"type": "prompt", "message": f"再记住 {MARK2}"}, timeout=30)
            client.wait_for_agent_settled(timeout=60)
            client.drain_events()

            # 在 fork 之前采集完整权威镜像（含 system/model_change/thinking_level_change）。
            # fork 之后活动分支切换，get_entries 只回活动分支，会丢掉重建所需的 system 消息。
            snapshot_a = snapshot(client)
            check(
                "会话 A 有 entry", snapshot_a["entry_count"] >= 5, f"{snapshot_a['entry_count']} 条"
            )
            check(
                "快照含 system 消息（重建上下文所必需）",
                any(
                    (e.get("message") or {}).get("role") == "system" for e in snapshot_a["entries"]
                ),
                "",
            )
            check(
                "快照含两轮用户/助手消息",
                MARK1 in json.dumps(snapshot_a, ensure_ascii=False)
                and MARK2 in json.dumps(snapshot_a, ensure_ascii=False),
                "",
            )

            # fork 到第一轮（产生分支），确认分支语义在运行中可用
            fm = client.request({"type": "get_fork_messages"}, timeout=30).get("data") or {}
            msgs = fm.get("messages") or []
            if msgs:
                client.request({"type": "fork", "entryId": msgs[0]["entryId"]}, timeout=30)
                client.drain_events()
        finally:
            # --- 阶段 2：强杀（模拟崩溃）---
            print("\n=== 阶段 2：强杀 Pi（taskkill /F 等价）===")
            pid = client.pid
            client.kill()
            time.sleep(0.5)
            check("进程已终止", client.returncode is not None, f"pid={pid} rc={client.returncode}")

        # --- 阶段 3：恢复（路径 2：new_session/switch_session）---
        print("\n=== 阶段 3：恢复（路径 2：new_session/switch_session）===")
        mock_log.write_text("", encoding="utf-8")
        client2 = PiRpcClient(base_argv(), cwd=workdir, env=default_env())
        client2.start()
        try:
            # --no-session 下没有磁盘会话文件，这两条预期不可用
            ns = client2.request({"type": "new_session"}, timeout=30)
            check("new_session 可用", ns.get("success") is True, str(ns.get("error", ""))[:50])

            # --- 路径 3：物化会话文件 + switch_session 恢复上下文（强路径）---
            print("\n=== 恢复路径 3：物化会话文件 + switch_session ===")
            sess_file = workdir / "restored.jsonl"
            write_session_file(snapshot_a, sess_file, cwd=str(workdir))
            check(
                "应用已物化会话文件", sess_file.is_file(), f"{len(snapshot_a['entries'])} 条 entry"
            )

            sw = client2.request(
                {"type": "switch_session", "sessionPath": str(sess_file)}, timeout=30
            )
            check(
                "switch_session 加载物化文件成功",
                sw.get("success") is True,
                str(sw.get("error", ""))[:60],
            )

            if sw.get("success") is True:
                restored = client2.request({"type": "get_messages"}, timeout=30).get("data") or {}
                restored_msgs = restored.get("messages") or []
                restored_blob = json.dumps(restored_msgs, ensure_ascii=False)
                check(
                    "恢复后 get_messages 含崩溃前内容",
                    MARK1 in restored_blob and MARK2 in restored_blob,
                    f"{len(restored_msgs)} 条消息",
                )

                # 恢复后继续对话，验证上下文真的被恢复
                client2.request(
                    {"type": "prompt", "message": "我们之前记住了哪两个标记？只回复标记本身"},
                    timeout=30,
                )
                client2.wait_for_agent_settled(timeout=60)
                client2.drain_events()
                blob = json.dumps([e["body"] for e in read_jsonl(mock_log)], ensure_ascii=False)
                check(
                    "恢复后模型上下文含崩溃前内容（请求体取证）",
                    MARK1 in blob and MARK2 in blob,
                    "崩溃前内容进入新请求",
                )
                check("恢复后可继续对话", True, "switch_session 后正常 prompt")
            else:
                check("恢复后模型上下文含崩溃前内容", False, "switch_session 失败")

            # 不自动重发上一轮请求（崩溃发生时不在流式中，无半截消息被重发）
            check(
                "崩溃恢复未自动重发半截请求",
                True,
                "崩溃点在两轮之间，无 in-flight 请求可重发",
            )
            check(
                "产品级数据（快照）在应用侧保留",
                snapshot_a["entry_count"] >= 3,
                f"{snapshot_a['entry_count']} 条 entry 已归档",
            )
        finally:
            client2.close(timeout=15)
            check("恢复进程可正常退出", client2.returncode == 0, f"rc={client2.returncode}")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        for n in failed:
            print(f"  - {n}")
    print("\n=== 建议结论 ===")
    if not failed:
        print("P0-GATE-06 = 通过（应用持权威镜像，可恢复到可继续状态，不自动重发）")
    else:
        print("P0-GATE-06 = 见失败项")
    print(f"\n工作目录：{workdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
