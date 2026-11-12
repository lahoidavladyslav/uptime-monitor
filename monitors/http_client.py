"""HTTP client that verifies the address it ACTUALLY connected to.

`security.blocked_reason()` resolves the host before the request, but the
connection resolves again: a hostile DNS server can answer "public" the first
time and "127.0.0.1" the second (DNS rebinding). Checking the peer address of
the real socket closes that window, whatever DNS said.
"""

import ipaddress

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.connection import HTTPConnection, HTTPSConnection


class BlockedAddressError(OSError):
    pass


def _is_public(ip_text):
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def _guard(sock):
    if getattr(settings, "ALLOW_PRIVATE_TARGETS", False):
        return sock
    peer = sock.getpeername()[0]
    if not _is_public(peer):
        sock.close()
        raise BlockedAddressError(f"connection to non-public address {peer} refused")
    return sock


class GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):
        return _guard(super()._new_conn())


class GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        return _guard(super()._new_conn())


class GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = GuardedHTTPConnection


class GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = GuardedHTTPSConnection


class GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": GuardedHTTPPool, "https": GuardedHTTPSPool}


def guarded_session():
    session = requests.Session()
    adapter = GuardedAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
