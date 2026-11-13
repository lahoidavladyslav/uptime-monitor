import asyncio
import logging
import time
from datetime import UTC, timedelta
from smtplib import SMTPException
from typing import NamedTuple

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from django.db import IntegrityError, connection, transaction
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, Q, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from . import async_checks, check_types, maintenance, partitions, regions, rollups, subscriptions, webhooks
from .checks import MAX_BODY_BYTES
from .http_client import guarded_session
from .models import (
    CheckResult,
    Incident,
    Monitor,
    MonitorRegion,
    OutboxEvent,
    StatusSubscriber,
)
from .security import blocked_reason

logger = logging.getLogger(__name__)

# last_checked_at is the *start* of the previous check and the scheduler ticks every 10 s (settings), so without
# slack a 60 s monitor would drift to a 70 s cadence and, under load, miss a whole tick (see README, H5).
DUE_SLACK = timedelta(seconds=10)


def under_maintenance(now):
    """Exists() subquery: a concrete maintenance window (the monitor's own or its user's) covers `now`."""
    return maintenance.under_maintenance_expr(now)


def due_monitors(now=None):
    """Active monitors whose next check is due. Filtering happens in SQL.

    A maintenance window does NOT stop the checks any more: they run and are stored (flagged), they just open
    no incident and count towards no uptime.
    """
    now = now or timezone.now()
    one_second = Value(timedelta(seconds=1), output_field=DurationField())
    return (
        Monitor.objects.filter(is_active=True)
        .exclude(check_type="ssl_expiry")  # checked once a day by check_ssl, not every interval
        .annotate(
            due_at=ExpressionWrapper(
                F("last_checked_at") + F("interval_seconds") * one_second,
                output_field=DateTimeField(),
            )
        )
        .filter(Q(last_checked_at__isnull=True) | Q(due_at__lte=now + DUE_SLACK))
    )


def enqueue_checks(monitor_id, force=False):
    """One task per region, each on that region's own queue (`checks.<region>`)."""
    kind = Monitor.objects.filter(pk=monitor_id).values_list("check_type", flat=True).first()
    if kind is not None and not check_types.get(kind).needs_http:
        return  # ssl_expiry: "check now" is check_ssl (the callers enqueue it), there is nothing to request here
    for region in settings.CHECK_REGIONS:
        check_monitor.apply_async(
            args=[monitor_id], kwargs={"force": force, "region": region}, queue=f"checks.{region}"
        )


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _take_queue_slots(monitor_ids, region, now):
    """Mark (monitor, region) as queued and return the ids WE marked: one conditional UPDATE ... RETURNING.

    A monitor already waiting in line (queued_at fresh) is skipped, so when the queue grows faster than it
    drains the scheduler stops feeding it instead of piling five checks onto one monitor. A flag older than
    QUEUED_TTL is treated as a lost task and taken again.
    """
    MonitorRegion.objects.bulk_create(
        [MonitorRegion(monitor_id=i, region=region) for i in monitor_ids], ignore_conflicts=True
    )
    with connection.cursor() as cur:
        cur.execute(
            """UPDATE monitors_monitorregion SET queued_at = %s
               WHERE region = %s AND monitor_id = ANY(%s)
                 AND (queued_at IS NULL OR queued_at < %s - make_interval(secs => %s))
               RETURNING monitor_id""",
            [now, region, list(monitor_ids), now, settings.QUEUED_TTL_SECONDS],
        )
        taken = {row[0] for row in cur.fetchall()}
    return [i for i in monitor_ids if i in taken]  # keep the "most overdue first" order


@shared_task
def schedule_checks():
    """Every minute: put due monitors on the region queues in BATCHES, most overdue first, unless already queued."""
    now = timezone.now()
    ids = list(due_monitors(now).order_by(F("due_at").asc(nulls_first=True), "pk").values_list("pk", flat=True))
    for region in settings.CHECK_REGIONS if ids else []:
        for chunk in _chunks(_take_queue_slots(ids, region, now), settings.CHECK_BATCH_SIZE):
            check_batch.apply_async(args=[chunk], kwargs={"region": region}, queue=f"checks.{region}")
    return len(ids)


