"""SSRF protection for upstream URLs registered at runtime.

Always blocks cloud metadata endpoints and link-local/unspecified addresses (checked on every
address the hostname resolves to, not just the literal). When an allowlist is configured,
the hostname must also be on it — the recommended setting for any internet-facing gateway.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "instance-data", "metadata.azure.com"}


class UnsafeUpstream(ValueError):
    pass


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:  # note: ipaddress classifies ::1 as "reserved", so check loopback first
        return None
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "unspecified/multicast/reserved address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _blocked_ip(ip.ipv4_mapped)
    return None


async def check_upstream_url(url: str, allowlist: list[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUpstream("upstream URLs must be http(s)")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUpstream("upstream URL has no host")
    if host in BLOCKED_HOSTNAMES:
        raise UnsafeUpstream(f"host '{host}' is a metadata endpoint")
    if allowlist and host not in {h.lower() for h in allowlist}:
        raise UnsafeUpstream(f"upstream host '{host}' is not in upstream_host_allowlist")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return  # unresolvable now; registration proceeds and the upstream simply stays unhealthy
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    for ip in addresses:
        reason = _blocked_ip(ip)
        if reason:
            raise UnsafeUpstream(f"host '{host}' resolves to {ip}: {reason}")
