"""SSRF guard: monitors may only target public internet hosts."""

import ipaddress
import socket
from urllib.parse import urlsplit

from django.conf import settings


def _is_public(ip_text):
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global is False for private, loopback, link-local (169.254/16, incl. cloud
    # metadata), CGNAT 100.64/10, reserved, multicast and unspecified ranges.
    return ip.is_global


def _resolve(host):
    """Single seam for DNS so tests can fake it without touching the global socket module."""
    return socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)


def blocked_reason(url):
    """Return a human-readable reason if `url` must not be requested, else None.

    A host that does not resolve is NOT blocked here: the check itself will just
    fail with a connection error, which is the honest "down" result.
    """
    if getattr(settings, "ALLOW_PRIVATE_TARGETS", False):
        return None
    host = urlsplit(url).hostname
    if not host:
        return "URL без хоста"
    try:  # IP literal: decide directly, no DNS involved
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None if _is_public(str(literal)) else "Адреса веде у приватну/внутрішню мережу"
    try:
        infos = _resolve(host)
    except socket.gaierror:
        return None
    for info in infos:
        if not _is_public(info[4][0]):
            return "Адреса веде у приватну/внутрішню мережу"
    return None
