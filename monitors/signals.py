import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Monitor

logger = logging.getLogger(__name__)


def status_cache_key(slug):
    return f"public-status:{slug}"


def _drop(key):
    # A cache outage must never break saving a monitor; worst case the page
    # stays stale until its 60s TTL expires.
    try:
        cache.delete(key)
    except Exception:
        logger.warning("could not invalidate %s (cache unavailable)", key)


@receiver([post_save, post_delete], sender=Monitor)
def invalidate_public_status(sender, instance, **kwargs):
    """Making a monitor private must 404 immediately, not after the 60s TTL."""
    if not instance.public_slug:
        return
    key = status_cache_key(instance.public_slug)
    _drop(key)  # now...
    # ...and again after commit: a request between delete and commit could
    # otherwise re-cache the old (still public) state.
    transaction.on_commit(lambda: _drop(key))
