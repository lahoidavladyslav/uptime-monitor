"""Read-side helpers: all numbers are computed by the database."""

from datetime import UTC, datetime, time, timedelta

from django.db.models import Avg, Count, Max, Min, Q
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

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


def daily_bar(monitor, days=7):
    """One query for all days: TruncDate + values + annotate."""
    today = timezone.now().date()
    first_day = today - timedelta(days=days - 1)
    start = datetime.combine(first_day, time.min, tzinfo=UTC)
    rows = (
        monitor.checks.filter(checked_at__gte=start)
        .annotate(day=TruncDate("checked_at"))
        .values("day")
        .annotate(total=Count("id"), up=Count("id", filter=Q(is_up=True)))
        .order_by("day")
    )
    by_day = {r["day"]: r for r in rows}
    bar = []
    for i in range(days):
        day = first_day + timedelta(days=i)
        row = by_day.get(day)
        uptime = uptime_percent(row["total"], row["up"]) if row else None
        bar.append({"day": day, "uptime": uptime, "color": day_color(uptime)})
    return bar


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
