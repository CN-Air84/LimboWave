"""加密密钥库的单元测试。

关键不变量（Phase 1A 验收 + ADR-0002）：
- 密钥**绝不以明文落盘**。
- 加解密密钥来自资料库主密钥（``VaultKey``），磁盘上没有可用的明文密钥。
- 篡改密文会被完整性校验发现。
- Phase 1A 旧格式（明文 ``master.key``）可被一次性迁移。
"""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from limbowave.infrastructure.crypto.secret_store import SecretStore, migrate_legacy_secrets
from limbowave.infrastructure.crypto.vault import VaultError, VaultKey

SECRET = "sk-live-THIS-MUST-NEVER-APPEAR-ON-DISK"


@pytest.fixture
def store(tmp_path: Path, vault_key: VaultKey) -> SecretStore:
    return SecretStore(vault_key, tmp_path / "vault" / "secrets.json")


def test_round_trip(store: SecretStore) -> None:
    store.set("key-a", SECRET)
    assert store.get("key-a") == SECRET


def test_plaintext_never_hits_disk(tmp_path: Path, store: SecretStore) -> None:
    """整个数据目录里不得出现密钥明文。"""
    store.set("key-a", SECRET)

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")


def test_no_plaintext_master_key_file(tmp_path: Path, store: SecretStore) -> None:
    """ADR-0002：不再存在明文 master.key。"""
    store.set("key-a", SECRET)
    assert not (tmp_path / "vault" / "master.key").exists()


def test_missing_ref_returns_none(store: SecretStore) -> None:
    assert store.get("nope") is None


def test_refs_sorted(store: SecretStore) -> None:
    store.set("zeta", "z")
    store.set("alpha", "a")
    assert store.refs() == ["alpha", "zeta"]


def test_delete(store: SecretStore) -> None:
    store.set("key-a", SECRET)
    store.delete("key-a")
    assert store.get("key-a") is None
    assert store.refs() == []


def test_delete_missing_is_noop(store: SecretStore) -> None:
    store.delete("never-existed")
    assert store.refs() == []


def test_persists_across_instances(tmp_path: Path, vault_key: VaultKey) -> None:
    data = tmp_path / "vault" / "secrets.json"
    SecretStore(vault_key, data).set("key-a", SECRET)
    assert SecretStore(vault_key, data).get("key-a") == SECRET


def test_wrong_key_cannot_decrypt(tmp_path: Path, vault_key: VaultKey, make_key) -> None:
    """换一个主密钥（等于换密码重建资料库）就读不出旧密文。"""
    data = tmp_path / "vault" / "secrets.json"
    SecretStore(vault_key, data).set("key-a", SECRET)
    with pytest.raises(VaultError):
        SecretStore(make_key(), data).get("key-a")


def test_tampered_ciphertext_detected(tmp_path: Path, store: SecretStore) -> None:
    """篡改密文必须被发现（AEAD 完整性），不能静默返回垃圾。"""
    store.set("key-a", SECRET)

    data_path = tmp_path / "vault" / "secrets.json"
    blob = json.loads(data_path.read_text(encoding="utf-8"))["key-a"]
    raw = bytearray(base64.b64decode(blob))
    raw[-1] ^= 0x01  # 翻转最后一字节
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    payload["key-a"] = base64.b64encode(bytes(raw)).decode("ascii")
    data_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VaultError):
        store.get("key-a")


def test_multiple_refs_isolated(store: SecretStore) -> None:
    store.set("key-a", "secret-a")
    store.set("key-b", "secret-b")
    assert store.get("key-a") == "secret-a"
    assert store.get("key-b") == "secret-b"


# ---------- 旧格式迁移 ----------


def _write_legacy(directory: Path, refs: dict[str, str]) -> None:
    """按 Phase 1A 旧格式落盘：明文 master.key + 它加密的 secrets.json。"""
    directory.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_bytes(32)
    (directory / "master.key").write_text(base64.b64encode(raw).decode("ascii"), encoding="utf-8")
    cipher = ChaCha20Poly1305(raw)
    data = {}
    for ref, secret in refs.items():
        nonce = secrets.token_bytes(12)
        sealed = cipher.encrypt(nonce, secret.encode("utf-8"), None)
        data[ref] = base64.b64encode(nonce + sealed).decode("ascii")
    (directory / "secrets.json").write_text(json.dumps(data), encoding="utf-8")


def test_migrate_legacy_secrets(tmp_path: Path, vault_key: VaultKey) -> None:
    vault_dir = tmp_path / "vault"
    _write_legacy(vault_dir, {"key-a": SECRET, "key-b": "second"})

    migrated = migrate_legacy_secrets(vault_dir, vault_key)
    assert migrated == 2
    assert not (vault_dir / "master.key").exists()

    store = SecretStore(vault_key, vault_dir / "secrets.json")
    assert store.get("key-a") == SECRET
    assert store.get("key-b") == "second"


def test_migrate_legacy_noop_without_legacy(tmp_path: Path, vault_key: VaultKey) -> None:
    assert migrate_legacy_secrets(tmp_path / "vault", vault_key) == 0


def test_legacy_plaintext_gone_after_migration(tmp_path: Path, vault_key: VaultKey) -> None:
    vault_dir = tmp_path / "vault"
    _write_legacy(vault_dir, {"key-a": SECRET})
    migrate_legacy_secrets(vault_dir, vault_key)
    for path in vault_dir.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")
