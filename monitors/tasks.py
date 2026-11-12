import logging
import time
from datetime import UTC, timedelta
from smtplib import SMTPException

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from django.db import IntegrityError, transaction
from django.db.models import DateTimeField, DurationField, Exists, ExpressionWrapper, F, OuterRef, Q, Value
from django.urls import reverse
from django.utils import timezone

from . import rollups, subscriptions
from .checks import MAX_BODY_BYTES, evaluate
from .http_client import guarded_session
from .models import CheckResult, CheckRollupHourly, Incident, MaintenanceWindow, Monitor, Subscriber
from .security import blocked_reason

logger = logging.getLogger(__name__)

# beat ticks every 60s and last_checked_at is the *start* of the previous check,
# so without slack a 60s monitor would drift to a 120s cadence.
DUE_SLACK = timedelta(seconds=10)


def under_maintenance(now):
    """Exists() subquery: the monitor has a maintenance window covering `now`."""
    return Exists(MaintenanceWindow.objects.filter(monitor=OuterRef("pk"), starts_at__lte=now, ends_at__gt=now))


def due_monitors(now=None):
    """Active, not-in-maintenance monitors whose next check is due. Filtering happens in SQL."""
    now = now or timezone.now()
    one_second = Value(timedelta(seconds=1), output_field=DurationField())
    return (
        Monitor.objects.filter(is_active=True)
        .exclude(under_maintenance(now))
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


def _read_body(response, deadline):
    """Read at most MAX_BODY_BYTES and never longer than the overall deadline (slow-drip servers)."""
    chunks, size = [], 0
    for chunk in response.iter_content(chunk_size=8192):
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BODY_BYTES or time.monotonic() > deadline:
            break
    return b"".join(chunks)[:MAX_BODY_BYTES].decode(response.encoding or "utf-8", errors="replace")


def perform_http_check(url, *, method="GET", headers=None, body="", expected_status="", expected_text=""):
    """Returns (status_code, response_time_ms, is_up, error). Never raises."""
    reason = blocked_reason(url)
    if reason:
        return None, None, False, f"Заблоковано: {reason}"
    timeout = settings.CHECK_TIMEOUT_SECONDS
    started = time.monotonic()
    request_headers = {"User-Agent": "UptimeMonitor/1.0", **(headers or {})}
    try:
        # allow_redirects=False: 3xx counts as "up" by default, we don't chase it.
        # stream=True: headers first; the body is read only when we must search it for text.
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
            body_text = _read_body(response, started + timeout) if expected_text and method != "HEAD" else ""
            is_up, error = evaluate(response.status_code, expected_status, expected_text, body_text)
            return response.status_code, elapsed, is_up, error
    except requests.Timeout:
        return None, None, False, f"Timeout after {timeout}s"
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
        transaction.on_commit(lambda: _notify_everyone(incident.pk, "opened"))
    elif result.is_up and open_incident is not None:
        open_incident.resolved_at = result.checked_at
        open_incident.save(update_fields=["resolved_at"])
        incident_id = open_incident.pk
        transaction.on_commit(lambda: _notify_everyone(incident_id, "resolved"))


def _notify_everyone(incident_id, kind):
    """The owner (private details) and the public subscribers (public details only)."""
    send_incident_notification.delay(incident_id, kind)
    notify_subscribers.delay(incident_id, kind)


def claim_monitor(monitor_id, force):
    """Atomically mark the monitor as "being checked now". True if *we* own this run.

    A single conditional UPDATE: Postgres serialises concurrent updates of the
    row and re-evaluates the WHERE clause, so of N duplicate tasks exactly one
    sees the monitor as due and wins. No lock is held afterwards, so the slow
    HTTP request below runs outside any transaction.
    """
    now = timezone.now()
    qs = Monitor.objects.filter(pk=monitor_id, is_active=True).exclude(under_maintenance(now))
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

    cfg = (
        Monitor.objects.filter(pk=monitor_id)
        .values("url", "http_method", "request_headers", "request_body", "expected_status", "expected_text")
        .first()
    )
    if cfg is None:
        return "skipped"
    status_code, elapsed, is_up, error = perform_http_check(
        cfg["url"],
        method=cfg["http_method"],
        headers=cfg["request_headers"],
        body=cfg["request_body"],
        expected_status=cfg["expected_status"],
        expected_text=cfg["expected_text"],
    )

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
    sub = Subscriber.objects.select_related("status_page", "monitor").filter(pk=subscriber_id).first()
    incident = Incident.objects.select_related("monitor").filter(pk=incident_id).first()
    if sub is None or incident is None or not sub.is_confirmed:
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
    sub = Subscriber.objects.select_related("status_page", "monitor").filter(pk=subscriber_id).first()
    if sub is None or sub.is_confirmed:
        return "skipped"
    title = sub.status_page.title if sub.status_page_id else sub.monitor.public_name
    send_mail(
        f"Підтвердіть підписку: {title}",
        f"Ви (або хтось інший) попросили надсилати сповіщення про збої «{title}» на цю адресу.\n\n"
        f"Підтвердити підписку (діє 3 дні): {subscriptions.confirm_url(sub)}\n\n"
        "Якщо це були не ви — просто проігноруйте цей лист, нічого не буде надіслано.",
        settings.DEFAULT_FROM_EMAIL,
        [sub.email],
    )
    return "sent"


@shared_task
def cleanup_unconfirmed_subscribers():
    """Pending rows whose confirmation link has expired (they can never be confirmed any more)."""
    cutoff = timezone.now() - timedelta(seconds=subscriptions.CONFIRM_MAX_AGE)
    deleted, _ = Subscriber.objects.filter(confirmed_at__isnull=True, created_at__lt=cutoff).delete()
    return deleted


@shared_task
def cleanup_old_checks():
    """Delete in batches so we never hold one giant transaction / table lock.

    The cutoff is hour-aligned: a half-deleted hour could later be "rebuilt" from the rest of its raw rows
    into a wrong rollup, so an hour is either wholly kept or wholly gone.
    """
    cutoff = rollups.floor_hour(timezone.now() - timedelta(days=settings.CHECK_RETENTION_DAYS))
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
    CheckRollupHourly.objects.filter(hour__lt=timezone.now() - timedelta(days=settings.ROLLUP_RETENTION_DAYS)).delete()
    return total


# ----------------------------- SSL certificate expiry -----------------------------
SSL_THRESHOLDS = (14, 3, 0)  # alert once when each is crossed


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


def ssl_level(days_left):
    """Which alert threshold is currently crossed (smallest one), or None."""
    crossed = [t for t in SSL_THRESHOLDS if days_left <= t]
    return min(crossed) if crossed else None


@shared_task
def schedule_ssl_checks():
    ids = list(Monitor.objects.filter(is_active=True, url__istartswith="https://").values_list("pk", flat=True))
    for monitor_id in ids:
        check_ssl.delay(monitor_id)
    return len(ids)


@shared_task
def check_ssl(monitor_id):
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
        Monitor.objects.filter(pk=monitor.pk).update(
            ssl_error="Сертифікат прострочений" if expired else f"Помилка сертифіката: {exc.verify_message}"[:200],
            ssl_expires_at=now if expired else None,
        )
        if expired:
            _maybe_alert(monitor, 0)
        return "invalid"
    except (OSError, ssl.SSLError) as exc:
        Monitor.objects.filter(pk=monitor.pk).update(ssl_error=f"{type(exc).__name__}"[:200])
        return "error"

    days_left = (expires - now).days if expires > now else 0
    Monitor.objects.filter(pk=monitor.pk).update(ssl_expires_at=expires, ssl_error="")
    _maybe_alert(monitor, ssl_level(days_left), days_left)
    return days_left


def _maybe_alert(monitor, level, days_left=0):
    """One mail per threshold: 14d -> 3d -> expired. Renewal resets the state."""
    if level is None:
        if monitor.ssl_alerted_level is not None:
            Monitor.objects.filter(pk=monitor.pk).update(ssl_alerted_level=None)
        return
    if monitor.ssl_alerted_level is None or level < monitor.ssl_alerted_level:
        # conditional update = atomic claim, so two workers can't both send
        claimed = (
            Monitor.objects.filter(pk=monitor.pk)
            .filter(Q(ssl_alerted_level__isnull=True) | Q(ssl_alerted_level__gt=level))
            .update(ssl_alerted_level=level)
        )
        if claimed:
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
