"""真实客户端 IP 解析，为审计提供可信来源地址。

背景：audit 记 client_ip=request.client.host，经反代/网关后拿到的是代理地址。
统一 helper：所有审计挂载点走 get_client_ip(request)，规则——

  1. 直连（来源不是可信代理）：只信 TCP 对端地址，XFF/X-Real-IP 一律忽略
     （伪造头防护——攻击者直连时随手带 XFF 不能改写审计来源）；
  2. 来源是可信代理（本机/私网，可配 TRUSTED_PROXIES）：解析 X-Forwarded-For
     链，**自右向左**跳过可信代理条目，第一个非可信 IP = 真实客户端。
     右起取值是防伪造的关键：代理追加式 XFF（client, 代理把来路追加到右端）下，
     链最左的条目是客户端可自填的，左取会被"伪造 XFF 不被骗"场景打穿；
  3. XFF 缺失/全链可信：X-Real-IP 兜底，再退 TCP 对端地址。

TRUSTED_PROXIES 语义（逗号分隔，默认 "loopback, private"）：
  loopback = 127.0.0.0/8 + ::1；private = RFC1918 + fc00::/7；
  可填任意 IP/CIDR 追加；"none" = 一律不信转发头；"any" = 全信（不推荐）。
"""
from __future__ import annotations

import ipaddress

from app.core.config import settings

_KEYWORDS = {
    "loopback": ["127.0.0.0/8", "::1/128"],
    "private": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"],
}

_cache: tuple = ("", [])


def _networks() -> list:
    raw = getattr(settings, "TRUSTED_PROXIES", "") or ""
    global _cache
    if _cache[0] == raw:
        return _cache[1]
    nets: list = []
    if raw.strip().lower() == "any":
        nets = ["any"]
    elif raw.strip().lower() not in ("none", ""):
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            entries = _KEYWORDS.get(tok.lower(), [tok])
            for e in entries:
                try:
                    nets.append(ipaddress.ip_network(e, strict=False))
                except ValueError:
                    continue   # 配置写错只跳过该项，不炸请求
    _cache = (raw, nets)
    return nets


def _ip_of(value: str):
    """条目转 ip 对象；容忍 'ip:port'（部分代理的 XFF 形态）；解析失败返回 None。"""
    v = (value or "").strip().strip('"')
    if not v:
        return None
    try:
        return ipaddress.ip_address(v)
    except ValueError:
        pass
    if v.count(":") == 1:   # IPv4:port（IPv6 形如 a:b:c 不止一个冒号）
        try:
            return ipaddress.ip_address(v.split(":")[0])
        except ValueError:
            return None
    return None


def is_trusted_proxy(host: str) -> bool:
    ip = _ip_of(host)
    if ip is None:
        return False
    nets = _networks()
    if nets == ["any"]:
        return True
    return any(ip in n for n in nets if n != "any")


_LOCAL_NETS: list = []
for _kw in ("loopback", "private"):
    for _e in _KEYWORDS[_kw]:
        _LOCAL_NETS.append(ipaddress.ip_network(_e, strict=False))


def is_local_client(request) -> bool:
    """登录找回提示可见性：解析后的真实客户端
    落在 loopback/私网 才算"本机来源"。固定策略，**不随 TRUSTED_PROXIES**——
    配成 "any" 时不能把公网来源放进来。外部（公网）来源恒不可见。

    经 get_client_ip 解析（非可信来源无视 XFF）：公网攻击者伪造 XFF 也换不到提示。
    """
    ip = _ip_of(get_client_ip(request))
    if ip is None:
        return False
    return any(ip in n for n in _LOCAL_NETS)


def get_client_ip(request) -> str:
    """请求 -> 真实客户端 IP（Request/WebSocket 均可；无 .client 时返回 ""）。

    调用方占位（copilot 的 SimpleNamespace(host="copilot")）原样透传——
    非可信来源不碰 headers，占位值不被转发头污染。
    """
    remote = (request.client.host if getattr(request, "client", None) else "") or ""
    if not remote:
        return ""
    if not is_trusted_proxy(remote):
        return remote
    headers = getattr(request, "headers", None)
    xff = (headers.get("x-forwarded-for", "") if headers else "") or ""
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        parsed = [(p, _ip_of(p)) for p in parts]
        # 自右向左：跳过可信代理/不可解析条目，第一个非可信 IP 即真实客户端
        # （返回规范化 IP，'ip:port' 形态剥端口）
        for raw, ip in reversed(parsed):
            if ip is not None and not is_trusted_proxy(raw):
                return str(ip)
        # 全链可信（客户端本身是可信代理）：取最左原始来源
        for raw, ip in parsed:
            if ip is not None:
                return str(ip)
    real = (headers.get("x-real-ip", "") if headers else "") or ""
    real = real.strip()
    if real:
        real_ip = _ip_of(real)
        return str(real_ip) if real_ip is not None else real
    return remote