def _read_body(response, deadline):
    """Read at most MAX_BODY_BYTES and never longer than the overall deadline (slow-drip servers).

    Returns (text, truncated): `truncated` means we stopped before the end of the body, so "not found" in it
    proves less than usual and the check reports that.
    """
    chunks, size, truncated = [], 0, False
    for chunk in response.iter_content(chunk_size=8192):
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BODY_BYTES or time.monotonic() > deadline:
            truncated = True
            break
    text = b"".join(chunks)[:MAX_BODY_BYTES].decode(response.encoding or "utf-8", errors="replace")
    return text, truncated


class CheckOutcome(NamedTuple):
    status_code: int | None
    response_time_ms: int | None
    is_up: bool
    error: str | None
    details: dict


def _failed(message, **details):
    return CheckOutcome(None, None, False, message, {"message": message, **details})


def perform_http_check(url, *, method="GET", headers=None, body="", check_type="http_status", check_config=None):
    """Run one HTTP check of any type. Returns a CheckOutcome. Never raises."""
    strategy = check_types.get(check_type)
    config = check_config or {}
    reason = blocked_reason(url)
    if reason:
        return _failed(f"Заблоковано: {reason}", type=check_type)
    timeout = settings.CHECK_TIMEOUT_SECONDS
    started = time.monotonic()
    request_headers = {"User-Agent": "UptimeMonitor/1.0", **(headers or {})}
    try:
        # allow_redirects=False: 3xx counts as "up" by default, we don't chase it.
        # stream=True: headers first; the body is read only for the types that look into it.
        with (
            guarded_session() as session,
            session.request(
                method,
                url,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                headers=request_headers,
                data=body.encode() if body and method == "POST" else None,
            ) as response,
        ):
            elapsed = round((time.monotonic() - started) * 1000)
            text, truncated = "", False
            if strategy.needs_body and method != "HEAD":
                text, truncated = _read_body(response, started + timeout)
            result = strategy.evaluate_http(config, check_types.HttpResponse(response.status_code, text, truncated))
            return CheckOutcome(response.status_code, elapsed, result.is_up, result.message, result.details)
    except requests.Timeout:
        return _failed(f"Timeout after {timeout}s", type=check_type)
    except requests.RequestException as exc:
        if "non-public address" in str(exc):  # raised by the connection-level SSRF guard
            return _failed("Заблоковано: адреса веде у приватну/внутрішню мережу", type=check_type)
        return _failed(f"{type(exc).__name__}: {exc}"[:500], type=check_type)


FLAP_WINDOW = timedelta(minutes=15)
FLAP_MAX_TRANSITIONS = 4  # MORE than this many state changes inside FLAP_WINDOW = flapping


def recent_transitions(monitor, at):
    """Incident openings + closings in the 15 minutes up to `at` (each is one state change)."""
    since = at - FLAP_WINDOW
    incidents = Incident.objects.filter(monitor=monitor)
    return (
        incidents.filter(started_at__gt=since, started_at__lte=at).count()
        + incidents.filter(resolved_at__gt=since, resolved_at__lte=at).count()
    )


def _muted_by_flapping(monitor, at):
    """True if this transition's notifications must be suppressed. Starting to flap announces it ONCE."""
    if monitor.flapping_since is not None:
        return True
    if recent_transitions(monitor, at) > FLAP_MAX_TRANSITIONS:
        monitor.flapping_since = at
        Monitor.objects.filter(pk=monitor.pk).update(flapping_since=at)
        _announce(monitor, "flapping")
        return True
    return False


def _maybe_stabilise(monitor, at):
    """A quiet FLAP_WINDOW ends flapping, with one notice that says what state the monitor settled in."""
    if monitor.flapping_since is not None and recent_transitions(monitor, at) == 0:
        monitor.flapping_since = None
        Monitor.objects.filter(pk=monitor.pk).update(flapping_since=None)
        _announce(monitor, "stabilised")


