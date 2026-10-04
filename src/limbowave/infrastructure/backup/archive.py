"""加密备份归档的读写（Phase 8 / §十四.3）。

文件布局：

    [4B magic "LWBK"][4B 格式版本][4B KDF time_cost][4B memory_cost]
    [4B parallelism][2B salt_len][salt][12B nonce][4B 密文长度][密文]

头是**明文**的：恢复前要能读出「这是不是 LimboWave 备份、什么版本、
用什么 KDF 参数」——但头里没有任何敏感内容。KDF 参数放进头是为了
将来调参仍能解开旧备份。

密钥派生：``Argon2id(备份密码, salt, 参数)`` → 32 字节
→ ``ChaCha20-Poly1305`` 加密 tar 归档。**不使用资料库主密钥**
（§十四.3：独立备份密码，以便迁移到新设备）。
"""

from __future__ import annotations

import io
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from limbowave.domain.backup import FORMAT_VERSION, MAGIC, WrongPassword
from limbowave.infrastructure.crypto.vault import (
    KdfParams,
    derive_kek,  # 复用同一套 Argon2id 派生（同一加密边界）
)

_HEADER_STRUCT = struct.Struct("<4sIIIIH")  # magic, version, time, memory, parallel, salt_len
_NONCE_BYTES = 12
_LENGTH_STRUCT = struct.Struct("<I")


@dataclass(frozen=True, slots=True)
class ArchiveHeader:
    """明文头。恢复前可读，用于展示与 KDF 参数回读。"""

    format_version: int
    kdf: KdfParams
    salt: bytes


def build_archive(files: dict[str, bytes]) -> bytes:
    """把 ``{归档内路径: 内容}`` 打成 tar 字节流。确定性顺序（便于比对）。"""
    buffer = io.BytesIO()
    # 不写 mtime/uid/gid：让同样的输入产出同样的 tar（可复现）
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name in sorted(files):
            content = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def unpack_archive(data: bytes) -> dict[str, bytes]:
    """解开 tar 字节流。路径经过安全检查（拒绝绝对路径与 .. 穿越）。"""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            # 防归档穿越：备份包也可能是恶意构造的
            if name.startswith("/") or ".." in Path(name).parts or ":" in name:
                raise WrongPassword(f"归档内含非法路径：{name}")
            extracted = tar.extractfile(member)
            files[name] = extracted.read() if extracted is not None else b""
    return files


def write_archive(
    path: Path, files: dict[str, bytes], password: str, *, params: KdfParams | None = None
) -> ArchiveHeader:
    """加密写盘。返回写入的明文头（供调用方记录）。"""
    import os
    import secrets

    kdf = params or KdfParams()
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    key = derive_kek(password, salt, kdf)
    payload = build_archive(files)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, payload, None)

    header = _HEADER_STRUCT.pack(
        MAGIC,
        FORMAT_VERSION,
        kdf.time_cost,
        kdf.memory_cost,
        kdf.parallelism,
        len(salt),
    )
    body = header + salt + nonce + _LENGTH_STRUCT.pack(len(sealed)) + sealed
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)  # 先写临时文件再原子替换：崩溃不留半截备份
    os.replace(tmp, path)
    return ArchiveHeader(format_version=FORMAT_VERSION, kdf=kdf, salt=salt)


def read_header(path: Path) -> ArchiveHeader:
    """只读明文头（不解密）。用于恢复前的快速识别。"""
    with path.open("rb") as handle:
        raw = handle.read(_HEADER_STRUCT.size)
    magic, version, time_cost, memory_cost, parallelism, salt_len = _HEADER_STRUCT.unpack(raw)
    if magic != MAGIC:
        raise WrongPassword("不是 LimboWave 备份文件（magic 不匹配）")
    with path.open("rb") as handle:
        handle.seek(_HEADER_STRUCT.size)
        salt = handle.read(salt_len)
    return ArchiveHeader(
        format_version=int(version),
        kdf=KdfParams(
            time_cost=int(time_cost),
            memory_cost=int(memory_cost),
            parallelism=int(parallelism),
        ),
        salt=salt,
    )


def read_archive(path: Path, password: str) -> tuple[ArchiveHeader, dict[str, bytes]]:
    """解密并解开归档。密码错或归档被篡改 → WrongPassword。"""
    raw = path.read_bytes()
    if len(raw) < _HEADER_STRUCT.size:
        raise WrongPassword("备份文件不完整")
    magic, version, time_cost, memory_cost, parallelism, salt_len = _HEADER_STRUCT.unpack(
        raw[: _HEADER_STRUCT.size]
    )
    if magic != MAGIC:
        raise WrongPassword("不是 LimboWave 备份文件（magic 不匹配）")

    offset = _HEADER_STRUCT.size
    salt = raw[offset : offset + salt_len]
    offset += salt_len
    nonce = raw[offset : offset + _NONCE_BYTES]
    offset += _NONCE_BYTES
    (cipher_len,) = _LENGTH_STRUCT.unpack(raw[offset : offset + _LENGTH_STRUCT.size])
    offset += _LENGTH_STRUCT.size
    sealed = raw[offset : offset + cipher_len]

    header = ArchiveHeader(
        format_version=int(version),
        kdf=KdfParams(
            time_cost=int(time_cost),
            memory_cost=int(memory_cost),
            parallelism=int(parallelism),
        ),
        salt=salt,
    )
    key = derive_kek(password, salt, header.kdf)
    try:
        payload = ChaCha20Poly1305(key).decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        raise WrongPassword("备份密码错误，或备份文件已损坏") from exc
    return header, unpack_archive(payload)
