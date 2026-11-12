"""Webhook delivery: signing, the outbox write, claiming and sending.

Delivery is at-least-once. Duplicates are possible (a worker may die after the receiver answered but
before we recorded it), which is why every request carries a stable X-Event-Id.
"""

import hashlib
import hmac
import json
import random
import time
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .http_client import guarded_session
from .models import OutboxEvent, WebhookEndpoint
from .security import blocked_reason

MAX_ATTEMPTS = 8
BASE_DELAY_SECONDS = 60  # 1 min, 2, 4, 8 ... before jitter
LEASE = timedelta(minutes=2)  # how long a claimed event is invisible to other workers
REQUEST_TIMEOUT = (3.05, 5)  # connect, read: one slow receiver must not pin a worker
BATCH = 50
MAX_ENDPOINTS = 5
USER_AGENT = "UptimeMonitor-Webhook/1.0"


# ----------------------------- signing -----------------------------
def serialise(payload: dict) -> bytes:
    """The ONE place a payload becomes bytes. The signature is computed over, and the request sends, these
    exact bytes (never a dict serialised a second time, whose key order or spacing could differ)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """What a receiver does. Constant-time comparison, so the check leaks nothing about the digest."""
    return hmac.compare_digest(sign(secret, body).encode(), header.encode())


# ----------------------------- outbox write -----------------------------
def build_payload(event_type, monitor, incident, event_id) -> dict:
    return {
        "event_id": str(event_id),
        "type": event_type,
        "occurred_at": (incident.resolved_at or incident.started_at).isoformat(),
        "monitor": {"id": monitor.pk, "name": monitor.name, "url": monitor.url},
        "incident": {
            "id": incident.pk,
            "started_at": incident.started_at.isoformat(),
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
            "first_error": incident.first_error,
        },
    }


def enqueue_incident_event(monitor, incident, event_type) -> int:
    """Write outbox rows for the monitor's active endpoints. MUST be called inside the transaction that
    creates/changes the incident: either both are committed or neither is."""
    rows = []
    for endpoint in WebhookEndpoint.objects.filter(monitor=monitor, is_active=True):
        event_id = uuid.uuid4()
        rows.append(
            OutboxEvent(
                event_id=event_id,
                endpoint=endpoint,
                event_type=event_type,
                payload=build_payload(event_type, monitor, incident, event_id),
            )
        )
    OutboxEvent.objects.bulk_create(rows)
    if rows:
        from .tasks import dispatch_outbox

        transaction.on_commit(dispatch_outbox.delay)  # low latency; the periodic run is the safety net
    return len(rows)


def enqueue_monitor_event(monitor, event_type, extra=None) -> int:
    """A monitor-level (not incident-level) event, e.g. monitor.flapping. Same outbox, same transaction."""
    rows = []
    for endpoint in WebhookEndpoint.objects.filter(monitor=monitor, is_active=True):
        event_id = uuid.uuid4()
        payload = {
            "event_id": str(event_id),
            "type": event_type,
            "occurred_at": timezone.now().isoformat(),
            "monitor": {"id": monitor.pk, "name": monitor.name, "url": monitor.url},
            **(extra or {}),
        }
        rows.append(OutboxEvent(event_id=event_id, endpoint=endpoint, event_type=event_type, payload=payload))
    OutboxEvent.objects.bulk_create(rows)
    if rows:
        from .tasks import dispatch_outbox

        transaction.on_commit(dispatch_outbox.delay)
    return len(rows)


# ----------------------------- claiming -----------------------------
def claim_due_events(limit=BATCH) -> list[int]:
    """Lease up to `limit` due events to the caller; two concurrent callers never get the same row.

    SELECT ... FOR UPDATE SKIP LOCKED inside a short transaction: rows another worker is claiming right
    now are skipped, not waited for. The lease (next_attempt_at in the future) outlives the transaction, so
    the slow HTTP request happens with no lock held, and a worker that dies mid-delivery just lets the lease
    expire. attempts is counted at claim time, so a crash-looping event can't retry forever.
    """
    now = timezone.now()
    with transaction.atomic():
        OutboxEvent.objects.filter(
            status=OutboxEvent.Status.PENDING, attempts__gte=MAX_ATTEMPTS, next_attempt_at__lte=now
        ).update(status=OutboxEvent.Status.DEAD)
        ids = list(
            OutboxEvent.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(
                status=OutboxEvent.Status.PENDING,
                attempts__lt=MAX_ATTEMPTS,
                next_attempt_at__lte=now,
                endpoint__is_active=True,
            )
            .order_by("next_attempt_at", "id")
            .values_list("pk", flat=True)[:limit]
        )
        OutboxEvent.objects.filter(pk__in=ids).update(attempts=F("attempts") + 1, next_attempt_at=now + LEASE)
    return ids


def next_delay(attempts: int, uniform=None) -> timedelta:
    """Exponential backoff with +-25% jitter (so a recovered receiver isn't hit by every retry at once)."""
    uniform = uniform or random.uniform
    return timedelta(seconds=BASE_DELAY_SECONDS * 2 ** (attempts - 1) * uniform(0.75, 1.25))


# ----------------------------- sending -----------------------------
def send(event: OutboxEvent) -> str | None:
    """POST the event. Returns None on success, otherwise a short error text. Never raises."""
    endpoint = event.endpoint
    reason = blocked_reason(endpoint.url)  # re-checked on every attempt: DNS may have changed since saving
    if reason:
        return f"Заблоковано: {reason}"
    body = serialise(event.payload)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Event-Id": str(event.event_id),
        "X-Event-Type": event.event_type,
        "X-Timestamp": str(int(time.time())),
        "X-Signature": sign(endpoint.secret, body),
    }
    try:
        with (
            guarded_session() as session,
            session.post(
                endpoint.url, data=body, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True
            ) as response,
        ):
            if 200 <= response.status_code < 300:
                return None
            return f"HTTP {response.status_code}"
    except Exception as exc:
        text = str(exc)
        if "non-public address" in text:  # the connection-level guard (DNS rebinding)
            return "Заблоковано: адреса веде у приватну/внутрішню мережу"
        return f"{type(exc).__name__}: {text}"[:500]


def record_result(event_id: int, error: str | None) -> None:
    """Conditional on still being pending, so an owner's manual retry in the meantime isn't overwritten."""
    now = timezone.now()
    qs = OutboxEvent.objects.filter(pk=event_id, status=OutboxEvent.Status.PENDING)
    if error is None:
        qs.update(status=OutboxEvent.Status.DELIVERED, processed_at=now, last_error="")
        return
    attempts = qs.values_list("attempts", flat=True).first() or 1
    if attempts >= MAX_ATTEMPTS:
        qs.update(status=OutboxEvent.Status.DEAD, last_error=error)
    else:
        qs.update(next_attempt_at=now + next_delay(attempts), last_error=error)