def _announce(monitor, kind):
    """One 'unstable' / 'stabilised' notice: mail to the owner + a webhook event (written in this transaction)."""
    is_down = Incident.objects.filter(monitor=monitor, resolved_at__isnull=True).exists()
    webhooks.enqueue_monitor_event(monitor, f"monitor.{kind}", {"state": "down" if is_down else "up"})
    monitor_id = monitor.pk
    transaction.on_commit(lambda: send_flapping_notice.delay(monitor_id, kind, is_down))


def update_incident(monitor, result):
    """Open on up->down, close on down->up, otherwise do nothing (no spam)."""
    set_incident_state(monitor, result.is_up, result.checked_at, result.error)


def set_incident_state(monitor, is_up, at, error, in_maintenance=False):
    """Apply a verdict to the monitor's incident. Runs inside the monitor row lock, so two workers can't both
    open one; the partial UniqueConstraint is the last line of defence.

    - inside a maintenance window nothing is opened; an incident that was already open is NOT closed
      artificially, it is flagged `during_maintenance` (and closes normally on real recovery);
    - after a transition, notifications go out unless the monitor is flapping (see _muted_by_flapping).
    """
    open_incident = Incident.objects.filter(monitor=monitor, resolved_at__isnull=True).first()
    if not is_up and open_incident is None:
        if in_maintenance:
            return
        try:
            with transaction.atomic():
                incident = Incident.objects.create(monitor=monitor, started_at=at, first_error=error or "")
        except IntegrityError:
            return
        if _muted_by_flapping(monitor, at):
            return
        webhooks.enqueue_incident_event(monitor, incident, "incident.opened")  # same transaction as the incident
        transaction.on_commit(lambda: _notify_everyone(incident.pk, "opened"))
    elif not is_up:
        if in_maintenance and not open_incident.during_maintenance:
            Incident.objects.filter(pk=open_incident.pk).update(during_maintenance=True)
    elif open_incident is not None:
        open_incident.resolved_at = at
        open_incident.save(update_fields=["resolved_at"])
        if _muted_by_flapping(monitor, at):
            return
        incident_id = open_incident.pk
        webhooks.enqueue_incident_event(monitor, open_incident, "incident.resolved")
        transaction.on_commit(lambda: _notify_everyone(incident_id, "resolved"))


