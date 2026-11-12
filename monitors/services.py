"""Read-side helpers: all numbers are computed by the database."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from django.db.models import Avg, Count, Max, Min, Q
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

from .models import CheckResult, Incident

PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}


def uptime_percent(total, up):
    """None (not 0, not ZeroDivisionError) when there is no data."""
    if not total:
        return None
    return round(up * 100 / total, 2)


def monitor_stats(monitor, period="24h"):
    since = timezone.now() - PERIODS[period]
    agg = monitor.checks.filter(checked_at__gte=since).aggregate(
        total=Count("id"),
        up=Count("id", filter=Q(is_up=True)),
        down=Count("id", filter=Q(is_up=False)),
        rt_avg=Avg("response_time_ms"),
        rt_min=Min("response_time_ms"),
        rt_max=Max("response_time_ms"),
    )
    agg["uptime"] = uptime_percent(agg["total"], agg["up"])
    if agg["rt_avg"] is not None:
        agg["rt_avg"] = round(agg["rt_avg"])
    return agg


def day_color(uptime):
    if uptime is None:
        return "gray"
    if uptime < 90:
        return "red"
    if uptime < 99:
        return "yellow"
    return "green"


def daily_bars(monitor_ids, days=7):
    """{monitor_id: [day cells]} for many monitors with ONE query (TruncDate + values + annotate)."""
    today = timezone.now().date()
    first_day = today - timedelta(days=days - 1)
    start = datetime.combine(first_day, time.min, tzinfo=UTC)
    rows = (
        CheckResult.objects.filter(monitor_id__in=monitor_ids, checked_at__gte=start)
        .annotate(day=TruncDate("checked_at"))
        .values("monitor_id", "day")
        .annotate(total=Count("id"), up=Count("id", filter=Q(is_up=True)))
    )
    by_key = {(r["monitor_id"], r["day"]): r for r in rows}
    result = {}
    for mid in monitor_ids:
        bar = []
        for i in range(days):
            day = first_day + timedelta(days=i)
            row = by_key.get((mid, day))
            uptime = uptime_percent(row["total"], row["up"]) if row else None
            bar.append({"day": day, "uptime": uptime, "color": day_color(uptime)})
        result[mid] = bar
    return result


def daily_bar(monitor, days=7):
    return daily_bars([monitor.pk], days)[monitor.pk]


def uptime_by_monitor(monitor_ids, period="7d"):
    """{monitor_id: uptime% or None} in one grouped query."""
    since = timezone.now() - PERIODS[period]
    rows = (
        CheckResult.objects.filter(monitor_id__in=monitor_ids, checked_at__gte=since)
        .values("monitor_id")
        .annotate(total=Count("id"), up=Count("id", filter=Q(is_up=True)))
    )
    found = {r["monitor_id"]: uptime_percent(r["total"], r["up"]) for r in rows}
    return {mid: found.get(mid) for mid in monitor_ids}


def public_incident_history(monitor_ids, days=30, limit=30):
    """Incidents for the public page. Deliberately WITHOUT error text or URLs."""
    since = timezone.now() - timedelta(days=days)
    items = []
    for inc in (
        Incident.objects.filter(monitor_id__in=monitor_ids, started_at__gte=since)
        .select_related("monitor")
        .order_by("-started_at")[:limit]
    ):
        end = inc.resolved_at or timezone.now()
        items.append(
            {
                "id": inc.pk,
                "name": inc.monitor.public_name,
                "started_at": inc.started_at,
                "resolved_at": inc.resolved_at,
                "minutes": int((end - inc.started_at).total_seconds() // 60),
            }
        )
    return items


def response_time_series(monitor, period="24h"):
    """Points for the chart. Bounded size no matter how many checks exist.

    24h: the raw checks (<= 1440 at a 1-minute interval).
    7d:  hourly buckets aggregated in SQL (<= 168 points) instead of ~10k raw rows.
    `ms` is None for a failed check/bucket without any timing -> a gap in the line;
    `down` marks points where something failed.
    """
    since = timezone.now() - PERIODS[period]
    qs = monitor.checks.filter(checked_at__gte=since)
    if period == "24h":
        rows = qs.order_by("checked_at").values_list("checked_at", "response_time_ms", "is_up")
        return [{"t": t.strftime("%H:%M"), "ms": ms, "down": not up} for t, ms, up in rows]
    rows = (
        qs.annotate(hour=TruncHour("checked_at"))
        .values("hour")
        .annotate(avg_ms=Avg("response_time_ms"), failed=Count("id", filter=Q(is_up=False)))
        .order_by("hour")
    )
    return [
        {
            "t": r["hour"].strftime("%d.%m %H:00"),
            "ms": round(r["avg_ms"]) if r["avg_ms"] is not None else None,
            "down": r["failed"] > 0,
        }
        for r in rows
    ]


def long_term_uptime(monitor, days):
    """Uptime over a long window from hourly rollups (raw checks are kept only 30 days)."""
    from django.db.models import Sum

    since = timezone.now() - timedelta(days=days)
    agg = monitor.hourly_stats.filter(hour__gte=since).aggregate(total=Sum("total"), up=Sum("up"))
    return uptime_percent(agg["total"] or 0, agg["up"] or 0)


# ----------------------------- dashboard (shared by the Django page and the JSON API) -----------------------------
@dataclass(frozen=True)
class DashboardRow:
    id: int
    name: str
    status: str  # up | down | unknown | maintenance | paused
    last_checked_at: datetime | None
    uptime_24h: float | None
    down_minutes: int | None
    spark: list[int | None]


def dashboard_queryset(user, now=None):
    """All per-monitor numbers for the dashboard in ONE query (Subquery + Count(filter=...))."""
    from django.db.models import OuterRef, Subquery

    from .models import Monitor
    from .tasks import under_maintenance

    now = now or timezone.now()
    since = now - timedelta(hours=24)
    latest = CheckResult.objects.filter(monitor=OuterRef("pk")).order_by("-checked_at")
    open_incident = Incident.objects.filter(monitor=OuterRef("pk"), resolved_at__isnull=True)
    return Monitor.objects.filter(user=user).annotate(
        last_is_up=Subquery(latest.values("is_up")[:1]),
        down_since=Subquery(open_incident.values("started_at")[:1]),
        in_maintenance=under_maintenance(now),
        checks_24h=Count("checks", filter=Q(checks__checked_at__gte=since)),
        up_24h=Count("checks", filter=Q(checks__checked_at__gte=since, checks__is_up=True)),
    )


def sparklines(monitor_ids, now=None):
    """{monitor_id: 24 hourly average response times (ms), oldest -> newest, None = no timing}.

    One grouped query for all monitors; the last value is the current (partial) hour.
    """
    ids = list(monitor_ids)
    if not ids:
        return {}
    now = now or timezone.now()
    last_hour = now.replace(minute=0, second=0, microsecond=0)
    hours = [last_hour - timedelta(hours=23 - i) for i in range(24)]
    rows = (
        CheckResult.objects.filter(monitor_id__in=ids, checked_at__gte=hours[0])
        .annotate(hour=TruncHour("checked_at"))
        .values("monitor_id", "hour")
        .annotate(avg_ms=Avg("response_time_ms"))
    )
    found = {(r["monitor_id"], r["hour"]): r["avg_ms"] for r in rows}
    result = {}
    for mid in ids:
        series = []
        for h in hours:
            avg = found.get((mid, h))
            series.append(None if avg is None else round(avg))
        result[mid] = series
    return result


def monitor_status(m):
    if not m.is_active:
        return "paused"
    if m.in_maintenance:
        return "maintenance"
    if m.last_is_up is None:
        return "unknown"
    return "up" if m.last_is_up else "down"


def dashboard_rows(user, now=None):
    now = now or timezone.now()
    monitors = list(dashboard_queryset(user, now))
    sparks = sparklines([m.pk for m in monitors], now)
    return [
        DashboardRow(
            id=m.pk,
            name=m.name,
            status=monitor_status(m),
            last_checked_at=m.last_checked_at,
            uptime_24h=uptime_percent(m.checks_24h, m.up_24h),
            down_minutes=int((now - m.down_since).total_seconds() // 60) if m.down_since else None,
            spark=sparks[m.pk],
        )
        for m in monitors
    ]


def summarize(rows):
    counts = {"up": 0, "down": 0, "unknown": 0, "maintenance": 0, "paused": 0}
    for r in rows:
        counts[r.status] += 1
    counts["open_incidents"] = sum(1 for r in rows if r.down_minutes is not None)
    return counts


def ssl_days_left(monitor):
    if monitor.ssl_expires_at is None:
        return None
    return max((monitor.ssl_expires_at - timezone.now()).days, 0)


def cooldown_ok(key, seconds):
    """cache.add is atomic: only the first caller within `seconds` gets True.

    If the cache is down we fail open (better to allow a click than to 500).
    """
    from django.core.cache import cache

    try:
        return cache.add(key, 1, seconds)
    except Exception:
        return True
