"""系统保护恢复验收（Task 1.3 / §12.3）。

锁住的不变量：
- 主密钥**不以明文**出现在 vault.json 或 DPAPI 密文里；
- 启用恢复后，主密码路径**不被削弱**（错误密码照样拒）；
- **忘掉主密码也能重置**，且重置后的密钥仍是同一把（旧数据能解开）；
- 支持**主动关闭**恢复，关闭后无法再用恢复重置；
- 身份验证不通过时**什么都不改**；
- 本模块不接触 PIN/认证内容（结构性成立：只拿布尔结果）。

DPAPI 只在 Windows 可用；非 Windows 环境跳过依赖它的用例，
但**领域规则（顺序、拒绝语义）用假闸门覆盖**，不依赖平台。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from limbowave.infrastructure.crypto import recovery
from limbowave.infrastructure.crypto.recovery import (
    METHOD_DPAPI,
    NoHelloGate,
    RecoveryError,
    describe_protection,
    dpapi_available,
)
from limbowave.infrastructure.crypto.vault import (
    InvalidPassword,
    KdfParams,
    Vault,
    VaultError,
)

FAST_KDF = KdfParams(time_cost=1, memory_cost=8, parallelism=1)
PASSWORD = "correct horse battery staple"
requires_dpapi = pytest.mark.skipif(not dpapi_available(), reason="需要 Windows DPAPI")


@dataclass(frozen=True, slots=True)
class _DenyGate:
    """永远拒绝的闸门（验证失败路径）。"""

    @property
    def name(self) -> str:
        return "test-deny"

    def available(self) -> bool:
        return True

    def verify(self, reason: str) -> bool:
        return False


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "vault.json", params=FAST_KDF)
    v.create(PASSWORD)
    return v


def _wraps(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["wraps"]


# ---------- 闸门与说明文本（不依赖平台） ----------


def test_gate_name_reflects_capability() -> None:
    """闸门名字必须反映**实际**能力，不能乐观也不能悲观。

    判定依据是闸门自己（依次尝试 WinRT 绑定与系统 PowerShell），
    不再是「有没有 pip 的 WinRT 绑定」——后者在 PowerShell 路径可用时会误判。
    """
    gate = recovery.hello_gate()
    from limbowave.domain.platform_capabilities import supports_system_identity
    if not supports_system_identity():
        assert gate.name == "unavailable"
        assert not gate.available() and not gate.verify("test")
        return
    assert gate.name in ("windows-account", "windows-hello")
    if gate.name == "windows-hello":
        assert gate.available(), "报 Hello 就必须真的可用"
    else:
        assert not recovery.hello_available(), "报账户级就必须真的探测不到 Hello"


def test_no_hello_gate_always_passes() -> None:
    gate = NoHelloGate()
    assert gate.available()
    assert gate.verify("任意理由")


def test_describe_protection_is_honest_when_no_hello(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 Hello 时必须**明说**未要求 Hello，并提示可关闭恢复。"""
    from limbowave.domain import platform_capabilities
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: True)
    monkeypatch.setattr(recovery, "hello_gate", lambda: NoHelloGate())
    text = describe_protection()
    assert "未" in text and "Windows Hello" in text
    assert "关闭恢复" in text  # 告知用户可关掉


