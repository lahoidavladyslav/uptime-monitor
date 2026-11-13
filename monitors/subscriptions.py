"""Email subscriptions to public status pages: double opt-in, one-click unsubscribe.

Nothing here reveals whether an address is already known: callers always answer the same way.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import StatusPage, StatusSubscriber

logger = logging.getLogger("subscriptions")

CONFIRM_SALT = "subscription-confirm"
CONFIRM_MAX_AGE = 48 * 3600  # the confirmation link lives 48 hours


def absolute(path: str) -> str:
    return f"{settings.SITE_URL}{path}"


def confirm_token_for(subscriber) -> str:
    return signing.dumps(subscriber.pk, salt=CONFIRM_SALT)


def confirm_url(subscriber) -> str:
    return absolute(reverse("subscription_confirm", args=[confirm_token_for(subscriber)]))


def unsubscribe_url(subscriber) -> str:
    return absolute(reverse("unsubscribe", args=[subscriber.unsub_token]))


def rate_limited(ip: str | None) -> bool:
    """Counts every attempt (valid or not) per client IP per hour. Fails open if the cache is down."""
    key = f"subscribe-rate:{ip or 'unknown'}"
    try:
        cache.add(key, 0, 3600)
        return cache.incr(key) > settings.SUBSCRIBE_LIMIT_PER_HOUR
    except Exception:
        logger.warning("subscribe rate limit unavailable (cache down)")
        return False


def request_subscription(target, email: str) -> None:
    """Create/refresh a pending subscription and queue its confirmation mail. Silent no-op when the
    address is already active, was mailed a few minutes ago, or the target is full.

    An address that had unsubscribed starts over: the row is reset to "pending" (old consent is not reused)."""
    from .tasks import send_subscription_confirmation

    email = email.strip().lower()
    field = "status_page" if isinstance(target, StatusPage) else "monitor"
    existing = StatusSubscriber.objects.filter(**{field: target})
    sub = existing.filter(email__iexact=email).first()
    if sub is None:
        if existing.filter(unsubscribed_at__isnull=True).count() >= settings.MAX_SUBSCRIBERS_PER_TARGET:
            logger.warning("subscriber cap reached", extra={"event": "subscription_cap", field: target.pk})
            return
        sub = StatusSubscriber.objects.create(**{field: target}, email=email)
    elif sub.unsubscribed_at is not None:
        StatusSubscriber.objects.filter(pk=sub.pk).update(
            unsubscribed_at=None, confirmed_at=None, confirmation_sent_at=None
        )
        sub.refresh_from_db()
    elif sub.is_confirmed:
        return
    cooldown = timedelta(minutes=settings.SUBSCRIBE_RESEND_COOLDOWN_MINUTES)
    if sub.confirmation_sent_at and timezone.now() - sub.confirmation_sent_at < cooldown:
        return
    StatusSubscriber.objects.filter(pk=sub.pk).update(confirmation_sent_at=timezone.now())
    logger.info("confirmation requested", extra={"event": "subscription_requested", "subscriber_id": sub.pk})
    send_subscription_confirmation.delay(sub.pk)


def confirm(token: str):
    """The confirmed StatusSubscriber, or None for a bad/expired/orphaned/unsubscribed token. Idempotent."""
    try:
        pk = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
    except signing.BadSignature:
        return None
    sub = StatusSubscriber.objects.filter(pk=pk, unsubscribed_at__isnull=True).first()
    if sub is None:
        return None
    if not sub.is_confirmed:
        sub.confirmed_at = timezone.now()
        sub.save(update_fields=["confirmed_at"])
        logger.info("subscription confirmed", extra={"event": "subscription_confirmed", "subscriber_id": sub.pk})
    return sub


def unsubscribe(token: str) -> None:
    """Soft opt-out by capability token: stamp `unsubscribed_at` once (idempotent, atomic). Unknown tokens are ignored
    (same answer either way)."""
    sub = StatusSubscriber.objects.filter(unsub_token=token, unsubscribed_at__isnull=True).first()
    if sub is not None and StatusSubscriber.objects.filter(pk=sub.pk, unsubscribed_at__isnull=True).update(
        unsubscribed_at=timezone.now()
    ):
        logger.info("unsubscribed", extra={"event": "unsubscribed", "subscriber_id": sub.pk})


def recipients_for(incident):
    """Confirmed subscribers of the incident's monitor and of every public page that lists it,
    one per address (a person subscribed twice gets one mail)."""
    monitor = incident.monitor
    wanted = Q(status_page__is_public=True, status_page__monitors=monitor)
    if monitor.is_public:
        wanted |= Q(monitor=monitor)
    seen, result = set(), []
    for sub in (
        StatusSubscriber.objects.filter(wanted, confirmed_at__isnull=False, unsubscribed_at__isnull=True)
        .select_related("status_page", "monitor")
        .order_by("id")
        .distinct()
    ):
        if sub.email not in seen:
            seen.add(sub.email)
            result.append(sub)
    return result
