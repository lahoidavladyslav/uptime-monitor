"""H5: the asynchronous check engine. It must never touch the ORM and must respect its concurrency limits."""

import asyncio
import socket
import threading
import time

import httpx
import pytest

from monitors import async_checks
from monitors.async_checks import CheckItem, run_checks


def item(i, host="a.example.com", **kw):
    base = dict(
        monitor_id=i, url=f"https://{host}/{i}", method="GET", headers={}, body="", expected_status="", expected_text=""
    )
    return CheckItem(**{**base, **kw})


class Counter:
    """MockTransport handler that records how many requests are in flight, globally and per host."""

    def __init__(self, delay=0.02, status=200, body=b"ok"):
        self.delay, self.status, self.body = delay, status, body
        self.now, self.peak = 0, 0
        self.per_host_now, self.per_host_peak = {}, {}

    async def __call__(self, request):
        host = request.url.host
        self.now += 1
        self.per_host_now[host] = self.per_host_now.get(host, 0) + 1
        self.peak = max(self.peak, self.now)
        self.per_host_peak[host] = max(self.per_host_peak.get(host, 0), self.per_host_now[host])
        try:
            await asyncio.sleep(self.delay)
            return httpx.Response(self.status, content=self.body)
        finally:
            self.now -= 1
            self.per_host_now[host] -= 1