@shared_task(
    autoretry_for=(SMTPException, OSError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def send_flapping_notice(monitor_id, kind, is_down):
    monitor = Monitor.objects.select_related("user").filter(pk=monitor_id).first()
    if monitor is None or not monitor.user.email:
        return "skipped"
    if kind == "flapping":
        subject = f"⚠️ {monitor.name} нестабільний"
        body = (
            f"Монітор «{monitor.name}» змінив стан понад {FLAP_MAX_TRANSITIONS} разів за 15 хвилин.\n"
            "Щоб не засипати вас листами, наступні сповіщення про цей монітор вимкнено до стабілізації. "
            "Історія інцидентів записується як зазвичай."
        )
    else:
        state = "недоступний" if is_down else "працює"
        subject = f"🟡 {monitor.name} стабілізувався"
        body = f"Монітор «{monitor.name}» не змінював стан 15 хвилин. Зараз він {state}. Сповіщення знову увімкнено."
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [monitor.user.email])
    return "sent"


@shared_task
def expand_maintenance(now=None, horizon_days=maintenance.HORIZON_DAYS):
    """Materialise the next month of every recurring maintenance rule (idempotent)."""
    return maintenance.expand_windows(now, horizon_days)


def _notify_everyone(incident_id, kind):
    """The owner (private details) and the public subscribers (public details only)."""
    send_incident_notification.delay(incident_id, kind)
    notify_subscribers.delay(incident_id, kind)


def claim_monitor(monitor_id, force, region=None):
    """Atomically mark the monitor as "being checked now" BY THIS REGION. True if *we* own this run.

    A single conditional UPDATE on the (monitor, region) clock: Postgres serialises concurrent updates of the
    row and re-evaluates the WHERE clause, so of N duplicate tasks of one region exactly one sees the check as
    due and wins, while the other regions (their own clock rows) run independently. No lock is held afterwards,
    so the slow HTTP request below runs outside any transaction.
    """
    region = region or regions.default_region()
    now = timezone.now()
    monitor = Monitor.objects.filter(pk=monitor_id, is_active=True).values("interval_seconds").first()
    if monitor is None:
        return False, now
    MonitorRegion.objects.bulk_create([MonitorRegion(monitor_id=monitor_id, region=region)], ignore_conflicts=True)
    clock = MonitorRegion.objects.filter(monitor_id=monitor_id, region=region)
    if not force:
        due_before = now - timedelta(seconds=monitor["interval_seconds"]) + DUE_SLACK
        clock = clock.filter(Q(last_checked_at__isnull=True) | Q(last_checked_at__lte=due_before))
    if clock.update(last_checked_at=now) != 1:
        return False, now
    # queryset.update(): no post_save, so the public-page cache isn't dropped by every check. The monitor-level
    # clock is what the scheduler and the dashboard read.
    Monitor.objects.filter(pk=monitor_id).update(last_checked_at=now)
    return True, now


@shared_task
def check_monitor(monitor_id, force=False, region=None):
    """Claim (short) -> HTTP (no transaction) -> record (short, row-locked).

    Duplicates: a second task for the same monitor AND region fails the claim and exits.
    Crash after claim: that interval's check is lost for that region, the next one runs on
    schedule (trade-off: at-most-once per interval instead of a held lock).
    """
    region = region or regions.default_region()
    claimed, started_at = claim_monitor(monitor_id, force, region)
    if not claimed:
        return "skipped"

    cfg = (
        Monitor.objects.filter(pk=monitor_id)
        .values("url", "http_method", "request_headers", "request_body", "check_type", "check_config")
        .first()
    )
    if cfg is None:
        return "skipped"
    outcome = perform_http_check(
        cfg["url"],
        method=cfg["http_method"],
        headers=cfg["request_headers"],
        body=cfg["request_body"],
        check_type=cfg["check_type"],
        check_config=cfg["check_config"],
    )
    return record_check(monitor_id, region, started_at, outcome)


def record_check(monitor_id, region, checked_at, outcome):
    """Write one region's result and let the quorum decide about the incident.

    The monitor row lock serialises everything that can change incident state, so regions reporting at the
    same moment are evaluated one after the other, each seeing the other's committed result.
    """
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update(of=("self",)).filter(pk=monitor_id).first()
        if monitor is None:  # deleted while we were doing the request
            return "skipped"
        in_window = maintenance.is_active(monitor, checked_at)
        result = CheckResult.objects.create(
            monitor=monitor,
            region=region,
            checked_at=checked_at,
            status_code=outcome.status_code,
            response_time_ms=outcome.response_time_ms,
            is_up=outcome.is_up,
            error=outcome.error,
            details=outcome.details,
            in_maintenance=in_window,
        )
        _maybe_stabilise(monitor, checked_at)
        verdict = regions.decide(monitor, result)
        if verdict is not None:
            set_incident_state(
                monitor, verdict, checked_at, outcome.error if not verdict else None, in_maintenance=in_window
            )
    return "up" if outcome.is_up else "down"


def claim_batch(monitor_ids, region, force=False):
    """Claim many monitors for one region in a single UPDATE ... FROM ... RETURNING.

    It is the same atomic "is it due?" test as `claim_monitor`, per (monitor, region) clock: of two batches
    racing for a monitor exactly one gets it (the loser re-evaluates the WHERE after the row lock). It also
    clears the queued flag of EVERY id in the batch, claimed or not, so nothing stays "waiting" forever.
    Returns (claimed ids, now).
    """
    now = timezone.now()
    ids = list(monitor_ids)
    MonitorRegion.objects.bulk_create([MonitorRegion(monitor_id=i, region=region) for i in ids], ignore_conflicts=True)
    MonitorRegion.objects.filter(monitor_id__in=ids, region=region).update(queued_at=None)
    with connection.cursor() as cur:
        cur.execute(
            """UPDATE monitors_monitorregion r SET last_checked_at = %s
               FROM monitors_monitor m
               WHERE r.monitor_id = m.id AND r.region = %s AND r.monitor_id = ANY(%s) AND m.is_active
                 AND (%s OR r.last_checked_at IS NULL
                      OR r.last_checked_at <= %s - make_interval(secs => m.interval_seconds) + %s)
               RETURNING r.monitor_id""",
            [now, region, ids, force, now, DUE_SLACK],
        )
        claimed = sorted(row[0] for row in cur.fetchall())
    if claimed:
        Monitor.objects.filter(pk__in=claimed).update(last_checked_at=now)  # the scheduler/dashboard clock
    return claimed, now


@shared_task
def check_batch(monitor_ids, region=None, force=False):
    """Check many monitors at once: bulk claim -> ONE asyncio run (no ORM inside) -> ONE bulk write.

    A new event loop per task is deliberate: `run_checks` creates its HTTP client inside and closes it in a
    `finally`, so a long-lived Celery process never holds a loop or a socket between tasks.
    """
    region = region or regions.default_region()
    claimed, _ = claim_batch(monitor_ids, region, force)
    if not claimed:
        return {"checked": 0, "up": 0, "down": 0, "decided": 0}
    items = [
        async_checks.CheckItem(
            monitor_id=c["pk"],
            url=c["url"],
            method=c["http_method"],
            headers=c["request_headers"],
            body=c["request_body"],
            check_type=c["check_type"],
            check_config=c["check_config"],
        )
        for c in Monitor.objects.filter(pk__in=claimed).values(
            "pk", "url", "http_method", "request_headers", "request_body", "check_type", "check_config"
        )
    ]
    rows = asyncio.run(
        async_checks.run_checks(items, concurrency=settings.CHECK_CONCURRENCY, per_host=settings.CHECK_PER_HOST)
    )
    return record_batch(rows, region)


def record_batch(rows, region):
    """Write a batch of results with ONE bulk insert and run the incident logic only where it can matter.

    All the batch's monitors are locked in id order (so two workers can't deadlock), results for monitors that
    vanished meanwhile are dropped, and the per-monitor quorum/incident code runs only for monitors whose state
    can change: "up with no open incident" (the steady state, ~all of them) costs no further queries.
    """
    if not rows:
        return {"checked": 0, "up": 0, "down": 0, "decided": 0}
    now = timezone.now()
    summary = {"checked": 0, "up": 0, "down": 0, "decided": 0}
    with transaction.atomic():
        monitors = {
            m.pk: m
            for m in Monitor.objects.select_for_update(of=("self",))
            .filter(pk__in={r.monitor_id for r in rows})
            .annotate(in_window=maintenance.under_maintenance_expr(now))
            .order_by("pk")
        }
        kept = [r for r in rows if r.monitor_id in monitors]
        created = CheckResult.objects.bulk_create(
            [
                CheckResult(
                    monitor_id=r.monitor_id,
                    region=region,
                    checked_at=r.started_at,
                    status_code=r.status_code,
                    response_time_ms=r.response_time_ms,
                    is_up=r.is_up,
                    error=r.error,
                    details=r.details,
                    in_maintenance=monitors[r.monitor_id].in_window,
                )
                for r in kept
            ]
        )
        open_ids = set(
            Incident.objects.filter(monitor_id__in=list(monitors), resolved_at__isnull=True).values_list(
                "monitor_id", flat=True
            )
        )
        for row, result in zip(kept, created, strict=True):
            monitor = monitors[row.monitor_id]
            summary["checked"] += 1
            summary["up" if row.is_up else "down"] += 1
            has_open = row.monitor_id in open_ids
            changes = (
                has_open if row.is_up else (not has_open or monitor.in_window)
            ) or monitor.flapping_since is not None
            if not changes:
                continue
            summary["decided"] += 1
            _maybe_stabilise(monitor, row.started_at)
            verdict = regions.decide(monitor, result)
            if verdict is not None:
                set_incident_state(
                    monitor,
                    verdict,
                    row.started_at,
                    row.error if not verdict else None,
                    in_maintenance=monitor.in_window,
                )
    return summary


@shared_task(
    autoretry_for=(SMTPException, OSError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def send_incident_notification(incident_id, kind):
    incident = Incident.objects.select_related("monitor__user").filter(pk=incident_id).first()
    if incident is None:
        return "gone"
    monitor = incident.monitor
    if kind == "opened":
        subject = f"🔴 {monitor.name} недоступний"
        body = (
            f"Монітор «{monitor.name}» ({monitor.url}) недоступний з {incident.started_at:%Y-%m-%d %H:%M} UTC.\n"
            f"Помилка: {incident.first_error}"
        )
    else:
        subject = f"🟢 {monitor.name} знову працює"
        body = (
            f"Монітор «{monitor.name}» ({monitor.url}) відновився о {incident.resolved_at:%Y-%m-%d %H:%M} UTC.\n"
            f"Тривалість простою: {incident.resolved_at - incident.started_at}"
        )
    email = monitor.user.email
    if not email:
        return "no-email"
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email])
    return "sent"


# ----------------------------- public subscribers -----------------------------
_RETRY = dict(
    autoretry_for=(SMTPException, OSError), retry_backoff=True, retry_backoff_max=600, retry_jitter=True, max_retries=3
)


@shared_task
def notify_subscribers(incident_id, kind):
    """Fan out one task per recipient, so a failing address can't hold up or duplicate the others."""
    incident = Incident.objects.select_related("monitor").filter(pk=incident_id).first()
    if incident is None:
        return "gone"
    queued = 0
    for sub in subscriptions.recipients_for(incident):
        try:
            send_subscriber_email.delay(sub.pk, incident_id, kind)
            queued += 1
        except Exception:
            logger.exception("could not queue subscriber mail", extra={"subscriber_id": sub.pk})
    return queued


@shared_task(**_RETRY)
def send_subscriber_email(subscriber_id, incident_id, kind):
    """Public-safe content only: the public name, never the monitored URL or the error text."""
    sub = StatusSubscriber.objects.select_related("status_page", "monitor").filter(pk=subscriber_id).first()
    incident = Incident.objects.select_related("monitor").filter(pk=incident_id).first()
    if sub is None or incident is None or not sub.is_active:
        return "gone"
    name = incident.monitor.public_name
    if kind == "opened":
        subject = f"🔴 {name} недоступний"
        text = f"«{name}» недоступний з {incident.started_at:%Y-%m-%d %H:%M} UTC."
    else:
        subject = f"🟢 {name} знову працює"
        text = f"«{name}» відновився о {incident.resolved_at:%Y-%m-%d %H:%M} UTC. Простій: {incident.resolved_at - incident.started_at}."
    page_path = (
        reverse("group_status", args=[sub.status_page.slug])
        if sub.status_page_id
        else reverse("public_status", args=[sub.monitor.public_slug])
    )
    unsub = subscriptions.unsubscribe_url(sub)
    body = f"{text}\n\nСтатус: {subscriptions.absolute(page_path)}\n\nВідписатися: {unsub}"
    EmailMessage(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [sub.email],
        headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    ).send()
    logger.info("subscriber mail sent", extra={"event": "subscriber_mail_sent", "subscriber_id": sub.pk, "kind": kind})
    return "sent"


@shared_task(**_RETRY)
def send_subscription_confirmation(subscriber_id):
    sub = StatusSubscriber.objects.select_related("status_page", "monitor").filter(pk=subscriber_id).first()
    if sub is None or sub.is_confirmed or sub.unsubscribed_at is not None:
        return "skipped"
    title = sub.status_page.title if sub.status_page_id else sub.monitor.public_name
    send_mail(
        f"Підтвердіть підписку: {title}",
        f"Ви (або хтось інший) попросили надсилати сповіщення про збої «{title}» на цю адресу.\n\n"
        f"Підтвердити підписку (діє 48 годин): {subscriptions.confirm_url(sub)}\n\n"
        "Якщо це були не ви — просто проігноруйте цей лист, нічого не буде надіслано.",
        settings.DEFAULT_FROM_EMAIL,
        [sub.email],
    )
    return "sent"


@shared_task
def cleanup_unconfirmed_subscribers():
    """Pending rows whose confirmation link has expired (it can never be used any more). Age counts from the LAST
    confirmation mail, so someone who re-subscribed an old row is not purged the moment they ask."""
    cutoff = timezone.now() - timedelta(seconds=subscriptions.CONFIRM_MAX_AGE)
    deleted, _ = (
        StatusSubscriber.objects.filter(confirmed_at__isnull=True, unsubscribed_at__isnull=True)
        .annotate(last_mail=Coalesce("confirmation_sent_at", "created_at"))
        .filter(last_mail__lt=cutoff)
        .delete()
    )
    return deleted


@shared_task
def manage_partitions():
    """Hourly: create the next days' partitions, adopt strays from the default partition, drop partitions beyond
    retention (after rolling them up), prune old rollups. Replaces the old batched DELETE cleanup."""
    return partitions.manage()


# ----------------------------- SSL certificate expiry -----------------------------
SSL_THRESHOLDS = (14, 7, 1, 0)  # days; announced once each per certificate (0 = expired)


def fetch_ssl_expiry(host, port=443):
    """Returns the certificate's notAfter as an aware datetime. Raises ssl.SSLError / OSError."""
    import socket
    import ssl
    from datetime import datetime

    from .http_client import _guard

    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=settings.CHECK_TIMEOUT_SECONDS) as raw:
        _guard(raw)  # same SSRF protection as for HTTP checks
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            not_after = tls.getpeercert()["notAfter"]
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=UTC)


