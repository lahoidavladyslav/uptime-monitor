"""Hourly rollups: how they are written (`build`) and the single path every long-window reader uses.

Reading rule. For a window [start, end) the rollup answers hours < T and the raw table answers [T, end),
with T = floor_hour(now) - 1h. Why the previous hour is read raw too: `build_rollups` recomputes the
previous and current hour every 5 minutes, so hour H-1 becomes final only after the first run following
the boundary; reading it raw for up to two hours means a reader never sees a half-finished rollup row.
Everything is UTC and hour-aligned (`AT TIME ZONE 'UTC'`), independent of settings.TIME_ZONE.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from django.db import connection
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

from .models import CheckResult, CheckRollupHourly

ONE_HOUR = timedelta(hours=1)


def floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def reader_boundary(now: datetime | None = None) -> datetime:
    return floor_hour(now or timezone.now()) - ONE_HOUR


def window_start(days: int, now: datetime | None = None) -> datetime:
    """Start of an N-day window: hour-aligned, so rollup and raw readers cover exactly the same span."""
    return floor_hour(now or timezone.now()) - timedelta(days=days)


# ----------------------------- writing -----------------------------
_UPSERT = """
INSERT INTO {table} (monitor_id, hour, checks_total, checks_up, sum_response_ms, response_count,
                     min_response_ms, max_response_ms)
