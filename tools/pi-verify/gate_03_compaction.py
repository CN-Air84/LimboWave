"""P0-GATE-03 实机探针：session_before_compact 能否可靠接管压缩。

四个阶段：
  1. observe：确认钩子被触发、preparation 内容齐全、reason 正确（manual）。
  2. cancel：返回 {cancel:true}，验证上下文未被压缩（contextUsage.tokens 不变）。
  3. custom：返回自定义摘要，验证下一轮请求的上下文里出现自定义摘要、且不含被压掉的原文。
  4. threshold：用小 contextWindow 模型 + 小 reserveTokens 触发自动压缩，确认钩子 reason=threshold。

用法：python tools/pi-verify/gate_03_compaction.py
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
PI_AGENT = Path.home() / ".pi" / "agent"
EXT = Path(__file__).resolve().parent / "extensions" / "gate-03-compaction-hook.ts"
MARKER = "CUSTOM-SUMMARY-MARKER"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


_BACKUP: dict[str, str | None] = {}


def _backup(path: Path) -> None:
    if str(path) not in _BACKUP:
        _BACKUP[str(path)] = path.read_text(encoding="utf-8") if path.is_file() else None


def _restore_all() -> None:
    for p, content in _BACKUP.items():
        path = Path(p)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content, encoding="utf-8")


def _write_settings(updates: dict[str, Any]) -> None:
    settings = PI_AGENT / "settings.json"
    _backup(settings)
    base = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    base.update(updates)
    settings.write_text(json.dumps(base, indent=2), encoding="utf-8")


def _write_manual_compaction_settings() -> None:
    """手动阶段：keepRecentTokens 调小，否则切割点不前移、prepareCompaction 返回 null。"""
    _write_settings(
        {"compaction": {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 200}}
    )


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


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def run_session(
    workdir: Path,
    mode: str,
    mock_log: Path,
    *,
    prompts: list[str],
    do_compact: bool = True,
    post_compact_prompt: str | None = None,
) -> dict[str, Any]:
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
    ext_log = workdir / f"gate03-{mode}.jsonl"
    env_extra = {"GATE03_MODE": mode, "GATE03_LOG": str(ext_log)}
    client = PiRpcClient(argv, cwd=workdir, env=default_env(env_extra))
    client.start()
    out: dict[str, Any] = {
        "hook": [],
        "compact_resp": None,
        "events": [],
        "before": None,
        "after": None,
    }
    try:
        for p in prompts:
            client.request({"type": "prompt", "message": p}, timeout=30)
            client.wait_for_agent_settled(timeout=60)
            client.drain_events()

        out["before"] = client.request({"type": "get_session_stats"}, timeout=30).get("data") or {}

        if do_compact:
            resp = client.request({"type": "compact"}, timeout=60)
            out["compact_resp"] = resp
            time.sleep(0.3)
            out["events"] = client.drain_events()
            out["after"] = (
                client.request({"type": "get_session_stats"}, timeout=30).get("data") or {}
            )

        if post_compact_prompt is not None:
            client.request({"type": "prompt", "message": post_compact_prompt}, timeout=30)
            client.wait_for_agent_settled(timeout=60)
            client.drain_events()
    finally:
        client.close(timeout=15)
    time.sleep(0.3)
    out["hook"] = read_jsonl(ext_log)
    return out


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pi-gate03-"))
    mock_log = workdir / "mock-requests.jsonl"

    print("=== 启动 mock provider ===")
    mock = start_mock(mock_log)
    time.sleep(1.5)

    # 手动 compact 需要 prepareCompaction 返回非空（切割点之上有可压消息），
    # 否则抛 "Nothing to compact (session too small)"，session_before_compact 钩子不会触发。
    # 用多条很长的 prompt 撑起可压缩的历史（每条约 320 中文字）。
    prompts = [
        f"第{i}轮：请记住数字 {1000 + i}。" + ("这是一段用于撑起压缩历史的占位内容。" * 40)
        for i in range(1, 6)
    ]

    # 手动阶段也需要 keepRecentTokens 足够小，否则 findCutPoint 永远不会把切割点前移，
    # prepareCompaction 返回 null，钩子不触发（"Nothing to compact"）。启动前统一写小。
    _write_manual_compaction_settings()

    try:
        # --- 阶段 1：observe ---
        print("=== 阶段 1：observe（确认钩子触发与 preparation）===")
        mock_log.write_text("", encoding="utf-8")
        r = run_session(workdir, "observe", mock_log, prompts=prompts)
        hook = r["hook"]
        check("observe：钩子被触发", len(hook) >= 1, f"{len(hook)} 次")
        if hook:
            h = hook[0]
            check("observe：reason=manual", h.get("reason") == "manual", str(h.get("reason")))
            check(
                "observe：preparation 含 messagesToSummarize",
                h.get("messagesToSummarize") is not None,
                f"messages={h.get('messagesToSummarize')} firstKept={h.get('firstKeptEntryId')}",
            )
        resp = r["compact_resp"] or {}
        check("observe：compact 命令成功", resp.get("success") is True, str(resp.get("error", "")))
        ev = json.dumps(r["events"])
        check("observe：compaction_start/end 事件", "compaction_start" in ev, "")

        # --- 阶段 2：cancel ---
        print("\n=== 阶段 2：cancel（上下文不得被压缩）===")
        mock_log.write_text("", encoding="utf-8")
        r = run_session(workdir, "cancel", mock_log, prompts=prompts)
        hook = r["hook"]
        check("cancel：钩子被触发", len(hook) >= 1, f"{len(hook)} 次")
        before = (r["before"] or {}).get("contextUsage") or {}
        after = (r["after"] or {}).get("contextUsage") or {}
        check(
            "cancel：压缩未发生（contextUsage.tokens 不变）",
            before.get("tokens") == after.get("tokens"),
            f"{before.get('tokens')} → {after.get('tokens')}",
        )
        ev = json.dumps(r["events"]) + json.dumps(r["compact_resp"])
        check("cancel：响应/事件表明已中止", "cancel" in ev.lower() or "aborted" in ev.lower(), "")

        # --- 阶段 3：custom ---
        print("\n=== 阶段 3：custom（自定义摘要注入）===")
        mock_log.write_text("", encoding="utf-8")
        r = run_session(
            workdir, "custom", mock_log, prompts=prompts, post_compact_prompt="总结我们讨论了什么"
        )
        hook = r["hook"]
        check("custom：钩子被触发", len(hook) >= 1, f"{len(hook)} 次")
        # 压缩后的那轮请求应含自定义摘要，且不含被压掉的原文
        entries = read_jsonl(mock_log)
        check("custom：压缩后有后续请求", len(entries) >= 1, f"{len(entries)} 次")
        if entries:
            last_blob = json.dumps(entries[-1]["body"], ensure_ascii=False)
            check("custom：上下文含自定义摘要标记", MARKER in last_blob)
            check("custom：被压掉的原文不再出现", "1001" not in last_blob, "（第一轮数字）")

        # --- 阶段 4：threshold（自动触发）---
        print("\n=== 阶段 4：threshold（自动压缩）===")
        # 用小 contextWindow 模型 + 小 reserveTokens 让上下文快速超限
        real_models = PI_AGENT / "models.json"
        _backup(real_models)
        try:
            real_models.write_text(
                json.dumps(
                    {
                        "providers": {
                            "mock": {
                                "baseUrl": f"http://127.0.0.1:{MOCK_PORT}/v1",
                                "api": "openai-completions",
                                "apiKey": "mock-key",
                                "models": [
                                    {
                                        "id": "mock-model",
                                        "name": "Mock",
                                        "contextWindow": 2000,
                                        "maxTokens": 200,
                                        "input": ["text"],
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            _write_settings(
                {"compaction": {"enabled": True, "reserveTokens": 1800, "keepRecentTokens": 50}}
            )

            mock_log.write_text("", encoding="utf-8")
            long_prompts = [f"第{i}轮：" + ("详细说明。" * 40) for i in range(1, 6)]
            r = run_session(workdir, "observe", mock_log, prompts=long_prompts, do_compact=False)
            hook = r["hook"]
            check("threshold：自动压缩触发了钩子", len(hook) >= 1, f"{len(hook)} 次")
            if hook:
                reasons = [h.get("reason") for h in hook]
                check(
                    "threshold：reason 为 threshold/overflow",
                    any(x in ("threshold", "overflow") for x in reasons),
                    str(reasons),
                )
        finally:
            pass  # 统一在外层 finally 还原
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
        _restore_all()

    failed = [n for n, ok, _ in checks if not ok]
    print("\n" + "=" * 60)
    print(f"共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        for n in failed:
            print(f"  - {n}")
    print(f"\n工作目录：{workdir}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
