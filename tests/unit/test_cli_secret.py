"""资料库 / 密钥维护 CLI 的单元测试。

重点锁住三个回归：
- **非 TTY 的 stdin 不得挂死**（``getpass`` 在管道下会阻塞，曾实测 exit=124）。
- 密钥只进加密库，不落普通配置。
- 主密码与密钥**只经 stdin**，绝不出现在命令行参数里（进程列表可见）。

非 TTY 的 stdin 协议：每行一个值——``secret set`` 依次是「主密码、密钥」。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from limbowave import cli
from limbowave.bootstrap import AppContext, AppPaths
from limbowave.infrastructure.crypto.secret_store import SecretStore
from limbowave.infrastructure.crypto.vault import InvalidPassword, Vault

PASSWORD = "cli test password"
SECRET = "sk-cli-TEST-VALUE"


@pytest.fixture
def cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 CLI 的资料库根指向临时目录，并初始化一个资料库。"""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    def _context() -> AppContext:
        return AppContext(
            paths=AppPaths(data_root=data_root, log_root=tmp_path / "logs"),
            python_version="3.12.0",
            frozen=False,
        )

    monkeypatch.setattr(cli, "create_context", _context)

    # 测试用最小 KDF 参数：默认参数面向真实交互登录，会让每个用例多花约一秒
    from limbowave.infrastructure.crypto.vault import KdfParams

    fast = KdfParams(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr(cli, "Vault", lambda p: Vault(p, params=fast))

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.vault_main(["init"]) == 0
    return data_root


def _feed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """模拟非 TTY 的 stdin。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _store(cli_root: Path) -> SecretStore:
    key = Vault(cli_root / "vault.json").unlock(PASSWORD)
    return SecretStore(key, cli_root / "vault" / "secrets.json")


def test_set_then_list_then_delete(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, f"{PASSWORD}\n{SECRET}\n")

    assert cli.secret_main(["set", "key-a"]) == 0
    assert _store(cli_root).get("key-a") == SECRET

    _feed(monkeypatch, PASSWORD + "\n")
    assert cli.secret_main(["list"]) == 0
    assert "key-a" in capsys.readouterr().out

    _feed(monkeypatch, PASSWORD + "\n")
    assert cli.secret_main(["delete", "key-a"]) == 0
    assert _store(cli_root).get("key-a") is None


def test_non_tty_stdin_does_not_hang(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：管道输入下必须正常返回，不能阻塞在 getpass 上。"""
    _feed(monkeypatch, f"{PASSWORD}\n{SECRET}\n")
    assert cli.secret_main(["set", "key-a"]) == 0


def test_wrong_password_rejected(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, f"wrong password\n{SECRET}\n")
    assert cli.secret_main(["set", "key-a"]) == 1
    assert "错误" in capsys.readouterr().err
    assert _store(cli_root).refs() == []


def test_secret_requires_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未 init 资料库时，secret 命令明确报错而不是隐式创建。"""
    data_root = tmp_path / "bare"
    data_root.mkdir()
    monkeypatch.setattr(
        cli,
        "create_context",
        lambda: AppContext(
            paths=AppPaths(data_root=data_root, log_root=tmp_path / "logs"),
            python_version="3.12.0",
            frozen=False,
        ),
    )
    _feed(monkeypatch, "anything\n")
    assert cli.secret_main(["list"]) == 1


def test_empty_secret_rejected(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, f"{PASSWORD}\n\n")
    assert cli.secret_main(["set", "key-a"]) == 1
    assert _store(cli_root).refs() == []


def test_delete_missing_returns_error(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, PASSWORD + "\n")
    assert cli.secret_main(["delete", "never"]) == 1


def test_list_empty(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _feed(monkeypatch, PASSWORD + "\n")
    assert cli.secret_main(["list"]) == 0
    assert "空" in capsys.readouterr().out


def test_secret_never_in_plain_config(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥不得出现在资料库根下的任何非 vault 文件里。"""
    _feed(monkeypatch, f"{PASSWORD}\n{SECRET}\n")
    cli.secret_main(["set", "key-a"])

    for path in cli_root.rglob("*"):
        if path.is_file() and "vault" not in path.parts:
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")


def test_vault_init_idempotent_refusal(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已存在的资料库拒绝被 init 覆盖。"""
    _feed(monkeypatch, "another password\n")
    assert cli.vault_main(["init"]) == 1


def test_vault_change_password(cli_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, f"{PASSWORD}\nnew password\n")
    assert cli.vault_main(["change-password"]) == 0

    # 旧密码失效，新密码可用
    vault = Vault(cli_root / "vault.json")
    with pytest.raises(InvalidPassword):
        vault.unlock(PASSWORD)
    vault.unlock("new password")
