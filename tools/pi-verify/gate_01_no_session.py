"""P0-GATE-01 实机探针：--no-session 下的会话语义。

问题：`--no-session` 究竟是
  ① 仅禁止持久化磁盘，会话树仍在进程内完整存在；
  ② 完全禁用会话树与上下文积累；
  ③ 保留上下文，但部分 RPC 命令不可用。

判定方法（不依赖模型自述，靠 mock provider 记录的请求体作为铁证）：
  1. 启动本地 mock provider，其请求日志记录每次收到的 messages 条数。
  2. 以 `--no-session` 启动 Pi，连续两轮 prompt。
  3. 若第二轮请求里的消息条数多于第一轮，且含第一轮的内容 → 多轮上下文成立（排除 ②）。
  4. 依次探测分支类命令是否可用 → 区分 ① 与 ③。
  5. 检查磁盘是否产生任何会话文件。

用法：
    python tools/pi-verify/gate_01_no_session.py
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

PI_AGENT_DIR = Path.home() / ".pi" / "agent"
MOCK_PORT = 8787

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


def read_mock_log(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate01-"))
    mock_log = workdir / "mock-requests.jsonl"
    session_dir = workdir / "sessions"  # 受控 sessionDir，用于验证是否落盘
    session_dir.mkdir()

    sessions_before = (
        sorted(p.name for p in (PI_AGENT_DIR / "sessions").glob("*"))
        if (PI_AGENT_DIR / "sessions").is_dir()
        else []
    )

    print("=== 启动 mock provider ===")
    mock = start_mock(mock_log)
    time.sleep(1.5)

    argv = [
        *resolve_pi_command(),
        "--mode",
        "rpc",
        "--no-session",
        "--session-dir",
        str(session_dir),
        "--provider",
        "mock",
        "--model",
        "mock/mock-model",
        "--no-approve",
        "--no-context-files",
    ]
    print(f"=== 启动 Pi ===\n{' '.join(argv)}\n")

    client = PiRpcClient(argv, cwd=workdir, env=default_env())
    client.start()

    try:
        # --- 1. get_state：sessionFile 是否存在 ---
        print("=== 1. get_state ===")
        state = client.request({"type": "get_state"}, timeout=30)
        data = state.get("data", {})
        check(
            "get_state 成功",
            state.get("success") is True,
            f"model={data.get('model', {}).get('id') if data.get('model') else None}",
        )
        check(
            "sessionFile 字段不存在（内存模式特征）",
            "sessionFile" not in data,
            f"现存字段含 sessionId={data.get('sessionId', '')[:8]}…",
        )
        check("sessionId 仍分配", bool(data.get("sessionId")))

        # --- 2. 第一轮 ---
        print("\n=== 2. 第一轮 prompt ===")
        first = client.request({"type": "prompt", "message": "第一轮：请只回复 OK-ONE"}, timeout=30)
        check("prompt 被接受", first.get("success") is True, str(first.get("error", "")))
        settled = client.wait_for_agent_settled(timeout=90)
        check("收到 agent_settled", settled)
        client.drain_events()

        state1 = client.request({"type": "get_state"}, timeout=30).get("data", {})
        count1 = state1.get("messageCount", -1)

        # --- 3. 第二轮 ---
        print("\n=== 3. 第二轮 prompt ===")
        second = client.request(
            {"type": "prompt", "message": "第二轮：请只回复 OK-TWO"}, timeout=30
        )
        check("第二轮 prompt 被接受", second.get("success") is True, str(second.get("error", "")))
        settled = client.wait_for_agent_settled(timeout=90)
        check("第二轮 agent_settled", settled)
        client.drain_events()

        state2 = client.request({"type": "get_state"}, timeout=30).get("data", {})
        count2 = state2.get("messageCount", -1)
        check(
            "messageCount 在累积",
            count2 > count1,
            f"{count1} → {count2}",
        )

        # --- 4. mock 侧铁证：第二轮请求是否携带历史 ---
        print("\n=== 4. mock 侧请求体取证 ===")
        entries = read_mock_log(mock_log)
        check("mock 收到请求", len(entries) >= 2, f"共 {len(entries)} 次")
        if len(entries) >= 2:
            n1 = entries[0]["message_count"]
            n2 = entries[1]["message_count"]
            check(
                "第二轮请求携带更多消息（多轮上下文成立）",
                n2 > n1,
                f"第一轮 {n1} 条 → 第二轮 {n2} 条",
            )
            blob = json.dumps(entries[1]["body"], ensure_ascii=False)
            check("第二轮请求含第一轮内容", "OK-ONE" in blob or "第一轮" in blob)
            check(
                "请求体含 tools 定义",
                entries[1]["tool_count"] > 0,
                f"tools={entries[1]['tool_count']}",
            )

        # --- 5. 分支与查询类命令 ---
        print("\n=== 5. 分支与查询命令可用性（区分 ① 与 ③）===")
        for cmd in (
            {"type": "get_entries"},
            {"type": "get_tree"},
            {"type": "get_session_stats"},
            {"type": "get_fork_messages"},
            {"type": "get_messages"},
            {"type": "get_commands"},
        ):
            resp = client.request(cmd, timeout=30)
            ok = resp.get("success") is True
            detail = "" if ok else str(resp.get("error", ""))
            if ok and cmd["type"] == "get_entries":
                detail = f"{len(resp.get('data', {}).get('entries', []))} 条 entry"
            if ok and cmd["type"] == "get_tree":
                detail = f"leafId={str(resp.get('data', {}).get('leafId'))[:8]}…"
            check(f"{cmd['type']} 可用", ok, detail)

        # fork 需要的是「用户消息」的 entryId，不是 get_tree 的 leafId
        # （leaf 多为助手/工具结果消息，传进去会得到 Invalid entry ID for forking）
        forkable = client.request({"type": "get_fork_messages"}, timeout=30)
        fork_msgs = (forkable.get("data") or {}).get("messages") or []
        check("get_fork_messages 返回可 fork 条目", bool(fork_msgs), f"{len(fork_msgs)} 条")

        if fork_msgs:
            target = fork_msgs[0]["entryId"]
            tree_before = client.request({"type": "get_tree"}, timeout=30).get("data") or {}
            leaf_before = tree_before.get("leafId")

            fork = client.request({"type": "fork", "entryId": target}, timeout=30)
            ok = fork.get("success") is True
            detail = (
                f"forked-from={str((fork.get('data') or {}).get('text'))[:24]!r}"
                if ok
                else str(fork.get("error", ""))
            )
            check("fork 在内存模式下可用", ok, detail)

            if ok:
                tree_after = client.request({"type": "get_tree"}, timeout=30).get("data") or {}
                leaf_after = tree_after.get("leafId")
                check(
                    "fork 后活动分支发生切换",
                    leaf_after != leaf_before,
                    f"{str(leaf_before)[:8]}… → {str(leaf_after)[:8]}…",
                )
                # 分支切换后会话仍可继续使用
                client.drain_events()
                resume = client.request(
                    {"type": "prompt", "message": "分支后：请只回复 OK-FORK"}, timeout=30
                )
                check(
                    "fork 后仍可继续对话",
                    resume.get("success") is True,
                    str(resume.get("error", "")),
                )
                check("fork 后 agent_settled", client.wait_for_agent_settled(timeout=90))
                client.drain_events()
        else:
            check("fork 在内存模式下可用", False, "无可 fork 的用户消息条目")

        clone = client.request({"type": "clone"}, timeout=30)
        check(
            "clone 在内存模式下可用",
            clone.get("success") is True,
            str(clone.get("error", "")),
        )

        stats = client.request({"type": "get_session_stats"}, timeout=30).get("data") or {}
        check(
            "get_session_stats 报告 sessionFile",
            "sessionFile" not in stats or stats.get("sessionFile") in (None, ""),
            f"sessionFile={stats.get('sessionFile')!r} contextUsage={stats.get('contextUsage')}",
        )

        # --- 6. 磁盘取证 ---
        print("\n=== 6. 磁盘落盘检查 ===")
        session_dir_files = [p.name for p in session_dir.iterdir()]
        check("受控 sessionDir 内无文件", not session_dir_files, str(session_dir_files))

        sessions_after = (
            sorted(p.name for p in (PI_AGENT_DIR / "sessions").glob("*"))
            if (PI_AGENT_DIR / "sessions").is_dir()
            else []
        )
        check(
            "默认 sessions 目录未新增文件",
            sessions_after == sessions_before,
            f"{len(sessions_before)} → {len(sessions_after)}",
        )

        # --- 7. 退出语义 ---
        print("\n=== 7. 关闭语义 ===")
        client.close(timeout=15)
        check(
            "stdin EOF 后优雅退出，退出码 0", client.returncode == 0, f"退出码={client.returncode}"
        )

    finally:
        client.kill()
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    # --- 汇总 ---
    failed = [name for name, ok, _ in checks if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")

    # 按闸门规则给出建议结论
    print("\n=== 建议结论 ===")
    context_ok = all(ok for name, ok, _ in checks if "多轮上下文" in name or "累积" in name)
    branch_ok = all(
        ok
        for name, ok, _ in checks
        if any(k in name for k in ("get_entries", "get_tree", "fork", "clone"))
    )
    if context_ok and branch_ok and not failed:
        print("P0-GATE-01 = 通过（选项 ①：会话树与上下文完整存在于内存，仅不落盘）")
    elif context_ok and not branch_ok:
        print("P0-GATE-01 = 可绕过（选项 ③：上下文在，但部分分支命令不可用）")
    else:
        print("P0-GATE-01 = 不通过（多轮上下文未成立）")
    print(f"\n工作目录：{workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
