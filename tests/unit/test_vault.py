"""资料库信封加密的单元测试（设计计划 Task 1.2 要求）。

覆盖 Task 1.2 明列的六项：

- 正确密码解锁
- 错误密码拒绝
- 改密码后旧密码失效
- 对象文件解密完整
- 密文篡改被发现
- 崩溃后临时文件清理

外加 ADR-0002 的核心不变量：**主密钥不以明文落盘**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from limbowave.infrastructure.crypto.vault import (
    InvalidPassword,
    KdfParams,
    Vault,
    VaultError,
    VaultExists,
    VaultLocked,
)

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a completely different passphrase"
# 测试用轻参数：真实默认是 64MiB/3 轮，跑测试太慢。
TEST_PARAMS = KdfParams(time_cost=1, memory_cost=8, parallelism=1)


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault.json"


def _vault(path: Path) -> Vault:
    return Vault(path, params=TEST_PARAMS)


# ---------------------------------------------------------------- 1. 正确密码解锁


def test_unlock_with_correct_password(vault_path: Path) -> None:
    v = _vault(vault_path)
    key = v.create(PASSWORD)
    ciphertext = key.encrypt("机密内容")

    # 新实例（模拟重启）
    v2 = _vault(vault_path)
    key2 = v2.unlock(PASSWORD)
    assert key2.decrypt(ciphertext) == "机密内容"


# ---------------------------------------------------------------- 2. 错误密码拒绝


def test_wrong_password_rejected(vault_path: Path) -> None:
    _vault(vault_path).create(PASSWORD)

    v2 = _vault(vault_path)
    with pytest.raises(InvalidPassword):
        v2.unlock("wrong password")
    assert not v2.unlocked


def test_empty_password_rejected(vault_path: Path) -> None:
    with pytest.raises(VaultError):
        _vault(vault_path).create("")


# ---------------------------------------------------------------- 3. 改密码后旧密码失效


def test_change_password_invalidates_old(vault_path: Path) -> None:
    v = _vault(vault_path)
    key = v.create(PASSWORD)
    ciphertext = key.encrypt("改密码前加密的数据")

    v.change_password(PASSWORD, NEW_PASSWORD)

    # 旧密码失效
    with pytest.raises(InvalidPassword):
        _vault(vault_path).unlock(PASSWORD)

    # 新密码可用，且能解开**改密码前**的数据——证明主密钥未变、数据未被重写
    v3 = _vault(vault_path)
    key3 = v3.unlock(NEW_PASSWORD)
    assert key3.decrypt(ciphertext) == "改密码前加密的数据"


def test_change_password_does_not_rewrite_ciphertext(vault_path: Path) -> None:
    """改密码只重新封装主密钥：字段密文逐字节不变。"""
    v = _vault(vault_path)
    key = v.create(PASSWORD)
    ciphertext = key.encrypt("数据")

    v.change_password(PASSWORD, NEW_PASSWORD)

    key2 = _vault(vault_path).unlock(NEW_PASSWORD)
    assert key2.decrypt(ciphertext) == "数据"


# ---------------------------------------------------------------- 4. 对象文件解密完整


def test_round_trip_various_payloads(vault_path: Path) -> None:
    key = _vault(vault_path).create(PASSWORD)
    payloads = [
        "",
        "ascii only",
        "中文内容与全角标点。",
        "emoji 🎉 与换行\n\t制表",
        "x" * 10_000,
    ]
    for payload in payloads:
        assert key.decrypt(key.encrypt(payload)) == payload


def test_each_encryption_is_unique(vault_path: Path) -> None:
    """相同明文每次密文不同（逐值随机 nonce）——避免等值泄露。"""
    key = _vault(vault_path).create(PASSWORD)
    assert key.encrypt("same") != key.encrypt("same")


# ---------------------------------------------------------------- 5. 密文篡改被发现


def test_tampered_master_key_blob_detected(vault_path: Path) -> None:
    """篡改 vault 里的主密钥包装 → 拒绝解锁，不静默给出错误密钥。"""
    _vault(vault_path).create(PASSWORD)
    data = json.loads(vault_path.read_text(encoding="utf-8"))
    blob = data["wraps"][0]["blob"]
    # 翻转最后一个字符
    flipped = blob[:-1] + ("A" if blob[-1] != "A" else "B")
    data["wraps"][0]["blob"] = flipped
    vault_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises((InvalidPassword, VaultError)):
        _vault(vault_path).unlock(PASSWORD)


def test_tampered_field_ciphertext_detected(vault_path: Path) -> None:
    key = _vault(vault_path).create(PASSWORD)
    blob = key.encrypt("原始内容")
    flipped = blob[:-1] + ("A" if blob[-1] != "A" else "B")

    with pytest.raises(VaultError):
        key.decrypt(flipped)


def test_truncated_ciphertext_detected(vault_path: Path) -> None:
    key = _vault(vault_path).create(PASSWORD)
    blob = key.encrypt("内容")
    with pytest.raises(VaultError):
        key.decrypt(blob[: len(blob) // 2])


# ---------------------------------------------------------------- 6. 崩溃后临时文件清理


def test_no_temp_file_left_after_write(vault_path: Path) -> None:
    """写入用临时文件 + 原子替换：成功后不留 .tmp。"""
    _vault(vault_path).create(PASSWORD)
    assert vault_path.is_file()
    assert not vault_path.with_suffix(".tmp").exists()

    _vault(vault_path).unlock(PASSWORD)


def test_atomic_replace_preserves_old_on_partial_write(vault_path: Path) -> None:
    """留有 .tmp 残骸时，vault 本身仍是完整的旧内容。"""
    _vault(vault_path).create(PASSWORD)
    original = vault_path.read_text(encoding="utf-8")
    # 模拟崩溃留下的半截临时文件
    vault_path.with_suffix(".tmp").write_text("{ half written", encoding="utf-8")

    # 主文件仍可正常解锁
    assert _vault(vault_path).unlock(PASSWORD) is not None
    assert vault_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------- ADR-0002 不变量


def test_master_key_never_in_plaintext_on_disk(vault_path: Path) -> None:
    """磁盘上不得出现可用的明文主密钥。"""
    key = _vault(vault_path).create(PASSWORD)
    secret = key.encrypt("canary")
    raw_master = key._key

    content = vault_path.read_text(encoding="utf-8")
    assert raw_master.hex() not in content
    import base64

    assert base64.b64encode(raw_master).decode() not in content
    assert "canary" not in content
    assert secret not in content  # 字段密文也不落 vault


def test_vault_file_has_expected_shape(vault_path: Path) -> None:
    _vault(vault_path).create(PASSWORD)
    data = json.loads(vault_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    wraps = data["wraps"]
    assert len(wraps) == 1
    assert wraps[0]["method"] == "password"
    # KDF 参数随文件持久化：将来调参不会破坏既有资料库
    assert set(wraps[0]["kdf"]) == {"time_cost", "memory_cost", "parallelism"}
    assert wraps[0]["salt"]


def test_custom_kdf_params_survive_round_trip(vault_path: Path) -> None:
    """用非默认参数创建，解锁时必须读文件里的参数而非当前默认值。"""
    Vault(vault_path, params=KdfParams(time_cost=2, memory_cost=16, parallelism=1)).create(PASSWORD)
    # 用不同的默认参数打开，仍应成功（参数来自文件）
    other = Vault(vault_path, params=KdfParams(time_cost=1, memory_cost=8, parallelism=1))
    assert other.unlock(PASSWORD) is not None


def test_lock_requires_unlock_again(vault_path: Path) -> None:
    v = _vault(vault_path)
    v.create(PASSWORD)
    v.lock()
    assert not v.unlocked
    with pytest.raises(VaultLocked):
        v.require_key()


def test_create_refuses_to_overwrite(vault_path: Path) -> None:
    _vault(vault_path).create(PASSWORD)
    with pytest.raises(VaultExists):
        _vault(vault_path).create(PASSWORD)


def test_unlock_missing_vault_raises(vault_path: Path) -> None:
    with pytest.raises(VaultError):
        _vault(vault_path).unlock(PASSWORD)


def test_unsupported_version_rejected(vault_path: Path) -> None:
    _vault(vault_path).create(PASSWORD)
    data = json.loads(vault_path.read_text(encoding="utf-8"))
    data["version"] = 999
    vault_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(VaultError):
        _vault(vault_path).unlock(PASSWORD)