SELECT monitor_id,
       date_trunc('hour', checked_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
       count(*),
       count(*) FILTER (WHERE is_up),
       COALESCE(sum(response_time_ms), 0),
       count(response_time_ms),
       min(response_time_ms),
       max(response_time_ms)
FROM {raw}
WHERE checked_at >= %s AND checked_at < %s
GROUP BY 1, 2
ON CONFLICT (monitor_id, hour) DO UPDATE SET
    checks_total = EXCLUDED.checks_total, checks_up = EXCLUDED.checks_up,
    sum_response_ms = EXCLUDED.sum_response_ms, response_count = EXCLUDED.response_count,
    min_response_ms = EXCLUDED.min_response_ms, max_response_ms = EXCLUDED.max_response_ms
"""


def build(start: datetime, end: datetime) -> int:
    """(Re)compute rollup rows for raw checks in [start, end) with ONE INSERT ... ON CONFLICT DO UPDATE.

    Idempotent and set-based: the data never leaves the database. Only hours that still have raw rows are
    written, so rebuilding a range whose raw data was already cleaned up cannot wipe good rollups.
    `start` should be hour-aligned (a half hour would produce a partial row for that hour).
    """
    sql = _UPSERT.format(table=CheckRollupHourly._meta.db_table, raw=CheckResult._meta.db_table)
    with connection.cursor() as cur:
        cur.execute(sql, [start, end])
        return cur.rowcount


# ----------------------------- reading -----------------------------
@dataclass
class Agg:
    total: int = 0
    up: int = 0
    sum_ms: int = 0
    timed: int = 0
    min_ms: int | None = None
    max_ms: int | None = None

    @property
    def down(self):
        return self.total - self.up

    @property
    def uptime(self):
        return round(self.up * 100 / self.total, 2) if self.total else None

    @property
    def avg_ms(self):
        return round(self.sum_ms / self.timed) if self.timed else None

    def add(self, total, up, sum_ms, timed, lo, hi):
        self.total += total or 0
        self.up += up or 0
        self.sum_ms += sum_ms or 0
        self.timed += timed or 0
        if lo is not None:
            self.min_ms = lo if self.min_ms is None else min(self.min_ms, lo)
        if hi is not None:
            self.max_ms = hi if self.max_ms is None else max(self.max_ms, hi)


def _split(start, end, now):
    """(rollup_end, raw_start): the rollup serves [start, rollup_end), the raw table [raw_start, end)."""
    boundary = reader_boundary(now)
    return min(end, boundary), max(start, boundary)


def aggregate(monitor_ids, start: datetime, end: datetime, now: datetime | None = None) -> dict[int, Agg]:
    """{monitor_id: Agg} over [start, end): two grouped queries at most, however many monitors."""
    ids = list(monitor_ids)
    result = {mid: Agg() for mid in ids}
    if not ids:
        return result
    rollup_end, raw_start = _split(start, end, now or timezone.now())
    if start < rollup_end:
        rows = (
            CheckRollupHourly.objects.filter(monitor_id__in=ids, hour__gte=start, hour__lt=rollup_end)
            .values("monitor_id")
            .annotate(
                t=Sum("checks_total"), u=Sum("checks_up"), s=Sum("sum_response_ms"), n=Sum("response_count"),
                lo=Min("min_response_ms"), hi=Max("max_response_ms"),
            )
        )  # fmt: skip
        for r in rows:
            result[r["monitor_id"]].add(r["t"], r["u"], r["s"], r["n"], r["lo"], r["hi"])
    if raw_start < end:
        rows = (
            CheckResult.objects.filter(monitor_id__in=ids, checked_at__gte=raw_start, checked_at__lt=end)
            .values("monitor_id")
            .annotate(
                t=Count("id"), u=Count("id", filter=Q(is_up=True)), s=Sum("response_time_ms"),
                n=Count("response_time_ms"), lo=Min("response_time_ms"), hi=Max("response_time_ms"),
            )
        )  # fmt: skip
        for r in rows:
            result[r["monitor_id"]].add(r["t"], r["u"], r["s"], r["n"], r["lo"], r["hi"])
    return result


def aggregate_by_day(monitor_ids, first_day, now: datetime | None = None) -> dict[tuple[int, object], Agg]:
    """{(monitor_id, date): Agg} for every UTC day from `first_day` until now."""
    now = now or timezone.now()
    ids = list(monitor_ids)
    start = datetime.combine(first_day, time.min, tzinfo=UTC)
    out: dict[tuple[int, object], Agg] = {}
    rollup_end, raw_start = _split(start, now + timedelta(seconds=1), now)
    if ids and start < rollup_end:
        rows = (
            CheckRollupHourly.objects.filter(monitor_id__in=ids, hour__gte=start, hour__lt=rollup_end)
            .annotate(day=TruncDate("hour", tzinfo=UTC))
            .values("monitor_id", "day")
            .annotate(t=Sum("checks_total"), u=Sum("checks_up"))
        )
        for r in rows:
            out.setdefault((r["monitor_id"], r["day"]), Agg()).add(r["t"], r["u"], 0, 0, None, None)
    if ids:
        rows = (
            CheckResult.objects.filter(monitor_id__in=ids, checked_at__gte=raw_start)
            .annotate(day=TruncDate("checked_at", tzinfo=UTC))
            .values("monitor_id", "day")
            .annotate(t=Count("id"), u=Count("id", filter=Q(is_up=True)))
        )
        for r in rows:
            out.setdefault((r["monitor_id"], r["day"]), Agg()).add(r["t"], r["u"], 0, 0, None, None)
    return out


def hourly_series(monitor_id, start: datetime, now: datetime | None = None):
    """[(hour, avg_ms | None, failed_any)] oldest first, for the 7-day chart."""
    now = now or timezone.now()
    rollup_end, raw_start = _split(start, now + timedelta(seconds=1), now)
    points: dict[datetime, Agg] = {}
    for r in CheckRollupHourly.objects.filter(monitor_id=monitor_id, hour__gte=start, hour__lt=rollup_end):
        points.setdefault(r.hour, Agg()).add(
            r.checks_total, r.checks_up, r.sum_response_ms, r.response_count, r.min_response_ms, r.max_response_ms
        )
    raw = (
        CheckResult.objects.filter(monitor_id=monitor_id, checked_at__gte=raw_start)
        .annotate(hour=TruncHour("checked_at", tzinfo=UTC))
        .values("hour")
        .annotate(
            t=Count("id"), u=Count("id", filter=Q(is_up=True)), s=Sum("response_time_ms"), n=Count("response_time_ms")
        )
    )
    for r in raw:
        points.setdefault(r["hour"], Agg()).add(r["t"], r["u"], r["s"], r["n"], None, None)
    return [(h, a.avg_ms, a.down > 0) for h, a in sorted(points.items())]


def uptimes(monitor_ids, days_list, now: datetime | None = None) -> dict[int, dict[int, float | None]]:
    """{days: {monitor_id: uptime%}} for several window lengths with ONE rollup query + ONE raw query
    (conditional sums), instead of two queries per window."""
    now = now or timezone.now()
    ids = list(monitor_ids)
    boundary = reader_boundary(now)
    starts = {d: window_start(d, now) for d in days_list}
    totals = {d: {mid: [0, 0] for mid in ids} for d in days_list}
    if ids:
        agg = {}
        for d, start in starts.items():
            agg[f"t{d}"] = Sum("checks_total", filter=Q(hour__gte=start))
            agg[f"u{d}"] = Sum("checks_up", filter=Q(hour__gte=start))
        rows = (
            CheckRollupHourly.objects.filter(monitor_id__in=ids, hour__gte=min(starts.values()), hour__lt=boundary)
            .values("monitor_id")
            .annotate(**agg)
        )
        for r in rows:
            for d in days_list:
                totals[d][r["monitor_id"]][0] += r[f"t{d}"] or 0
                totals[d][r["monitor_id"]][1] += r[f"u{d}"] or 0
        raw_start = max(boundary, min(starts.values()))
        for r in (
            CheckResult.objects.filter(monitor_id__in=ids, checked_at__gte=raw_start)
            .values("monitor_id")
            .annotate(t=Count("id"), u=Count("id", filter=Q(is_up=True)))
        ):
            for d in days_list:  # every window starts before the boundary, so the raw tail is common to all
                totals[d][r["monitor_id"]][0] += r["t"]
                totals[d][r["monitor_id"]][1] += r["u"]
    return {
        d: {mid: (round(u * 100 / t, 2) if t else None) for mid, (t, u) in per.items()} for d, per in totals.items()
    }
