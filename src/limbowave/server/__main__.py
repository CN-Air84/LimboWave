"""CLI entrypoint for the locked-first LimboWave server."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from limbowave.bootstrap import AppPaths
from limbowave.infrastructure.crypto.vault import Vault, VaultError

from .control import send_control
from .lifecycle import ServerLifecycle


def _password(prompt: str) -> str:
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass(prompt)


def _paths(root: str | None) -> AppPaths:
    data = (
        Path(root).expanduser().resolve()
        if root
        else Path.home() / ".local" / "share" / "limbowave"
    )
    return AppPaths(data, data / "logs")


def _init(root: str | None) -> int:
    paths = _paths(root)
    paths.data_root.mkdir(parents=True, exist_ok=True)
    vault = Vault(paths.data_root / "vault.json")
    if vault.exists:
        print("资料库已存在，拒绝覆盖。", file=sys.stderr)
        return 1
    password = _password("设置服务器主密码（输入不回显）：")
    if not password:
        print("主密码为空，已取消。", file=sys.stderr)
        return 1
    vault.create(password)
    print(f"服务器资料库已创建：{paths.data_root}")
    return 0


def _control(root: str | None, command: str, argument: str | None = None) -> int:
    path = _paths(root).data_root / "server.sock"
    payload = _password("服务器主密码（输入不回显）：") if command == "unlock" else argument
    try:
        result = asyncio.run(send_control(path, command, payload))
    except OSError:
        print("服务器控制面不可用，请确认 server run 正在运行。", file=sys.stderr)
        return 1
    if command in {"pair", "devices"} and result.startswith(("{", "[")):
        print(result)
        return 0
    if result != "ok" and not (command == "status" and result in {"locked", "unlocked"}):
        print("操作失败。", file=sys.stderr)
        return 1
    print(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m limbowave.server")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    children: dict[str, argparse.ArgumentParser] = {}
    for name in ("init", "run", "pair", "devices", "unlock", "lock", "status"):
        child = sub.add_parser(name)
        child.add_argument("--data-dir", dest="child_data_dir", default=None)
        children[name] = child
    revoke = sub.add_parser("revoke")
    revoke.add_argument("device_id")
    revoke.add_argument("--data-dir", dest="child_data_dir", default=None)
    args = parser.parse_args(argv)
    data_dir = args.child_data_dir or args.data_dir
    if args.command == "init":
        return _init(data_dir)
    if args.command == "run":
        try:
            asyncio.run(ServerLifecycle(_paths(data_dir)).run_forever())
        except (VaultError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    return _control(data_dir, args.command, getattr(args, "device_id", None))


if __name__ == "__main__":
    raise SystemExit(main())
