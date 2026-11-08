"""SSRF policy for the HTTP verifiers.

Mirrors the semantics of Hermes core's ``tools.url_safety.is_safe_url``: only ``http``/``https``,
every DNS answer validated, cloud-metadata endpoints always refused, and private/loopback/
link-local/CGNAT destinations refused unless the user explicitly opts in.

Two deliberate differences from core, both because this plugin is a *verifier* rather than a
fetcher:

* Core fails open on DNS failure when a proxy is configured, delegating resolution to the proxy.
  A verifier has no proxy path, so every failure is fail-closed.
* Core exposes a ``security.fake_ip_ranges`` exemption for TUN fake-IP DNS. The plugin has no
  such need and does not implement it, so there is one fewer way to widen the policy.

The policy is applied twice: once on the URL the model recorded, and again on every redirect
target. A redirect is just another URL the model did not name, so it gets the same treatment.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable, Optional, Tuple
from urllib.parse import unquote, urlsplit

HTTP_SCHEMES = frozenset({"http", "https"})

# Metadata hostnames — refused regardless of the private-network opt-in.
_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})

# Cloud metadata / credential endpoints: the canonical SSRF target. Refused even when the user
# has opted into private networks, because "allow my LAN" is not "let the agent read IAM creds".
_METADATA_V4 = (
    "169.254.169.254",  # AWS/GCP/Azure/DO/Oracle IMDS
    "169.254.170.2",    # AWS ECS task metadata (task IAM credentials)
    "169.254.169.253",  # Azure IMDS wire server
    "100.100.100.200",  # Alibaba Cloud metadata
)
_ALWAYS_BLOCKED_IPS = frozenset(
    {ipaddress.ip_address(ip) for ip in _METADATA_V4}
    # Resolvers may answer an IPv4 name in IPv4-mapped form; ipaddress treats that as a
    # distinct IPv6 address, so it must be listed explicitly.
    | {ipaddress.ip_address("::ffff:" + ip) for ip in _METADATA_V4}
    | {ipaddress.ip_address("fd00:ec2::254")}  # AWS metadata over IPv6
)
_ALWAYS_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("169.254.0.0/16", "::ffff:169.254.0.0/112")
)

# 100.64.0.0/10 (CGNAT, RFC 6598) is neither is_private nor is_global, so it needs its own entry.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# RFC 2765 IPv4-translated wrapper ``::ffff:0:a.b.c.d``; ``ipv4_mapped`` does not read it.
_IPV4_TRANSLATED_NETWORK = ipaddress.ip_network("::ffff:0:0:0/96")

# WHATWG clients strip these from the ends of a URL; urlsplit keeps a trailing one in the host.
_URL_EDGE_WHITESPACE = " \t\n\r\x0b\x0c"
_IDNA_DOTS = str.maketrans(dict.fromkeys("\u3002\uff0e\uff61", "."))

Resolver = Callable[[str], Any]


def _default_resolver(hostname: str) -> Any:
    return socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)


def _normalize_hostname(host: Optional[str]) -> str:
    return (host or "").strip().lower().rstrip(".")


def _embedded_ipv4(ip: Any) -> Any:
    """The IPv4 address an IPv6 wrapper stands for, or *ip* unchanged.

    Both IPv4-mapped (``::ffff:x.x.x.x``) and IPv4-translated (``::ffff:0:x.x.x.x``) forms are
    read through, so a resolver's sentinel or a metadata answer cannot arrive as unrelated
    IPv6 space.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _IPV4_TRANSLATED_NETWORK:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_always_blocked_ip(ip: Any) -> bool:
    """Cloud-metadata endpoints, which no opt-in may ever permit."""
    ip = _embedded_ipv4(ip)
    return ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS)


def is_restricted_ip(ip: Any) -> bool:
    """Private, loopback, link-local, reserved, multicast, unspecified or CGNAT."""
    ip = _embedded_ipv4(ip)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip in _CGNAT_NETWORK
    )


def _parse_ip(hostname: str) -> Optional[Any]:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _url_hostname(url: str) -> str:
    """The host as a WHATWG client would dial it: IDNA-encoded, lowercased, no trailing dot."""
    host = urlsplit(url.strip(_URL_EDGE_WHITESPACE)).hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        host = host.translate(_IDNA_DOTS)
    return _normalize_hostname(host)


def authority_ambiguity(url: str) -> Optional[str]:
    """Why another HTTP client could read a different host from *url* than ``urlsplit`` does.

    A verifier must not accept a URL whose meaning depends on which parser reads it, so these are
    refused rather than guessed at. The reason string is returned instead of the URL because a URL
    can carry userinfo credentials.
    """
    url = url.strip(_URL_EDGE_WHITESPACE)
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if parts.scheme.lower() not in HTTP_SCHEMES:
        return f"unsupported scheme {parts.scheme or '<empty>'!r}"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return "control character in URL"
    if not parts.netloc:
        return "authority not introduced by //"
    if "\\" in parts.netloc:
        return "backslash in authority"
    _userinfo, at, host = parts.netloc.rpartition("@")
    if at:
        return "userinfo in authority"
    if "%" in host:
        return "percent-encoding in host"
    if any(c.isspace() for c in host):
        return "whitespace in host"
    try:
        parts.port
    except ValueError:
        return "invalid port"
    return None


def check_url(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Optional[Resolver] = None,
) -> Tuple[bool, str]:
    """Decide whether *url* may be requested.

    Returns ``(allowed, reason)``. Every failure is fail-closed: an unparseable URL, a DNS error
    and an unexpected exception all return ``False``. *reason* never contains the URL, which may
    carry credentials.
    """
    resolver = resolver or _default_resolver
    try:
        if not isinstance(url, str) or not url.strip():
            return False, "empty url"
        reason = authority_ambiguity(url)
        if reason is not None:
            return False, reason
        hostname = _url_hostname(url)
        if not hostname:
            return False, "no host"
        if hostname in _BLOCKED_HOSTNAMES:
            return False, "cloud metadata hostname"

        literal = _parse_ip(hostname)
        if literal is not None:
            return _verdict_for_ip(literal, allow_private)

        try:
            addr_info = resolver(hostname)
        except socket.gaierror:
            return False, "dns resolution failed"
        except Exception:
            return False, "dns resolution error"

        seen = 0
        for entry in addr_info or ():
            raw = entry[4][0] if len(entry) > 4 and entry[4] else None
            if not raw:
                continue
            seen += 1
            ip = _parse_ip(str(raw).split("%")[0])
            if ip is None:
                return False, "unparseable resolved address"
            allowed, why = _verdict_for_ip(ip, allow_private)
            if not allowed:
                return False, why
        if not seen:
            return False, "dns returned no addresses"
        return True, "ok"
    except Exception:
        return False, "url safety check error"


def _verdict_for_ip(ip: Any, allow_private: bool) -> Tuple[bool, str]:
    if is_always_blocked_ip(ip):
        return False, "cloud metadata address"
    if is_restricted_ip(ip) and not allow_private:
        return False, "private, loopback, link-local or reserved address"
    return True, "ok"
