"""
SSRF 地址守卫（安全审查 P0-7）

用于 MCP 网页抓取类工具（fetch / tavily_extract / tavily_crawl / tavily_map）：
调用前校验 URL 只能指向公网 http/https 地址，拒绝私网、回环、链路本地等内网地址。

背景：LLM Agent 的抓取工具是经典 SSRF 的 agent 变体 —— 任意注册用户可通过
聊天消息（或提前把恶意指令放进 RAG 文档/网页，即间接提示注入）诱导 LLM 访问
`http://169.254.169.254`（云元数据）、内网 `192.168.x.x`、`localhost` 等地址，
并把抓取结果回传，等价于服务器的任意内网探测。

残余风险说明：
- DNS rebinding（解析后域名 IP 变化）：本守卫在调用前解析一次并校验，
  理论上仍存在竞态窗口；彻底防御需在连接层（如代理）再次校验。
- 域名解析为多 IP 时按"任一命中禁止段即拒绝"处理（防绕过）。
"""

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

# 禁止访问的 IP 范围（IPv4 + IPv6）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),        # "本网络" 地址
    ipaddress.ip_network("10.0.0.0/8"),       # 私网
    ipaddress.ip_network("100.64.0.0/10"),    # 运营商级 NAT
    ipaddress.ip_network("127.0.0.0/8"),      # 回环（localhost）
    ipaddress.ip_network("169.254.0.0/16"),   # 链路本地（云元数据 169.254.169.254 在此段）
    ipaddress.ip_network("172.16.0.0/12"),    # 私网
    ipaddress.ip_network("192.168.0.0/16"),   # 私网
    ipaddress.ip_network("198.18.0.0/15"),    # 基准测试段
    ipaddress.ip_network("224.0.0.0/4"),      # 组播
    ipaddress.ip_network("240.0.0.0/4"),      # 保留
    ipaddress.ip_network("::1/128"),          # IPv6 回环
    ipaddress.ip_network("::/128"),           # IPv6 未指定地址
    ipaddress.ip_network("fc00::/7"),         # IPv6 唯一本地地址
    ipaddress.ip_network("fe80::/10"),        # IPv6 链路本地
    ipaddress.ip_network("ff00::/8"),         # IPv6 组播
]


def is_blocked_ip(ip: str) -> bool:
    """
    判断 IP 是否属于禁止访问的私网/保留地址段

    Args:
        ip: IP 字符串（IPv4 或 IPv6）

    Returns:
        禁止返回 True；无法解析的 IP 一律视为不安全返回 True
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return is_blocked_ip(str(addr.ipv4_mapped))
    return not addr.is_global or any(addr in net for net in _BLOCKED_NETWORKS)


def validate_public_url(url: str) -> None:
    """
    校验 URL 是否可安全抓取（仅公网 http/https）

    Args:
        url: 待校验的完整 URL

    Raises:
        ValueError: 协议不是 http/https、缺少主机名、解析到禁止访问的地址
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL 为空")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https 协议，收到: {parsed.scheme or '无协议'}")

    host = parsed.hostname
    if not host:
        raise ValueError(f"URL 缺少主机名: {url[:200]}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL 不允许包含用户凭据")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc

    # 主机名本身是字面 IP → 直接校验（支持 IPv6 字面量）
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # 非字面 IP，走 DNS 解析
    else:
        if is_blocked_ip(host):
            raise ValueError(f"禁止访问私网/保留地址: {host}")
        return

    # DNS 解析后校验（任一解析结果命中禁止段即拒绝）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"域名解析失败: {host}（{e}）")

    ips = {info[4][0] for info in infos}
    if not ips:
        raise ValueError(f"域名未解析出任何 IP: {host}")
    for ip in ips:
        if is_blocked_ip(ip):
            raise ValueError(f"域名 {host} 解析到禁止访问的地址: {ip}")


def extract_url_arg(args: tuple, kwargs: dict) -> Optional[str]:
    """
    从工具调用的参数中提取 url 参数（兼容位置参数与关键字参数）

    Args:
        args: 位置参数（可能包含单个 dict）
        kwargs: 关键字参数

    Returns:
        url 值（不存在返回 None）
    """
    if args and isinstance(args[0], dict):
        return args[0].get("url")
    return kwargs.get("url")


def extract_url_args(args: tuple, kwargs: dict, fields: set[str]) -> list[str]:
    """Collect declared URL fields in positional/dict/nested/list tool input."""
    values: list[str] = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in fields and item is not None:
                    items = item if isinstance(item, list) else [item]
                    if any(not isinstance(url, str) or not url.strip() for url in items):
                        raise ValueError("URL 参数必须是非空字符串或字符串数组")
                    values.extend(items)
                else:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(kwargs)
    visit(args)
    if args and isinstance(args[0], str) and not values:
        values.append(args[0])
    return list(dict.fromkeys(values))
