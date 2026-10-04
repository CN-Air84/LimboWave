"""全测试共用的资料库夹具。

集中定义**快速 KDF 参数**：默认 Argon2id 参数（64 MiB × 3 轮）面向真实交互登录，
在测试里会让每个用例多花几百毫秒。测试统一改用最小参数——这不削弱被测逻辑，
因为 KDF 参数本身也是 vault 文件的一部分、被独立测试覆盖（test_vault.py）。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path

import pytest

from limbowave.infrastructure.crypto.vault import KdfParams, Vault, VaultKey

TEST_PASSWORD = "test master password"
TEST_KDF = KdfParams(time_cost=1, memory_cost=8, parallelism=1)


def make_vault(path: Path, password: str = TEST_PASSWORD) -> VaultKey:
    """在指定路径创建测试资料库，返回已解锁的主密钥。"""
    return Vault(path, params=TEST_KDF).create(password)


@pytest.fixture
def vault_key(tmp_path: Path) -> VaultKey:
    """一个已解锁的测试资料库主密钥。"""
    return make_vault(tmp_path / "vault.json")


@pytest.fixture
def make_key() -> Callable[[], VaultKey]:
    """产出**另一个**随机主密钥（不建文件）——用于"换密钥读不出旧密文"类测试。"""
    return lambda: VaultKey(secrets.token_bytes(32))
