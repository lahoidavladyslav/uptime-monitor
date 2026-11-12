"""H1: webhooks with a transactional outbox. Each test pins one pitfall from the brief."""

import hmac
import json
import threading
from datetime import timedelta

import pytest
import requests
import responses
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from monitors import webhooks
from monitors.models import Incident, Monitor, OutboxEvent, WebhookEndpoint
from monitors.tasks import check_monitor, dispatch_outbox, update_incident

HOOK = "https://hooks.example.org/in"
SECRET = "s3cret-for-tests"


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Сайт", url="https://example.com")


@pytest.fixture
def hook(mon):
    return WebhookEndpoint.objects.create(monitor=mon, url=HOOK, secret=SECRET)


def make_event(hook, **over):
    fields = dict(
        endpoint=hook, event_type="incident.opened", payload={"hello": "світ"}, next_attempt_at=timezone.now()
    )
    return OutboxEvent.objects.create(**{**fields, **over})


def make_due(event):
    OutboxEvent.objects.filter(pk=event.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))


class Receiver:
    """A fake receiver: records exactly what arrived on the wire, replies with `status`."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return (self.status, {}, "")


@pytest.fixture
def receiver():
    r = Receiver()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.POST, HOOK, callback=r)
        r.rsps = rsps
        yield r


# ---------------- outbox is written with the incident ----------------
def run_check(mon, status, capture):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, mon.url, status=status)
        with capture(execute=False):
            check_monitor(mon.pk, force=True)


def test_opening_an_incident_writes_one_event_per_active_endpoint(
    mon, hook, other_user, django_capture_on_commit_callbacks
):
    off = WebhookEndpoint.objects.create(monitor=mon, url="https://off.example.org/x", is_active=False)
    foreign = Monitor.objects.create(user=other_user, name="x", url="https://x.example.com")
    WebhookEndpoint.objects.create(monitor=foreign, url="https://other.example.org/x")
    run_check(mon, 503, django_capture_on_commit_callbacks)
    events = OutboxEvent.objects.all()
    assert [e.endpoint_id for e in events] == [hook.pk] and off.pk not in {e.endpoint_id for e in events}
    e = events[0]
    assert (e.event_type, e.status, e.attempts) == ("incident.opened", "pending", 0)
    assert e.payload["monitor"] == {"id": mon.pk, "name": "Сайт", "url": "https://example.com"}
    assert e.payload["incident"]["first_error"] == "HTTP 503" and e.payload["event_id"] == str(e.event_id)


def test_resolving_writes_a_resolved_event_and_repeats_write_nothing(mon, hook, django_capture_on_commit_callbacks):
    run_check(mon, 503, django_capture_on_commit_callbacks)
    run_check(mon, 503, django_capture_on_commit_callbacks)  # down -> down: no spam
    assert OutboxEvent.objects.count() == 1
    run_check(mon, 200, django_capture_on_commit_callbacks)
    assert list(OutboxEvent.objects.order_by("id").values_list("event_type", flat=True)) == [
        "incident.opened",
        "incident.resolved",
    ]


def test_incident_and_outbox_commit_or_roll_back_together(mon, hook):
    class Boom(Exception):
        pass

    result = type("R", (), {"is_up": False, "checked_at": timezone.now(), "error": "x"})()
    with pytest.raises(Boom), transaction.atomic():
        update_incident(mon, result)
        assert Incident.objects.count() == 1 and OutboxEvent.objects.count() == 1  # visible inside...
        raise Boom
    assert Incident.objects.count() == 0 and OutboxEvent.objects.count() == 0  # ...gone together


# ---------------- signing ----------------
def deliver_one(hook, receiver, **kw):
    e = make_event(hook, **kw)
    dispatch_outbox()
    e.refresh_from_db()
    return e


def test_request_is_signed_over_the_exact_bytes_that_were_sent(hook, receiver):
    e = deliver_one(hook, receiver, payload={"текст": "значення", "n": [3, 1, 2], "z": 1, "a": 2})
    req = receiver.requests[0]
    body = req.body if isinstance(req.body, bytes) else req.body.encode()
    header = req.headers["X-Signature"]
    assert header.startswith("sha256=")
    expected = hmac.new(SECRET.encode(), body, "sha256").hexdigest()
    assert header == f"sha256={expected}"
    assert webhooks.verify_signature(SECRET, body, header)  # what a receiver does
    assert json.loads(body) == e.payload  # ...and the body really is the payload


def test_the_same_payload_always_serialises_to_the_same_bytes(hook):
    a = webhooks.serialise({"b": 1, "a": {"y": 2, "x": 1}})
    b = webhooks.serialise({"a": {"x": 1, "y": 2}, "b": 1})
    assert a == b and isinstance(a, bytes) and b" " not in a


def test_headers_carry_event_id_and_timestamp(hook, receiver):
    e = deliver_one(hook, receiver)
    h = receiver.requests[0].headers
    assert h["X-Event-Id"] == str(e.event_id) and h["Content-Type"] == "application/json"
    assert abs(int(h["X-Timestamp"]) - timezone.now().timestamp()) < 5
    assert h["User-Agent"].startswith("UptimeMonitor")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body, sig, secret: (body + b" ", sig, secret),  # tampered body
        lambda body, sig, secret: (body, sig, "another-secret"),  # wrong secret
        lambda body, sig, secret: (body, sig.replace("sha256=", ""), secret),  # missing scheme
        lambda body, sig, secret: (body, "sha256=" + "0" * 64, secret),  # wrong digest
        lambda body, sig, secret: (body, "", secret),
        lambda body, sig, secret: (body, "sha1=" + sig[7:], secret),
    ],
)
def test_receiver_rejects_anything_but_the_genuine_signature(mutate):
    body = webhooks.serialise({"a": 1})
    sig = webhooks.sign(SECRET, body)
    b, s, k = mutate(body, sig, SECRET)
    assert webhooks.verify_signature(k, b, s) is False


def test_signature_comparison_is_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(webhooks.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    body = webhooks.serialise({"a": 1})
    webhooks.verify_signature(SECRET, body, webhooks.sign(SECRET, body))
    assert calls, "verify_signature must use hmac.compare_digest"


def test_secret_is_encrypted_at_rest(hook):
    with connection.cursor() as cur:
        cur.execute("SELECT secret FROM monitors_webhookendpoint WHERE id = %s", [hook.pk])
        raw = cur.fetchone()[0]
    assert SECRET not in raw and raw.startswith("fernet:")
    hook.refresh_from_db()
    assert hook.secret == SECRET


def test_new_endpoints_get_a_long_random_secret(mon):
    a, b = (
        WebhookEndpoint.objects.create(monitor=mon, url=HOOK),
        WebhookEndpoint.objects.create(monitor=mon, url=HOOK + "2"),
    )
    assert a.secret != b.secret and len(a.secret) >= 32


# ---------------- delivery, retries, dead ----------------
def test_success_marks_the_event_delivered(hook, receiver):
    e = deliver_one(hook, receiver)
    assert (e.status, e.attempts, e.last_error) == ("delivered", 1, "") and e.processed_at is not None


@pytest.mark.parametrize("status", [500, 404, 302, 429])
def test_non_2xx_is_a_failure_and_redirects_are_not_followed(hook, status):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, HOOK, status=status, headers={"Location": "http://169.254.169.254/"})
        e = deliver_one(hook, None)
    assert (e.status, e.attempts) == ("pending", 1) and f"HTTP {status}" in e.last_error


def test_network_errors_and_timeouts_are_failures_not_crashes(hook):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, HOOK, body=requests.Timeout("slow"))
        e = deliver_one(hook, None)
    assert e.status == "pending" and "Timeout" in e.last_error


def test_backoff_doubles_from_a_minute_with_jitter_and_dies_after_eight(hook, monkeypatch):
    monkeypatch.setattr(webhooks.random, "uniform", lambda lo, hi: 1.0)  # no jitter: exact sequence
    e = make_event(hook)
    delays = []
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, HOOK, status=500)
        for _ in range(8):
            make_due(e)
            dispatch_outbox()
            e.refresh_from_db()
            delays.append(round((e.next_attempt_at - timezone.now()).total_seconds() / 60))
    assert delays[:7] == [1, 2, 4, 8, 16, 32, 64]  # 1 min, 2, 4, 8 ...
    assert (e.status, e.attempts) == ("dead", 8) and e.processed_at is None
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, HOOK, status=200)
        make_due(e)
        dispatch_outbox()
        assert len(rsps.calls) == 0  # dead events are never sent again


def test_jitter_stays_within_bounds(hook):
    low = webhooks.next_delay(3, uniform=lambda lo, hi: lo)
    high = webhooks.next_delay(3, uniform=lambda lo, hi: hi)
    assert timedelta(seconds=240 * 0.75) <= low < high <= timedelta(seconds=240 * 1.25)


def test_events_that_are_not_due_are_left_alone(hook, receiver):
    make_event(hook, next_attempt_at=timezone.now() + timedelta(minutes=5))
    dispatch_outbox()
    assert receiver.requests == []


def test_a_dead_or_slow_endpoint_does_not_block_the_others(mon):
    bad = WebhookEndpoint.objects.create(monitor=mon, url="https://dead.example.org/x", secret="a")
    good = WebhookEndpoint.objects.create(monitor=mon, url="https://good.example.org/x", secret="b")
    bad_events = [make_event(bad) for _ in range(3)]
    good_event = make_event(good)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, "https://dead.example.org/x", body=requests.ConnectTimeout("no route"))
        rsps.add(responses.POST, "https://good.example.org/x", status=200)
        dispatch_outbox()
    good_event.refresh_from_db()
    assert good_event.status == "delivered"
    assert all(OutboxEvent.objects.get(pk=e.pk).attempts == 1 for e in bad_events)


def test_delivering_an_event_twice_is_harmless_for_our_state(hook, receiver):
    e = make_event(hook)
    dispatch_outbox()
    from monitors.tasks import deliver_outbox_event

    assert deliver_outbox_event(e.pk) == "skipped"  # a duplicate Celery delivery of the task
    assert len(receiver.requests) == 1


def test_inactive_endpoint_does_not_receive_already_queued_events(hook, receiver):
    make_event(hook)
    WebhookEndpoint.objects.filter(pk=hook.pk).update(is_active=False)
    dispatch_outbox()
    assert receiver.requests == []


# ---------------- SSRF ----------------
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/hook",
        "http://127.0.0.1:8000/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://[::1]/hook",
        "http://[::ffff:10.0.0.1]/hook",
        "ftp://example.org/hook",
        "file:///etc/passwd",
    ],
)
def test_private_and_odd_urls_cannot_be_saved(auth_client, mon, url, monkeypatch):
    import socket

    def resolver(host):  # the suite's blanket fake says "public" for every name; "localhost" must be itself
        ip = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr("monitors.security._resolve", resolver)
    r = auth_client.post(reverse("webhook_create", args=[mon.pk]), {"url": url})
    assert not WebhookEndpoint.objects.exists()
    assert r.status_code in (200, 302)


def test_a_hostname_that_resolves_to_a_private_address_cannot_be_saved(auth_client, mon, monkeypatch):
    import socket

    monkeypatch.setattr(
        "monitors.security._resolve", lambda host: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]
    )
    auth_client.post(reverse("webhook_create", args=[mon.pk]), {"url": "https://sneaky.example.org/hook"})
    assert not WebhookEndpoint.objects.exists()


def test_dns_rebinding_after_creation_is_caught_at_delivery(hook, monkeypatch):
    import socket

    monkeypatch.setattr(
        "monitors.security._resolve", lambda host: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
    )
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, HOOK, status=200)
        e = deliver_one(hook, None)
        assert len(rsps.calls) == 0  # no request was made at all
    assert e.status == "pending" and "Заблоковано" in e.last_error


# ---------------- claiming: concurrency, leases, crashes ----------------
def test_a_claimed_event_is_leased_and_not_claimed_again_until_the_lease_expires(hook):
    e = make_event(hook)
    assert webhooks.claim_due_events(10) == [e.pk]
    assert webhooks.claim_due_events(10) == []  # leased
    OutboxEvent.objects.filter(pk=e.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    assert webhooks.claim_due_events(10) == [e.pk]  # the worker that had it never reported back
    e.refresh_from_db()
    assert e.attempts == 2


def test_pending_events_that_burned_all_attempts_in_crashes_become_dead(hook):
    e = make_event(hook, attempts=webhooks.MAX_ATTEMPTS, next_attempt_at=timezone.now() - timedelta(seconds=1))
    assert webhooks.claim_due_events(10) == []
    e.refresh_from_db()
    assert e.status == "dead"


@pytest.mark.django_db(transaction=True)
def test_two_workers_claiming_at_once_never_share_an_event(mon):
    hook = WebhookEndpoint.objects.create(monitor=mon, url=HOOK)
    ids = {make_event(hook).pk for _ in range(24)}
    barrier, results = threading.Barrier(2), []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(webhooks.claim_due_events(24))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=15) for t in threads]
    claimed = [i for r in results for i in r]
    assert len(claimed) == len(set(claimed)), "an event was claimed by both workers"
    assert set(claimed) == ids


@pytest.mark.django_db(transaction=True)
def test_a_locked_row_does_not_block_other_workers(mon):
    hook = WebhookEndpoint.objects.create(monitor=mon, url=HOOK)
    events = [make_event(hook) for _ in range(6)]
    locked_ids = [e.pk for e in events[:2]]
    got, done = [], threading.Event()

    def other_worker():
        try:
            got.extend(webhooks.claim_due_events(10))
        finally:
            connection.close()
            done.set()

    with transaction.atomic():
        list(OutboxEvent.objects.select_for_update().filter(pk__in=locked_ids))  # a slow worker holds two rows
        threading.Thread(target=other_worker).start()
        assert done.wait(timeout=10), "claim blocked on a locked row instead of skipping it"
    assert set(got) == {e.pk for e in events[2:]}


# ---------------- UI: endpoints, log, retry ----------------
def test_webhooks_page_is_owner_only(auth_client, other_user):
    foreign = Monitor.objects.create(user=other_user, name="x", url="https://x.example.com")
    for name in ("webhooks", "webhook_create"):
        assert auth_client.get(reverse(name, args=[foreign.pk])).status_code == 404


def test_create_endpoint_shows_url_and_secret_and_lists_deliveries(auth_client, mon, receiver):
    auth_client.post(reverse("webhook_create", args=[mon.pk]), {"url": HOOK})
    ep = WebhookEndpoint.objects.get()
    e = make_event(ep)
    dispatch_outbox()
    html = auth_client.get(reverse("webhooks", args=[mon.pk])).content.decode()
    assert HOOK in html and ep.secret in html
    assert "incident.opened" in html and "delivered" in html and str(e.event_id)[:8] in html


def test_endpoints_per_monitor_are_capped(auth_client, mon):
    for i in range(webhooks.MAX_ENDPOINTS + 2):
        auth_client.post(reverse("webhook_create", args=[mon.pk]), {"url": f"https://h{i}.example.org/x"})
    assert WebhookEndpoint.objects.count() == webhooks.MAX_ENDPOINTS


def test_delete_endpoint_removes_its_events(auth_client, mon, hook):
    make_event(hook)
    auth_client.post(reverse("webhook_delete", args=[mon.pk, hook.pk]))
    assert not WebhookEndpoint.objects.exists() and not OutboxEvent.objects.exists()


def test_retry_resets_only_dead_events_of_my_monitors(auth_client, mon, hook, other_user):
    dead = make_event(hook, status="dead", attempts=8, last_error="HTTP 500")
    done = make_event(hook, status="delivered", attempts=1)
    r = auth_client.post(reverse("webhook_retry", args=[mon.pk, dead.pk]))
    assert r.status_code == 302
    dead.refresh_from_db()
    assert (dead.status, dead.attempts) == ("pending", 0) and dead.next_attempt_at <= timezone.now()
    assert auth_client.post(reverse("webhook_retry", args=[mon.pk, done.pk])).status_code == 404  # not dead
    done.refresh_from_db()
    assert done.status == "delivered"
    # someone else's event, addressed through my monitor id or through theirs
    foreign_mon = Monitor.objects.create(user=other_user, name="f", url="https://f.example.com")
    foreign_hook = WebhookEndpoint.objects.create(monitor=foreign_mon, url=HOOK)
    foreign_dead = make_event(foreign_hook, status="dead")
    assert auth_client.post(reverse("webhook_retry", args=[mon.pk, foreign_dead.pk])).status_code == 404
    assert auth_client.post(reverse("webhook_retry", args=[foreign_mon.pk, foreign_dead.pk])).status_code == 404
    foreign_dead.refresh_from_db()
    assert foreign_dead.status == "dead"


def test_retry_is_post_only(auth_client, mon, hook):
    dead = make_event(hook, status="dead")
    assert auth_client.get(reverse("webhook_retry", args=[mon.pk, dead.pk])).status_code == 405


def test_the_retried_event_is_delivered_by_the_next_run(auth_client, mon, hook, receiver):
    dead = make_event(hook, status="dead", attempts=8)
    auth_client.post(reverse("webhook_retry", args=[mon.pk, dead.pk]))
    dispatch_outbox()
    dead.refresh_from_db()
    assert dead.status == "delivered" and len(receiver.requests) == 1


# ---------------- housekeeping ----------------
def test_old_processed_events_are_purged_pending_ones_never(hook):
    from monitors.tasks import cleanup_outbox

    old = timezone.now() - timedelta(days=31)
    stale = [make_event(hook, status=s) for s in ("delivered", "dead")]
    pending = make_event(hook, status="pending")
    OutboxEvent.objects.update(created_at=old)
    assert cleanup_outbox() == 2
    assert list(OutboxEvent.objects.values_list("pk", flat=True)) == [pending.pk]
    assert not OutboxEvent.objects.filter(pk__in=[e.pk for e in stale]).exists()


def test_dispatcher_is_scheduled_often(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["dispatch-outbox"]
    assert entry["task"] == "monitors.tasks.dispatch_outbox" and entry["schedule"] <= 30
    assert settings.CELERY_TASK_ROUTES["monitors.tasks.dispatch_outbox"] == {"queue": "notifications"}
    assert settings.CELERY_TASK_ROUTES["monitors.tasks.deliver_outbox_event"] == {"queue": "notifications"}
