import logging
import time
from datetime import timedelta
from smtplib import SMTPException

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, Q, Value
from django.utils import timezone

from .http_client import guarded_session
from .models import CheckResult, Incident, Monitor
from .security import blocked_reason

logger = logging.getLogger(__name__)

# beat ticks every 60s and last_checked_at is the *start* of the previous check,
# so without slack a 60s monitor would drift to a 120s cadence.
DUE_SLACK = timedelta(seconds=10)


def due_monitors(now=None):
    """Active monitors whose next check is due. Filtering happens in SQL."""
    now = now or timezone.now()
    one_second = Value(timedelta(seconds=1), output_field=DurationField())
    return (
        Monitor.objects.filter(is_active=True)
        .annotate(
            due_at=ExpressionWrapper(
                F("last_checked_at") + F("interval_seconds") * one_second,
                output_field=DateTimeField(),
            )
        )
        .filter(Q(last_checked_at__isnull=True) | Q(due_at__lte=now + DUE_SLACK))
    )


@shared_task
def schedule_checks():
    ids = list(due_monitors().values_list("pk", flat=True))
    for monitor_id in ids:
        check_monitor.delay(monitor_id)
    return len(ids)


def perform_http_check(url):
    """Returns (status_code, response_time_ms, is_up, error). Never raises."""
    reason = blocked_reason(url)
    if reason:
        return None, None, False, f"Заблоковано: {reason}"
    started = time.monotonic()
    try:
        # allow_redirects=False: 3xx counts as "up" per spec, we don't chase it.
        # stream=True: we only need the headers, not the body.
        with (
            guarded_session() as session,
            session.get(
                url,
                timeout=settings.CHECK_TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": "UptimeMonitor/1.0"},
            ) as response,
        ):
            elapsed = round((time.monotonic() - started) * 1000)
            code = response.status_code
            is_up = 200 <= code < 400
            return code, elapsed, is_up, None if is_up else f"HTTP {code}"
    except requests.Timeout:
        return None, None, False, f"Timeout after {settings.CHECK_TIMEOUT_SECONDS}s"
    except requests.RequestException as exc:
        if "non-public address" in str(exc):  # raised by the connection-level SSRF guard
            return None, None, False, "Заблоковано: адреса веде у приватну/внутрішню мережу"
        return None, None, False, f"{type(exc).__name__}: {exc}"[:500]


def update_incident(monitor, result):
    """Open on up->down, close on down->up, otherwise do nothing (no spam).

    Runs inside the monitor row lock, so two workers can't both open one;
    the partial UniqueConstraint is the last line of defence.
    """
    open_incident = Incident.objects.filter(monitor=monitor, resolved_at__isnull=True).first()
    if not result.is_up and open_incident is None:
        try:
            with transaction.atomic():
                incident = Incident.objects.create(
                    monitor=monitor,
                    started_at=result.checked_at,
                    first_error=result.error or "",
                )
        except IntegrityError:
            return
        transaction.on_commit(lambda: send_incident_notification.delay(incident.pk, "opened"))
    elif result.is_up and open_incident is not None:
        open_incident.resolved_at = result.checked_at
        open_incident.save(update_fields=["resolved_at"])
        incident_id = open_incident.pk
        transaction.on_commit(lambda: send_incident_notification.delay(incident_id, "resolved"))


def claim_monitor(monitor_id, force):
    """Atomically mark the monitor as "being checked now". True if *we* own this run.

    A single conditional UPDATE: Postgres serialises concurrent updates of the
    row and re-evaluates the WHERE clause, so of N duplicate tasks exactly one
    sees the monitor as due and wins. No lock is held afterwards, so the slow
    HTTP request below runs outside any transaction.
    """
    now = timezone.now()
    qs = Monitor.objects.filter(pk=monitor_id, is_active=True)
    if not force:
        qs = qs.filter(pk__in=due_monitors(now).values("pk"))
    # queryset.update(): no post_save, so the public-page cache isn't dropped by every check.
    return qs.update(last_checked_at=now) == 1, now


@shared_task
def check_monitor(monitor_id, force=False):
    """Claim (short) -> HTTP (no transaction) -> record (short, row-locked).

    Duplicates: a second task for the same monitor fails the claim and exits.
    Crash after claim: that interval's check is lost, the next one runs on
    schedule (trade-off: at-most-once per interval instead of a held lock).
    """
    claimed, started_at = claim_monitor(monitor_id, force)
    if not claimed:
        return "skipped"

    url = Monitor.objects.filter(pk=monitor_id).values_list("url", flat=True).first()
    if url is None:
        return "skipped"
    status_code, elapsed, is_up, error = perform_http_check(url)

    with transaction.atomic():
        # Short lock, only for writing: serialises incident open/close per monitor.
        monitor = Monitor.objects.select_for_update(of=("self",)).filter(pk=monitor_id).first()
        if monitor is None:  # deleted while we were doing the request
            return "skipped"
        result = CheckResult.objects.create(
            monitor=monitor,
            checked_at=started_at,
            status_code=status_code,
            response_time_ms=elapsed,
            is_up=is_up,
            error=error,
        )
        update_incident(monitor, result)
    return "up" if is_up else "down"


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


@shared_task
def cleanup_old_checks():
    """Delete in batches so we never hold one giant transaction / table lock."""
    cutoff = timezone.now() - timedelta(days=settings.CHECK_RETENTION_DAYS)
    total = 0
    while True:
        ids = list(
            CheckResult.objects.filter(checked_at__lt=cutoff).values_list("pk", flat=True)[
                : settings.CLEANUP_BATCH_SIZE
            ]
        )
        if not ids:
            break
        deleted, _ = CheckResult.objects.filter(pk__in=ids).delete()
        total += deleted
    return total
