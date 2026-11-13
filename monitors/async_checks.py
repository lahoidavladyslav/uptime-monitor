"""Asynchronous check engine: many HTTP checks at once, in ONE event loop, with hard concurrency limits.

Rules this module keeps (each is pinned by a test):
- it never touches the ORM: it takes plain `CheckItem`s and returns plain `CheckRow`s, the caller does one bulk
  write (Django's ORM is synchronous; mixing it into coroutines means thread hops or "async unsafe" errors);
- one `asyncio.run()` per batch, and the client it creates is closed in `finally`: no loop and no connection
  outlives a Celery task, so a long-lived worker can't leak sockets;
- at most `concurrency` requests in flight, and at most `per_host` to any single host, however many monitors
  point at it (a user with 500 monitors on one domain must not DDoS that domain);
- the SSRF protection that `requests` gets from `GuardedAdapter` is repeated at the connection level (see
  `GuardedBackend`): the peer address is checked right after TCP connect, before any byte is sent.
"""

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpcore
import httpx
from django.conf import settings

from . import check_types
from .checks import MAX_BODY_BYTES
from .http_client import BlockedAddressError
from .security import blocked_reason

USER_AGENT = "UptimeMonitor/1.0"


@dataclass(frozen=True)
class CheckItem:
    monitor_id: int
    url: str
    method: str = "GET"
    headers: dict = field(default_factory=dict)
    body: str = ""
    check_type: str = "http_status"
    check_config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CheckRow:
    monitor_id: int
    started_at: datetime
    status_code: int | None
    response_time_ms: int | None
    is_up: bool
    error: str | None
    details: dict = field(default_factory=dict)


# ----------------------------- SSRF at connect time -----------------------------
def _is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Wraps another network backend and refuses to talk to a non-public peer (DNS rebinding safe)."""

    def __init__(self, inner):
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = await self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        if not getattr(settings, "ALLOW_PRIVATE_TARGETS", False):
            peer = stream.get_extra_info("server_addr")
            if peer and not _is_public(str(peer[0])):
                await stream.aclose()
                raise BlockedAddressError(f"connection to non-public address {peer[0]} refused")
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedAddressError("unix sockets are not allowed")

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


def make_client(concurrency: int = 200) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=0))
    # httpx has no public hook for the network backend: wrap the pool's. A test pins this attribute so an httpx
    # upgrade that renames it fails loudly instead of silently dropping the guard.
    pool = transport._pool
    pool._network_backend = GuardedBackend(pool._network_backend)
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=settings.CHECK_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )


# ----------------------------- the engine -----------------------------
def _host_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


async def _read_body(response: httpx.Response, deadline: float) -> tuple[str, bool]:
    chunks, size, truncated = [], 0, False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BODY_BYTES or time.monotonic() > deadline:
            truncated = True
            break
    return b"".join(chunks)[:MAX_BODY_BYTES].decode(response.encoding or "utf-8", errors="replace"), truncated


def _down(item: CheckItem, started: datetime, message: str) -> CheckRow:
    details = {"type": item.check_type, "message": message}
    return CheckRow(item.monitor_id, started, None, None, False, message, details)


async def _check_one(client, item: CheckItem, blocked: str | None) -> CheckRow:
    started = datetime.now(UTC)
    if blocked:
        return _down(item, started, f"Заблоковано: {blocked}")
    strategy = check_types.get(item.check_type)
    timeout = settings.CHECK_TIMEOUT_SECONDS
    t0 = time.monotonic()
    try:
        request = client.build_request(
            item.method,
            item.url,
            headers=item.headers or None,
            content=item.body.encode() if item.body and item.method == "POST" else None,
        )
        response = await client.send(request, stream=True)
        try:
            elapsed = round((time.monotonic() - t0) * 1000)
            text, truncated = "", False
            if strategy.needs_body and item.method != "HEAD":
                text, truncated = await _read_body(response, t0 + timeout)
            result = strategy.evaluate_http(
                item.check_config, check_types.HttpResponse(response.status_code, text, truncated)
            )
            return CheckRow(
                item.monitor_id, started, response.status_code, elapsed, result.is_up, result.message, result.details
            )
        finally:
            await response.aclose()
    except httpx.TimeoutException:
        return _down(item, started, f"Timeout after {timeout}s")
    except Exception as exc:  # network errors, protocol errors, the connection-level guard...
        if "non-public address" in str(exc):
            return _down(item, started, "Заблоковано: адреса веде у приватну/внутрішню мережу")
        return _down(item, started, f"{type(exc).__name__}: {exc}"[:500])


async def run_checks(
    items: list[CheckItem], *, concurrency: int, per_host: int, client: httpx.AsyncClient | None = None
) -> list[CheckRow]:
    """Run all checks concurrently under the limits; always returns one row per item, never raises."""
    if not items:
        return []
    own_client = client is None
    client = client or make_client(concurrency)
    global_slots = asyncio.Semaphore(concurrency)
    host_slots: dict[str, asyncio.Semaphore] = {}
    verdicts: dict[str, asyncio.Future] = {}  # DNS verdict per host, computed once per batch

    def verdict_for(item: CheckItem) -> asyncio.Future:
        key = _host_key(item.url)
        if key not in verdicts:  # blocking DNS runs in a thread, once per unique host
            verdicts[key] = asyncio.ensure_future(asyncio.to_thread(blocked_reason, item.url))
        return verdicts[key]

    async def one(item: CheckItem) -> CheckRow:
        host = host_slots.setdefault(_host_key(item.url), asyncio.Semaphore(per_host))
        async with host, global_slots:
            try:
                blocked = await verdict_for(item)
            except Exception as exc:  # a failing resolver must not lose the row
                return CheckRow(
                    item.monitor_id, datetime.now(UTC), None, None, False, f"{type(exc).__name__}: {exc}"[:500]
                )
            return await _check_one(client, item, blocked)

    try:
        return list(await asyncio.gather(*(one(i) for i in items)))
    finally:
        if own_client:
            await client.aclose()