@shared_task
def schedule_ssl_checks():
    """Once a day (beat, 04:00): every active https monitor, including the ones of type ssl_expiry."""
    ids = list(Monitor.objects.filter(is_active=True, url__istartswith="https://").values_list("pk", flat=True))
    for monitor_id in ids:
        check_ssl.delay(monitor_id)
    return len(ids)


@shared_task
def check_ssl(monitor_id):
    """Read the certificate, keep ssl_expires_at/ssl_error current, announce crossed thresholds, and, for monitors
    of type ssl_expiry, record the verdict as a CheckResult (so incidents/uptime work like for any other check)."""
    import ssl
    from urllib.parse import urlsplit

    monitor = Monitor.objects.filter(pk=monitor_id, url__istartswith="https://").first()
    if monitor is None:
        return "skipped"
    parts = urlsplit(monitor.url)
    if blocked_reason(monitor.url):
        return "blocked"
    now = timezone.now()
    try:
        expires = fetch_ssl_expiry(parts.hostname, parts.port or 443)
    except ssl.SSLCertVerificationError as exc:
        expired = "expired" in str(exc).lower()
        message = "Сертифікат прострочений" if expired else f"Помилка сертифіката: {exc.verify_message}"[:200]
        Monitor.objects.filter(pk=monitor.pk).update(ssl_error=message, ssl_expires_at=now if expired else None)
        if expired:
            _apply_ssl_thresholds(monitor, now, 0)
        reason = "certificate has expired" if expired else f"certificate is invalid: {exc.verify_message}"
        _record_ssl_result(monitor, now, False, reason, {"type": "ssl_expiry", "days_left": 0})
        return "invalid"
    except (OSError, ssl.SSLError) as exc:
        # not a verdict about the certificate (the network may be down): no result, no incident
        Monitor.objects.filter(pk=monitor.pk).update(ssl_error=f"{type(exc).__name__}"[:200])
        return "error"

    days_left = (expires - now).days if expires > now else 0
    previous_expiry = monitor.ssl_expires_at
    Monitor.objects.filter(pk=monitor.pk).update(ssl_expires_at=expires, ssl_error="")
    renewed = previous_expiry is not None and expires > previous_expiry + timedelta(days=1)
    _apply_ssl_thresholds(monitor, expires, days_left, renewed=renewed)
    if monitor.check_type == "ssl_expiry":
        verdict = check_types.get("ssl_expiry").evaluate_ssl(monitor.check_config, days_left)
        _record_ssl_result(monitor, now, verdict.is_up, verdict.message, verdict.details)
    return days_left


