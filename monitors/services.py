"""Read-side helpers: all numbers are computed by the database."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Avg, Count, Max, Min, Q
from django.db.models.functions import TruncHour
from django.utils import timezone

from . import rollups
from .models import CheckResult, Incident

PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}


def uptime_percent(total, up):
    """None (not 0, not ZeroDivisionError) when there is no data."""
    if not total:
        return None
    return round(up * 100 / total, 2)


def monitor_stats(monitor, period="24h"):
    """24h: straight from the (indexed) raw table. 7d: from hourly rollups + the raw tail (see rollups.py)."""
    now = timezone.now()
    if period == "24h":
        since = now - PERIODS[period]
        agg = monitor.checks.filter(checked_at__gte=since, in_maintenance=False).aggregate(
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
    days = PERIODS[period].days
    a = rollups.aggregate([monitor.pk], rollups.window_start(days, now), now + timedelta(seconds=1), now)[monitor.pk]
    return {
        "total": a.total, "up": a.up, "down": a.down, "uptime": a.uptime,
        "rt_avg": a.avg_ms, "rt_min": a.min_ms, "rt_max": a.max_ms,
    }  # fmt: skip


def day_color(uptime):
    if uptime is None:
        return "gray"
    if uptime < 90:
        return "red"
    if uptime < 99:
        return "yellow"
    return "green"


def daily_bars(monitor_ids, days=7):
    """{monitor_id: [day cells]} for many monitors: two grouped queries (rollup days + raw tail)."""
    today = timezone.now().date()
    first_day = today - timedelta(days=days - 1)
    by_key = rollups.aggregate_by_day(monitor_ids, first_day)
    result = {}
    for mid in monitor_ids:
        bar = []
        for i in range(days):
            day = first_day + timedelta(days=i)
            row = by_key.get((mid, day))
            uptime = row.uptime if row else None
            bar.append({"day": day, "uptime": uptime, "color": day_color(uptime)})
        result[mid] = bar
    return result


def daily_bar(monitor, days=7):
    return daily_bars([monitor.pk], days)[monitor.pk]


def uptime_by_monitor(monitor_ids, period="7d"):
    """{monitor_id: uptime% or None}: rollups for old hours, raw only for the last 1-2 hours."""
    now = timezone.now()
    ids = list(monitor_ids)
    aggs = rollups.aggregate(ids, rollups.window_start(PERIODS[period].days, now), now + timedelta(seconds=1), now)
    return {mid: aggs[mid].uptime for mid in ids}


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
    now = timezone.now()
    if period == "24h":
        qs = monitor.checks.filter(checked_at__gte=now - PERIODS[period])
        rows = qs.order_by("checked_at").values_list("checked_at", "response_time_ms", "is_up")
        return [{"t": t.strftime("%H:%M"), "ms": ms, "down": not up} for t, ms, up in rows]
    start = rollups.window_start(PERIODS[period].days, now)
    return [
        {"t": hour.strftime("%d.%m %H:00"), "ms": avg, "down": failed}
        for hour, avg, failed in rollups.hourly_series(monitor.pk, start, now)
    ]


def long_term_uptime(monitor, days):
    """Uptime over a long window; raw checks only live 7 days, so this is mostly rollup data."""
    return rollups.uptimes([monitor.pk], [days])[days][monitor.pk]


def uptime_windows(monitor, days_list=(7, 30, 90)):
    """{days: uptime%} for several windows in two queries in total."""
    per_window = rollups.uptimes([monitor.pk], list(days_list))
    return {d: per_window[d][monitor.pk] for d in days_list}


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


LAST_CHECK_WINDOW = timedelta(days=2)


def last_is_up_expr(now=None):
    """Result of the monitor's newest check, for `annotate(last_is_up=...)` (NULL = never checked).

    A monitor is normally checked every few minutes, so the newest row is in the last two daily partitions. The first
    subquery looks only there (partition pruning); the unbounded fallback, which has to walk every partition, runs only
    when that finds nothing (a paused or brand-new monitor): COALESCE evaluates it lazily. The answer is identical to
    the old unbounded `ORDER BY checked_at DESC LIMIT 1`, which cost one index probe per partition per monitor.
    """
    from django.db.models import BooleanField, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    now = now or timezone.now()
    base = CheckResult.objects.filter(monitor=OuterRef("pk")).order_by("-checked_at")
    recent = base.filter(checked_at__gte=now - LAST_CHECK_WINDOW)
    return Coalesce(
        Subquery(recent.values("is_up")[:1], output_field=BooleanField()),
        Subquery(base.values("is_up")[:1], output_field=BooleanField()),
        output_field=BooleanField(),
    )


def dashboard_queryset(user, now=None, *, counts=True):
    """All per-monitor numbers for the dashboard in ONE query.

    The 24h counters are correlated subqueries bounded by `checked_at >= since`, so Postgres prunes the daily
    partitions to the last two. (The earlier `Count("checks", filter=...)` was a LEFT JOIN with the time condition in
    the aggregate FILTER: it read every partition, i.e. the whole table.)

    `counts=False` leaves the counters out: list pages fetch them for all monitors at once with `attach_counts_24h`
    (one grouped query instead of two subqueries per row).
    """
    from django.db.models import IntegerField, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    from .models import Monitor
    from .tasks import under_maintenance

    now = now or timezone.now()
    since = now - timedelta(hours=24)
    open_incident = Incident.objects.filter(monitor=OuterRef("pk"), resolved_at__isnull=True)
    # samples taken during maintenance count towards neither side of the uptime ratio
    recent = CheckResult.objects.filter(monitor=OuterRef("pk"), checked_at__gte=since, in_maintenance=False)

    def count(qs):
        return Coalesce(
            Subquery(qs.order_by().values("monitor").annotate(n=Count("pk")).values("n"), output_field=IntegerField()),
            0,
        )

    qs = Monitor.objects.filter(user=user).annotate(
        last_is_up=last_is_up_expr(now),
        down_since=Subquery(open_incident.values("started_at")[:1]),
        in_maintenance=under_maintenance(now),
    )
    if counts:
        qs = qs.annotate(checks_24h=count(recent), up_24h=count(recent.filter(is_up=True)))
    return qs


def attach_counts_24h(monitors, now=None):
    """Set `checks_24h` / `up_24h` on every monitor with ONE grouped query bounded to the last 24 h."""
    monitors = list(monitors)
    now = now or timezone.now()
    rows = (
        CheckResult.objects.filter(
            monitor_id__in=[m.pk for m in monitors], checked_at__gte=now - timedelta(hours=24), in_maintenance=False
        )
        .order_by()
        .values("monitor_id")
        .annotate(total=Count("pk"), up=Count("pk", filter=Q(is_up=True)))
    )
    found = {r["monitor_id"]: (r["total"], r["up"]) for r in rows}
    for m in monitors:
        m.checks_24h, m.up_24h = found.get(m.pk, (0, 0))
    return monitors


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
        CheckResult.objects.filter(monitor_id__in=ids, checked_at__gte=hours[0], in_maintenance=False)
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
    monitors = attach_counts_24h(dashboard_queryset(user, now, counts=False), now)
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
