import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete
from django.dispatch import receiver

from .models import MaintenanceWindow, Monitor, StatusPage

logger = logging.getLogger(__name__)


def status_cache_key(slug):
    return f"public-status:{slug}"


def group_cache_key(slug):
    return f"public-group:{slug}"


def _drop(keys):
    # A cache outage must never break saving; worst case a page stays stale until its 60s TTL.
    for key in keys:
        try:
            cache.delete(key)
        except Exception:
            logger.warning("could not invalidate %s (cache unavailable)", key)


def _invalidate(keys):
    keys = [k for k in keys if k]
    if not keys:
        return
    _drop(keys)  # now...
    # ...and again after commit: a request between delete and commit could otherwise
    # re-cache the old (still public) state.
    transaction.on_commit(lambda: _drop(keys))


def _monitor_keys(monitor):
    keys = []
    if monitor.public_slug:
        keys.append(status_cache_key(monitor.public_slug))
    keys += [group_cache_key(s) for s in StatusPage.objects.filter(monitors=monitor).values_list("slug", flat=True)]
    return keys


@receiver(post_save, sender=Monitor)
def monitor_saved(sender, instance, **kwargs):
    """Making a monitor private (or renaming it) must show up immediately, not after the TTL."""
    _invalidate(_monitor_keys(instance))


@receiver(pre_delete, sender=Monitor)
def monitor_deleting(sender, instance, **kwargs):
    # pre_delete: the M2M rows (needed to find the groups) are still there.
    _invalidate(_monitor_keys(instance))


@receiver(post_save, sender=MaintenanceWindow)
@receiver(post_delete, sender=MaintenanceWindow)
def maintenance_changed(sender, instance, **kwargs):
    # a user-wide window (monitor is NULL) changes the status of every public page of that user
    monitors = [instance.monitor] if instance.monitor_id else Monitor.objects.filter(user_id=instance.user_id)
    keys = []
    for monitor in monitors:
        keys += _monitor_keys(monitor)
    _invalidate(keys)


@receiver(post_save, sender=StatusPage)
@receiver(post_delete, sender=StatusPage)
def page_changed(sender, instance, **kwargs):
    _invalidate([group_cache_key(instance.slug)])


@receiver(m2m_changed, sender=StatusPage.monitors.through)
def page_members_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if not action.startswith(("post_", "pre_clear")):
        return
    if reverse:  # monitor.status_pages.add(...)
        slugs = StatusPage.objects.filter(pk__in=pk_set or []).values_list("slug", flat=True)
        _invalidate([group_cache_key(s) for s in slugs])
    else:
        _invalidate([group_cache_key(instance.slug)])