def _record_ssl_result(monitor, at, is_up, message, details):
    if monitor.check_type != "ssl_expiry":
        return
    Monitor.objects.filter(pk=monitor.pk).update(last_checked_at=at)
    record_check(
        monitor.pk,
        regions.default_region(),
        at,
        CheckOutcome(
            None, None, is_up, None if is_up else message, details if is_up else {**details, "message": message}
        ),
    )


def _apply_ssl_thresholds(monitor, expires, days_left, renewed=False):
    """Mail once per threshold per certificate. `monitor` is the row as the caller read it: the state change is a
    conditional UPDATE on exactly that value, so of two workers that read the same row only one wins and sends."""
    seen = [] if renewed else list(monitor.ssl_notified)
    crossed = [t for t in SSL_THRESHOLDS if days_left <= t]
    new_state = sorted(set(seen) | set(crossed), reverse=True) if crossed else []  # healthy again: forget everything
    if new_state == list(monitor.ssl_notified):
        return
    won = Monitor.objects.filter(pk=monitor.pk, ssl_notified=monitor.ssl_notified).update(ssl_notified=new_state)
    # the state only changes when a threshold is newly crossed (or everything is forgotten, when `crossed` is empty)
    if won and crossed:
        send_ssl_alert.delay(monitor.pk, days_left)