def test_describe_protection_mentions_pin_when_hello_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 Hello 时说明要写清 PIN 也算通过（用户明确过的验收口径）。"""
    from limbowave.domain import platform_capabilities
    monkeypatch.setattr(platform_capabilities, "supports_system_identity", lambda: True)
    monkeypatch.setattr(recovery, "hello_gate", lambda: recovery.PowerShellHelloGate())
    text = describe_protection()
    assert "Windows Hello" in text
    assert "PIN" in text


def test_deny_gate_rejects() -> None:
    assert not _DenyGate().verify("x")


# ---------- DPAPI（平台相关） ----------


@requires_dpapi
def test_dpapi_round_trip() -> None:
    secret = b"a" * 32
    blob = recovery.dpapi_protect(secret)
    assert blob != secret
    assert secret not in blob  # 密文里没有明文
    assert recovery.dpapi_unprotect(blob) == secret


@requires_dpapi
def test_dpapi_blob_is_base64_safe() -> None:
    blob = recovery.dpapi_protect(b"x" * 32)
    text = recovery.encode_blob(blob)
    assert recovery.decode_blob(text) == blob
    assert base64.b64encode(blob).decode() == text


# ---------- 启用 / 关闭 ----------


@requires_dpapi
def test_enable_recovery_adds_wrap(vault: Vault, tmp_path: Path) -> None:
    assert not vault.recovery_enabled()
    notice = vault.enable_recovery(PASSWORD)
    assert vault.recovery_enabled()
    assert notice  # 返回给界面展示的说明

    methods = [w["method"] for w in _wraps(tmp_path / "vault.json")]
    assert "password" in methods
    assert METHOD_DPAPI in methods


@requires_dpapi
def test_enable_requires_correct_password(vault: Vault) -> None:
    """防止他人趁人离开时悄悄启用恢复。"""
    with pytest.raises(InvalidPassword):
        vault.enable_recovery("wrong password")
    assert not vault.recovery_enabled()


@requires_dpapi
def test_master_key_not_plaintext_in_vault_file(vault: Vault, tmp_path: Path) -> None:
    """vault.json 里不得出现任何可用的明文密钥。"""
    vault.enable_recovery(PASSWORD)
    # DPAPI 封装是 base64，解出来也是系统密文——不该有 32 字节可直接当密钥的内容
    for wrap in _wraps(tmp_path / "vault.json"):
        raw = base64.b64decode(wrap["blob"])
        assert len(raw) > 32  # 密文比密钥长（含 DPAPI 头）
        assert raw != raw[:32] * (len(raw) // 32)  # 不是重复的明文块


@requires_dpapi
def test_disable_recovery(vault: Vault) -> None:
    vault.enable_recovery(PASSWORD)
    assert vault.disable_recovery() is True
    assert not vault.recovery_enabled()
    assert vault.disable_recovery() is False  # 幂等：本来就没开


# ---------- 重置主密码 ----------


@requires_dpapi
def test_reset_password_without_old_one(vault: Vault, tmp_path: Path) -> None:
    """核心场景：忘掉主密码，靠系统保护重置。"""
    vault.enable_recovery(PASSWORD)
    # 模拟「忘了旧密码」：新开一个 Vault 实例，只用恢复路径
    fresh = Vault(tmp_path / "vault.json", params=FAST_KDF)
    fresh.unlock_with_recovery("brand new password", gate=NoHelloGate())

    # 旧密码失效
    old = Vault(tmp_path / "vault.json", params=FAST_KDF)
    with pytest.raises(InvalidPassword):
        old.unlock(PASSWORD)
    # 新密码可用
    new = Vault(tmp_path / "vault.json", params=FAST_KDF)
    new.unlock("brand new password")


@requires_dpapi
def test_reset_keeps_same_master_key(vault: Vault, tmp_path: Path) -> None:
    """**关键不变量**：重置改的是「包裹主密钥的方式」，不是主密钥本身——
    因此重置前后加密的数据都能解开。"""
    from limbowave.infrastructure.crypto.secret_store import SecretStore

    vault.enable_recovery(PASSWORD)
    store = SecretStore(vault.require_key(), tmp_path / "secrets.json")
    store.set("key-a", "sk-super-secret-value")

    # 重置主密码
    fresh = Vault(tmp_path / "vault.json", params=FAST_KDF)
    fresh.unlock_with_recovery("another password", gate=NoHelloGate())

    # 用**新密码解出的主密钥**读旧密文：必须读得出来
    reopened = Vault(tmp_path / "vault.json", params=FAST_KDF)
    reopened.unlock("another password")
    store_after = SecretStore(reopened.require_key(), tmp_path / "secrets.json")
    assert store_after.get("key-a") == "sk-super-secret-value"


@requires_dpapi
def test_reset_rejected_when_gate_denies(vault: Vault, tmp_path: Path) -> None:
    """身份验证不通过：什么都不改。"""
    vault.enable_recovery(PASSWORD)
    fresh = Vault(tmp_path / "vault.json", params=FAST_KDF)
    with pytest.raises(RecoveryError, match="未通过"):
        fresh.unlock_with_recovery("nope", gate=_DenyGate())

    # 旧密码仍然可用（没有任何改动）
    still = Vault(tmp_path / "vault.json", params=FAST_KDF)
    still.unlock(PASSWORD)


@requires_dpapi
def test_reset_without_recovery_enabled(vault: Vault) -> None:
    with pytest.raises(VaultError, match="未启用"):
        vault.unlock_with_recovery("x", gate=NoHelloGate())


@requires_dpapi
def test_reset_rejects_empty_password(vault: Vault) -> None:
    vault.enable_recovery(PASSWORD)
    with pytest.raises(VaultError, match="不得为空"):
        vault.unlock_with_recovery("", gate=NoHelloGate())


@requires_dpapi
def test_recovery_survives_password_change(vault: Vault, tmp_path: Path) -> None:
    """改主密码不该破坏恢复封装（只重封装 password 那份）。"""
    vault.enable_recovery(PASSWORD)
    vault.change_password(PASSWORD, "interim password")
    assert vault.recovery_enabled()

    fresh = Vault(tmp_path / "vault.json", params=FAST_KDF)
    fresh.unlock_with_recovery("final password", gate=NoHelloGate())
    final = Vault(tmp_path / "vault.json", params=FAST_KDF)
    final.unlock("final password")


# ---------- PIN 不入日志（结构性保证） ----------


def test_gate_returns_only_boolean() -> None:
    """闸门接口只返回布尔——没有任何地方能拿到 PIN 或认证内容。"""
    import inspect

    for gate_type in (_DenyGate, NoHelloGate, recovery.WinRtHelloGate):
        signature = inspect.signature(gate_type.verify)
        # 模块用了 `from __future__ import annotations`，注解以字符串形式存在
        annotation = signature.return_annotation
        assert annotation in (bool, "bool"), f"{gate_type.__name__} 返回 {annotation}"


def test_no_credential_material_in_vault_file(vault: Vault, tmp_path: Path) -> None:
    """vault.json 里不该出现任何认证相关字样。"""
    if not dpapi_available():
        pytest.skip("需要 DPAPI 才能启用恢复")
    vault.enable_recovery(PASSWORD)
    content = (tmp_path / "vault.json").read_text(encoding="utf-8").lower()
    for word in ("pin", "biometric", "fingerprint", "hello"):
        assert word not in content
