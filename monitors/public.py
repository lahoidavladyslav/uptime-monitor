"""Public, login-free pages: single-monitor status, grouped status pages, RSS feeds.

Privacy rules (enforced by what is passed to the templates, and covered by tests):
never expose the monitored URL, error texts, headers, or anything about the owner.
"""

from django.conf import settings
from django.contrib.syndication.views import Feed
from django.core.cache import cache
from django.db.models import OuterRef, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.feedgenerator import Atom1Feed
from django.views.decorators.cache import cache_control

from . import maintenance
from .models import CheckResult, Monitor, StatusPage
from .services import daily_bars, public_incident_history, uptime_by_monitor
from .signals import group_cache_key, status_cache_key


def _annotated(queryset):
    now = timezone.now()
    latest = CheckResult.objects.filter(monitor=OuterRef("pk")).order_by("-checked_at")
    return queryset.annotate(
        last_is_up=Subquery(latest.values("is_up")[:1]),
        in_maintenance=maintenance.under_maintenance_expr(now),
    )


def _cards(monitors):
    """Everything per-monitor for the page, from a constant number of queries."""
    ids = [m.pk for m in monitors]
    bars = daily_bars(ids, 7)
    uptimes = uptime_by_monitor(ids, "7d")
    return [
        {
            "name": m.public_name,
            "logo": m.public_logo_url,
            "is_up": m.last_is_up,
            "maintenance": m.in_maintenance,
            "uptime": uptimes[m.pk],
            "bar": bars[m.pk],
        }
        for m in monitors
    ]


def _overall(cards):
    if not cards or all(c["is_up"] is None for c in cards):
        return "unknown"
    if any(c["maintenance"] for c in cards):
        return "maintenance"
    downs = [c for c in cards if c["is_up"] is False]
    if not downs:
        return "ok"
    return "down" if len(downs) == len(cards) else "partial"


def _cached(key, build):
    html = cache.get(key)
    if html is None:
        html = build()
        cache.set(key, html, settings.PUBLIC_STATUS_CACHE_SECONDS)
    return html


def monitor_status(request, slug):
    """Cached for 60s; 404s are never cached. Invalidated on save/delete (signals.py)."""
    key = status_cache_key(slug)

    def build():
        monitor = _annotated(Monitor.objects.filter(public_slug=slug, is_public=True)).first()
        if monitor is None:
            raise Http404
        cards = _cards([monitor])
        return render_to_string(
            "monitors/status.html",
            {
                "title": monitor.public_name,
                "logo": monitor.public_logo_url,
                "cards": cards,
                "overall": _overall(cards),
                "single": cards[0],
                "history": public_incident_history([monitor.pk]),
                "feed_url": reverse("monitor_feed", args=[slug]),
                "subscribe_url": reverse("monitor_subscribe", args=[slug]),
            },
            request,
        )

    return HttpResponse(_cached(key, build))


def group_status(request, slug):
    key = group_cache_key(slug)

    def build():
        page = StatusPage.objects.filter(slug=slug, is_public=True).first()
        if page is None:
            raise Http404
        monitors = list(_annotated(page.monitors.all()).order_by("name", "id"))
        cards = _cards(monitors)
        return render_to_string(
            "monitors/status_group.html",
            {
                "title": page.title,
                "description": page.description,
                "logo": page.logo_url,
                "cards": cards,
                "overall": _overall(cards),
                "history": public_incident_history([m.pk for m in monitors]),
                "feed_url": reverse("group_feed", args=[slug]),
                "subscribe_url": reverse("group_subscribe", args=[slug]),
            },
            request,
        )

    return HttpResponse(_cached(key, build))


# ---------------------------- feeds ----------------------------
class IncidentFeed(Feed):
    """One entry per incident *state*: opening and resolution get distinct guids,
    so readers show a new entry when an outage ends."""

    feed_type = Atom1Feed

    def title(self, obj):
        return f"{obj['title']} — інциденти"

    def link(self, obj):
        return obj["link"]

    def description(self, obj):
        return f"Історія недоступності {obj['title']}"

    def items(self, obj):
        entries = []
        for inc in obj["history"]:
            entries.append({**inc, "state": "open", "link": obj["link"]})
            if inc["resolved_at"]:
                entries.append({**inc, "state": "resolved", "link": obj["link"]})
        entries.sort(key=lambda e: e["resolved_at"] if e["state"] == "resolved" else e["started_at"], reverse=True)
        return entries

    def item_title(self, item):
        if item["state"] == "resolved":
            return f"🟢 {item['name']} — відновлено"
        return f"🔴 {item['name']} — недоступний"

    def item_description(self, item):
        if item["state"] == "resolved":
            return f"Робота відновлена. Тривалість простою: {item['minutes']} хв."
        return "Виявлено недоступність."

    def item_guid(self, item):
        return f"incident-{item['id']}-{item['state']}"

    item_guid_is_permalink = False

    def item_pubdate(self, item):
        return item["resolved_at"] if item["state"] == "resolved" else item["started_at"]

    def item_link(self, item):
        return item["link"]


class MonitorFeed(IncidentFeed):
    def get_object(self, request, slug):
        monitor = get_object_or_404(Monitor, public_slug=slug, is_public=True)
        return {
            "title": monitor.public_name,
            "link": request.build_absolute_uri(reverse("public_status", args=[slug])),
            "history": public_incident_history([monitor.pk]),
        }


class GroupFeed(IncidentFeed):
    def get_object(self, request, slug):
        page = get_object_or_404(StatusPage, slug=slug, is_public=True)
        return {
            "title": page.title,
            "link": request.build_absolute_uri(reverse("group_status", args=[slug])),
            "history": public_incident_history(list(page.monitors.values_list("pk", flat=True))),
        }


monitor_feed = cache_control(public=True, max_age=60)(MonitorFeed())
group_feed = cache_control(public=True, max_age=60)(GroupFeed())
