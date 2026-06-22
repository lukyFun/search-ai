"""
IP 解析 + ACL 工具。

提供：
- parse_ip_list(spec)：把 "127.0.0.1,10.0.0.0/8,::1" 这样的字符串解析为 ipaddress 网络列表
- ip_in_networks(ip, nets)：判断 IP 是否落入任一网络
- real_client_ip(request, trusted_proxies)：从 X-Forwarded-For 解析真实 IP；
    从链路最右往左跳过 trusted_proxies；遇到第一个非可信地址即为真实客户端 IP。
    若 TRUSTED_PROXIES 为空 / 链中找不到非可信项，回退到 request.client.host。
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Iterable, Optional


@lru_cache(maxsize=8)
def parse_ip_list(spec: str) -> tuple:
    nets = []
    for raw in (spec or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def ip_in_networks(ip_str: str, nets: Iterable) -> bool:
    if not ip_str:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for n in nets:
        if ip_obj.version != n.version:
            continue
        if ip_obj in n:
            return True
    return False


def real_client_ip(request, trusted_proxies: Iterable) -> str:
    """
    从 X-Forwarded-For 解析真实客户端 IP。

    XFF 语义：左边是原始客户端，右边是离我们最近的代理。从最右往左剥离 trusted_proxies，
    剩下的链尾即为真实客户端。空列表 / 无 XFF / 任何异常 → 回落到 transport 层 host。
    """
    trusted_list = list(trusted_proxies)
    direct = request.client.host if request.client else ""

    if not trusted_list:
        return direct

    # 只有 direct 也是可信代理，才允许信任 XFF
    if not ip_in_networks(direct, trusted_list):
        return direct

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return direct

    chain = [p.strip() for p in xff.split(",") if p.strip()]
    # 从右往左跳过可信代理
    for ip in reversed(chain):
        if not ip_in_networks(ip, trusted_list):
            return ip
    # 全是可信代理 → 用最左的
    return chain[0] if chain else direct
