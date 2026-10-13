#!/usr/bin/env python
"""Create or restore an encrypted LimboWave server backup.

The backup password is independent from the vault password. Neither password is
accepted as a command-line argument by default; both are read interactively.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from limbowave.application.services.backup_service import BackupService  # noqa: E402
from limbowave.infrastructure.crypto.vault import Vault  # noqa: E402
from limbowave.infrastructure.database.migrations import CURRENT_VERSION  # noqa: E402


def _password(prompt: str, *, confirm: bool = False) -> str:
    value = getpass.getpass(prompt)
    if confirm:
        again = getpass.getpass("再次输入：")
        if value != again:
            raise SystemExit("两次输入的密码不一致")
    if not value:
        raise SystemExit("密码不能为空")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="备份输出文件")
    parser.add_argument("--source", type=Path, help="要恢复的备份文件")
    parser.add_argument("--destination", type=Path, help="恢复目标临时目录")
    args = parser.parse_args()

    if bool(args.output) == bool(args.source):
        parser.error("必须且只能选择 --output 或 --source 之一")

    try:
        app_version = importlib.metadata.version("limbowave")
    except importlib.metadata.PackageNotFoundError:
        app_version = "dev"

    if args.output is not None:
        vault_password = _password("资料库主密码：")
        backup_password = _password("独立备份密码：", confirm=True)
        vault = Vault(args.data_dir / "vault.json")
        key = vault.unlock(vault_password)
        service = BackupService(
            args.data_dir,
            app_version=app_version,
            schema_version=CURRENT_VERSION,
            vault_key=key,
        )
        result = service.create(args.output, backup_password)
        print(f"备份完成：{result.path}")
        return 0

    if args.destination is None:
        parser.error("恢复时必须提供 --destination")
    backup_password = _password("独立备份密码：")
    service = BackupService(
        args.data_dir,
        app_version=app_version,
        schema_version=CURRENT_VERSION,
    )
    inspection = service.restore_to(args.source, backup_password, args.destination)
    print(f"恢复完成：{args.destination}")
    print(f"完整性：{'通过' if inspection.integrity_ok else '失败'}")
    return 0 if inspection.integrity_ok and inspection.compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