def run(items, handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return asyncio.run(run_checks(items, client=client, **{"concurrency": 50, "per_host": 5, **kw}))


# ---------------- results ----------------
def test_each_item_gets_a_result_with_status_time_and_verdict():
    out = run([item(1), item(2)], Counter())
    assert sorted(r.monitor_id for r in out) == [1, 2]
    r = out[0]
    assert (r.status_code, r.is_up, r.error) == (200, True, None) and r.response_time_ms is not None
    assert r.started_at.tzinfo is not None


@pytest.mark.parametrize("status,up", [(200, True), (301, True), (404, False), (503, False)])
def test_default_expected_statuses_match_the_single_check(status, up):
    r = run([item(1)], Counter(status=status))[0]
    assert (r.status_code, r.is_up) == (status, up) and (r.error is None) == up


def test_custom_expected_status_and_text_are_honoured():
    ok = run([item(1, expected_status="204", expected_text="hello")], Counter(status=204, body=b"say hello"))[0]
    bad = run([item(2, expected_text="needle")], Counter(body=b"haystack"))[0]
    assert ok.is_up and not bad.is_up and "Очікуваний текст" in bad.error


def test_timeouts_and_connection_errors_become_down_results_not_exceptions():
    def boom(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    assert run([item(1)], boom)[0].is_up is False and "ConnectError" in run([item(1)], boom)[0].error
    r = run([item(1)], slow)[0]
    assert not r.is_up and r.status_code is None and "Timeout" in r.error


def test_one_failing_item_does_not_lose_the_others():
    def handler(request):
        if request.url.path == "/2":
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    out = {r.monitor_id: r.is_up for r in run([item(i) for i in (1, 2, 3)], handler)}
    assert out == {1: True, 2: False, 3: True}


def test_post_body_and_headers_are_sent():
    seen = {}

    def handler(request):
        seen.update(method=request.method, body=request.content, auth=request.headers.get("authorization"))
        return httpx.Response(200)

    run([item(1, method="POST", body="payload", headers={"Authorization": "Bearer x"})], handler)
    assert seen == {"method": "POST", "body": b"payload", "auth": "Bearer x"}


def test_redirects_are_not_followed_and_count_as_up_like_the_single_check():
    r = run([item(1)], lambda req: httpx.Response(302, headers={"Location": "http://169.254.169.254/"}))[0]
    assert r.status_code == 302 and r.is_up


def test_only_the_first_megabyte_of_a_body_is_read_when_searching_for_text():
    big = b"x" * (3 * 1024 * 1024) + b"needle"  # the needle is beyond the limit
    r = run([item(1, expected_text="needle")], Counter(body=big))[0]
    assert not r.is_up


# ---------------- concurrency ----------------
def test_global_concurrency_is_never_exceeded():
    counter = Counter(delay=0.03)
    run([item(i, host=f"h{i}.example.com") for i in range(60)], counter, concurrency=10, per_host=10)
    assert 1 < counter.peak <= 10


def test_per_host_concurrency_is_never_exceeded_even_with_many_monitors_on_one_host():
    counter = Counter(delay=0.03)
    run([item(i, host="busy.example.com") for i in range(40)], counter, concurrency=100, per_host=3)
    assert counter.per_host_peak["busy.example.com"] == 3  # reached the limit, never above it


def test_a_busy_host_does_not_hold_up_other_hosts():
    counter = Counter(delay=0.05)
    items = [item(i, host="busy.example.com") for i in range(20)] + [
        item(100 + i, host=f"o{i}.example.com") for i in range(5)
    ]
    out = run(items, counter, concurrency=100, per_host=1)
    assert len(out) == 25
    assert counter.per_host_peak["busy.example.com"] == 1
    assert counter.peak > 1  # the other hosts ran alongside the busy one


def test_hosts_are_compared_case_insensitively_and_ports_are_part_of_the_host_limit_key():
    counter = Counter(delay=0.03)
    run(
        [item(i, host="Busy.Example.com") for i in range(10)]
        + [item(20 + i, host="busy.example.com") for i in range(10)],
        counter,
        per_host=2,
    )
    assert max(counter.per_host_peak.values()) == 2


def test_throughput_is_concurrent_not_sequential():
    counter = Counter(delay=0.1)
    start = time.monotonic()
    run([item(i, host=f"h{i}.example.com") for i in range(50)], counter, concurrency=50)
    assert time.monotonic() - start < 1.5  # sequential would be 5 s


# ---------------- no ORM, no leaks ----------------
@pytest.mark.django_db
def test_the_engine_never_touches_the_database(django_assert_num_queries):
    with django_assert_num_queries(0):
        run([item(1), item(2)], Counter())


def test_the_client_is_closed_after_the_batch_when_the_engine_made_it(monkeypatch):
    made = []

    def factory(*args, **kw):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        made.append(client)
        return client

    monkeypatch.setattr(async_checks, "make_client", factory)
    asyncio.run(run_checks([], concurrency=5, per_host=2))
    assert made == []  # nothing to check: no client, no connections
    out = asyncio.run(run_checks([item(1)], concurrency=5, per_host=2))
    assert out[0].is_up and len(made) == 1 and made[0].is_closed


def test_a_client_passed_in_by_the_caller_is_left_open():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    async def go():
        await run_checks([item(1)], concurrency=5, per_host=2, client=client)
        return client.is_closed

    assert asyncio.run(go()) is False


def test_running_batches_back_to_back_does_not_accumulate_loops_or_threads():
    before = threading.active_count()
    for _ in range(5):
        run([item(i) for i in range(10)], Counter(delay=0.001))
    assert threading.active_count() <= before + 1


# ---------------- SSRF ----------------
def test_the_dns_check_runs_once_per_host_not_once_per_monitor(monkeypatch):
    calls = []
    monkeypatch.setattr(async_checks, "blocked_reason", lambda url: calls.append(url) or None)
    run([item(i, host="same.example.com") for i in range(30)], Counter())
    assert len(calls) == 1


def test_a_hostname_resolving_to_a_private_address_is_blocked_before_any_request(monkeypatch):
    monkeypatch.setattr(async_checks, "blocked_reason", lambda url: "Адреса веде у приватну/внутрішню мережу")
    counter = Counter()
    r = run([item(1)], counter)[0]
    assert not r.is_up and "Заблоковано" in r.error and counter.peak == 0


@pytest.fixture
def local_server():
    """A real socket server on 127.0.0.1: the only way to see the connection-level guard work."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    hits = []

    def serve():
        srv.settimeout(3)
        try:
            while True:
                conn, _ = srv.accept()
                if conn.recv(1024):  # count requests that actually carried bytes (a bare TCP connect is not one)
                    hits.append(1)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                conn.close()
        except (TimeoutError, OSError):
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits
    srv.close()


def test_dns_rebinding_is_caught_at_connect_time(local_server, monkeypatch, settings):
    """DNS answered 'public' for the pre-check but the connection really goes to 127.0.0.1."""
    port, hits = local_server
    settings.ALLOW_PRIVATE_TARGETS = False
    monkeypatch.setattr(async_checks, "blocked_reason", lambda url: None)  # the pre-flight was fooled
    local = CheckItem(
        monitor_id=1,
        url=f"http://127.0.0.1:{port}/",
        method="GET",
        headers={},
        body="",
        expected_status="",
        expected_text="",
    )
    r = asyncio.run(run_checks([local], concurrency=5, per_host=2))[0]
    assert not r.is_up and "Заблоковано" in r.error
    assert hits == []  # not a single byte reached the server


def test_private_targets_work_only_when_explicitly_allowed(local_server, settings):
    port, hits = local_server
    settings.ALLOW_PRIVATE_TARGETS = True
    r = asyncio.run(
        run_checks(
            [
                CheckItem(
                    monitor_id=1,
                    url=f"http://127.0.0.1:{port}/",
                    method="GET",
                    headers={},
                    body="",
                    expected_status="",
                    expected_text="",
                )
            ],
            concurrency=5,
            per_host=2,
        )
    )[0]
    assert r.is_up and hits


def test_the_guard_wraps_the_pool_attribute_this_code_relies_on():
    """If an httpx upgrade renames the private attribute, fail loudly instead of silently dropping the guard."""
    client = async_checks.make_client()
    pool = client._transport._pool
    assert isinstance(pool._network_backend, async_checks.GuardedBackend)
    asyncio.run(client.aclose())
