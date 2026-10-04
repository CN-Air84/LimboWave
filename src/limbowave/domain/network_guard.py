"""联网边界防护（Task 6.3 / 设计计划 §九.3）。

默认**禁止**访问：

- 环回地址：``127.0.0.0/8``、``::1``；
- 私有网段：``10/8``、``172.16/12``、``192.168/16``、``fc00::/7``；
- 链路本地：``169.254/16``、``fe80::/10``；
- 云元数据端点：``169.254.169.254``、``fd00:ec2::254`` 等——SSRF 的经典目标；
- ``0.0.0.0`` 与 ``::``（未指定地址）。

这些在**解析后**判定：域名可能解析到内网地址（DNS rebinding 的第一步），
所以既要查字面 IP，也要查解析结果。解析函数作为参数注入，便于测试与离线运行。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable

# 云元数据地址（各云厂商的已知端点）
_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / GCP / OpenStack
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IPv6 IMDS
    }
)

# 元数据主机名（部分云用固定域名）
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.goog"})


class NetworkBlocked(Exception):
    """目标地址被边界策略拒绝。"""


def is_forbidden_address(host: str) -> tuple[bool, str]:
    """字面地址是否被禁止。返回 (是否禁止, 原因)。纯函数。"""
    text = host.strip().strip("[]")  # IPv6 可能带方括号
    if not text:
        return True, "空地址"
    if text.lower() in _METADATA_HOSTS:
        return True, "云元数据主机名"
    if text in _METADATA_ADDRESSES:
        return True, "云元数据地址"
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False, ""  # 不是字面 IP（是域名），交给解析后判定
    if address.is_loopback:
        return True, "环回地址"
    if address.is_private:
        return True, "私有网段"
    if address.is_link_local:
        return True, "链路本地地址"
    if address.is_unspecified:
        return True, "未指定地址"
    if address.is_reserved:
        return True, "保留地址"
    if address.is_multicast:
        return True, "组播地址"
    return False, ""


def resolve_host(host: str) -> list[str]:
    """解析主机名为 IP 列表。解析失败返回空列表（调用方据此拒绝）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


def check_host(
    host: str,
    *,
    resolver: Callable[[str], list[str]] = resolve_host,
    allow_private: bool = False,
) -> None:
    """校验主机可访问。被禁止抛 :class:`NetworkBlocked`。

    ``allow_private=True`` 表示用户已显式授权访问内网（§九.3 的「除非用户显式授权」）。
    显式授权也只放开私有/环回，**云元数据地址始终禁止**——它是 SSRF 提权的跳板，
    不该被一次普通授权打开。
    """
    text = host.strip().strip("[]")
    if text in _METADATA_ADDRESSES or text.lower() in _METADATA_HOSTS:
        raise NetworkBlocked(f"云元数据地址始终禁止：{host}")

    forbidden, reason = is_forbidden_address(text)
    if forbidden:
        if allow_private and reason in ("环回地址", "私有网段", "链路本地地址"):
            return
        raise NetworkBlocked(f"目标地址被禁止（{reason}）：{host}")

    # 域名：解析后逐个校验（防 DNS 指向内网）
    addresses = resolver(text)
    if not addresses:
        raise NetworkBlocked(f"主机无法解析：{host}")
    for address in addresses:
        forbidden, reason = is_forbidden_address(address)
        if forbidden:
            if allow_private and reason in ("环回地址", "私有网段", "链路本地地址"):
                continue
            raise NetworkBlocked(f"{host} 解析到被禁止的地址（{reason}）：{address}")