@shared_task(
    autoretry_for=(SMTPException, OSError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def send_ssl_alert(monitor_id, days_left):
    monitor = Monitor.objects.select_related("user").filter(pk=monitor_id).first()
    if monitor is None or not monitor.user.email:
        return "skipped"
    if days_left <= 0:
        subject = f"🔴 SSL-сертифікат {monitor.name} прострочений"
        body = f"Сертифікат сайту «{monitor.name}» ({monitor.url}) прострочений."
    else:
        subject = f"⚠️ SSL-сертифікат {monitor.name} спливає через {days_left} дн."
        body = f"Сертифікат сайту «{monitor.name}» ({monitor.url}) діє ще {days_left} дн. Оновіть його."
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [monitor.user.email])
    return "sent"


# ----------------------------- hourly rollups -----------------------------
def rollup_range(start, end):
    """Recompute rollup rows for [start, end). Thin wrapper kept for callers and tests."""
    return rollups.build(start, end)


@shared_task
def build_rollups():
    """Every 5 minutes: recompute the previous and the current hour (a result that arrives late still has
    its own hour open), with a single INSERT ... ON CONFLICT DO UPDATE."""
    now = timezone.now()
    return rollups.build(rollups.floor_hour(now) - timedelta(hours=1), now + timedelta(seconds=1))


# ----------------------------- webhook outbox -----------------------------
@shared_task
def dispatch_outbox():
    """Claim due events (short transaction, SKIP LOCKED, lease) and hand each to its own task: a dead or
    slow endpoint then occupies only its own delivery, never the queue behind it."""
    queued = 0
    for event_id in webhooks.claim_due_events():
        try:
            deliver_outbox_event.delay(event_id)
            queued += 1
        except Exception:
            logger.exception("could not queue webhook delivery", extra={"event_id": event_id})  # lease expires, retried
    return queued


@shared_task
def deliver_outbox_event(event_id):
    event = (
        OutboxEvent.objects.select_related("endpoint").filter(pk=event_id, status=OutboxEvent.Status.PENDING).first()
    )
    if event is None or not event.endpoint.is_active:
        return "skipped"  # delivered/dead already (duplicate task) or the endpoint was switched off
    error = webhooks.send(event)
    webhooks.record_result(event_id, error)
    logger.info(
        "webhook delivery",
        extra={
            "event": "webhook_delivery",
            "event_id": str(event.event_id),
            "attempt": event.attempts,
            "ok": error is None,
        },
    )
    return "delivered" if error is None else "failed"


@shared_task
def cleanup_outbox():
    """Delivered and dead events older than 30 days. Pending ones are never purged."""
    cutoff = timezone.now() - timedelta(days=30)
    deleted, _ = OutboxEvent.objects.filter(
        status__in=[OutboxEvent.Status.DELIVERED, OutboxEvent.Status.DEAD], created_at__lt=cutoff
    ).delete()
    return deleted
