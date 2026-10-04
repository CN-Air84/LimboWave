"""最小命令行入口：维护资料库与加密密钥库。

设计约束：
- 主密码与密钥**只经标准输入读取**，不作为命令行参数（避免进入 shell 历史与进程列表）。
- 回显关闭（``getpass``），且绝不把密钥打印出来。
- 非 TTY 的 stdin 协议：每行一个值。``secret set`` 依次为「主密码、密钥」；
  其余命令首行为主密码。
- 只操作资料库根目录下的 ``config.json``、``vault.json`` 与 ``vault/``，与应用运行时同一套路径。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable

from limbowave.bootstrap import create_context
from limbowave.infrastructure.crypto.secret_store import SecretStore, migrate_legacy_secrets
from limbowave.infrastructure.crypto.vault import InvalidPassword, Vault, VaultError, VaultKey

USAGE = """用法：
  python -m limbowave                          启动 GUI（自动记录诊断日志）
  python -m limbowave --log-level DEBUG        开启本次运行的 DEBUG 日志
  python -m limbowave --log-console            同时输出诊断日志到控制台
  python -m limbowave --log-dir <目录>         指定日志保存目录
  python -m limbowave vault init               创建资料库（设置主密码）
  python -m limbowave vault change-password    修改主密码
  python -m limbowave secret set <引用名>      从标准输入读取密钥并加密存入
  python -m limbowave secret list              列出已存的引用名
  python -m limbowave secret delete <引用名>   删除一个引用
"""


def _read_line(prompt: str) -> str:
    """交互读取（不回显）。

    ``getpass`` 在非 TTY 的 stdin 上会转去读控制台并**阻塞**——管道与脚本里会直接挂死。
    因此非交互场景改为从 stdin 直读一行。
    """
    if sys.stdin is None or not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n") if sys.stdin is not None else ""
    return getpass.getpass(prompt)


def _vault() -> Vault:
    return Vault(create_context().paths.data_root / "vault.json")


def _unlock(vault: Vault) -> VaultKey | None:
    """解锁资料库并顺带迁移旧格式密钥。失败返回 None。"""
    password = _read_line("主密码（输入不回显）：")
    if not password:
        print("主密码为空，已取消。", file=sys.stderr)
        return None
    try:
        key = vault.unlock(password)
    except InvalidPassword:
        print("主密码错误。", file=sys.stderr)
        return None
    except VaultError as exc:
        print(f"资料库不可用：{exc}", file=sys.stderr)
        return None
    migrated = migrate_legacy_secrets(create_context().paths.data_root / "vault", key)
    if migrated:
        print(f"已迁移 {migrated} 条旧格式密钥。")
    return key


def _require_vault() -> Vault | None:
    vault = _vault()
    if not vault.exists:
        print("资料库不存在：请先运行 python -m limbowave vault init。", file=sys.stderr)
        return None
    return vault


def _store(key: VaultKey) -> SecretStore:
    return SecretStore(key, create_context().paths.data_root / "vault" / "secrets.json")


# ---------- vault ----------


def _cmd_vault_init() -> int:
    vault = _vault()
    if vault.exists:
        print("资料库已存在，拒绝覆盖。", file=sys.stderr)
        return 1
    interactive = sys.stdin is not None and sys.stdin.isatty()
    password = _read_line("设置主密码（输入不回显）：")
    if not password:
        print("主密码为空，已取消。", file=sys.stderr)
        return 1
    if interactive and getpass.getpass("再输入一次确认：") != password:
        print("两次输入不一致，已取消。", file=sys.stderr)
        return 1
    vault.create(password)
    print("资料库已创建。应用启动时会要求输入该主密码。")
    return 0


def _cmd_vault_change_password() -> int:
    vault = _require_vault()
    if vault is None:
        return 1
    interactive = sys.stdin is not None and sys.stdin.isatty()
    old = _read_line("当前主密码（输入不回显）：")
    new = _read_line("新主密码（输入不回显）：")
    if not old or not new:
        print("密码为空，已取消。", file=sys.stderr)
        return 1
    if interactive and getpass.getpass("再输入一次新密码确认：") != new:
        print("两次输入不一致，已取消。", file=sys.stderr)
        return 1
    try:
        vault.change_password(old, new)
    except InvalidPassword:
        print("当前主密码错误。", file=sys.stderr)
        return 1
    except VaultError as exc:
        print(f"修改失败：{exc}", file=sys.stderr)
        return 1
    print("主密码已更新（仅重新封装主密钥，数据未重写）。")
    return 0


def vault_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m limbowave vault", add_help=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="创建资料库")
    sub.add_parser("change-password", help="修改主密码")
    args = parser.parse_args(argv)
    if args.action == "init":
        return _cmd_vault_init()
    return _cmd_vault_change_password()


# ---------- secret ----------


def _with_unlocked_store(action: Callable[[SecretStore], int]) -> int:
    vault = _require_vault()
    if vault is None:
        return 1
    key = _unlock(vault)
    if key is None:
        return 1
    return action(_store(key))


def _cmd_set(ref: str) -> int:
    def _do(store: SecretStore) -> int:
        interactive = sys.stdin is not None and sys.stdin.isatty()
        value = _read_line(f"请输入 {ref} 的密钥（输入不回显）：")
        if not value:
            print("密钥为空，已取消。", file=sys.stderr)
            return 1
        if interactive and getpass.getpass("再输入一次确认：") != value:
            print("两次输入不一致，已取消。", file=sys.stderr)
            return 1
        store.set(ref, value)
        print(f"已加密存入引用 {ref!r}。")
        return 0

    return _with_unlocked_store(_do)


def _cmd_list() -> int:
    def _do(store: SecretStore) -> int:
        refs = store.refs()
        if not refs:
            print("密钥库为空。")
            return 0
        for ref in refs:
            print(ref)
        return 0

    return _with_unlocked_store(_do)


def _cmd_delete(ref: str) -> int:
    def _do(store: SecretStore) -> int:
        if store.get(ref) is None:
            print(f"引用 {ref!r} 不存在。", file=sys.stderr)
            return 1
        store.delete(ref)
        print(f"已删除引用 {ref!r}。")
        return 0

    return _with_unlocked_store(_do)


def secret_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m limbowave secret", add_help=True)
    sub = parser.add_subparsers(dest="action", required=True)
    set_parser = sub.add_parser("set", help="存入一个密钥")
    set_parser.add_argument("ref")
    sub.add_parser("list", help="列出引用名")
    del_parser = sub.add_parser("delete", help="删除一个引用")
    del_parser.add_argument("ref")

    args = parser.parse_args(argv)
    if args.action == "set":
        return _cmd_set(args.ref)
    if args.action == "list":
        return _cmd_list()
    return _cmd_delete(args.ref)


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("secret", "vault"):
        try:
            if argv[0] == "secret":
                return secret_main(argv[1:])
            return vault_main(argv[1:])
        except SystemExit as exc:  # argparse 的 --help / 参数错误
            return int(exc.code or 0)
    if argv and argv[0] in ("-h", "--help") and len(argv) == 1:
        print(USAGE)
        return 0

    from limbowave.app import main as app_main

    return app_main()
