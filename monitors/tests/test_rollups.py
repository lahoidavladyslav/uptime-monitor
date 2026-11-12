"""H2: hourly rollups. Every test pins one of the pitfalls from the brief."""

import random
from datetime import UTC, datetime, timedelta

import pytest
import time_machine
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Count, Max, Min, Q, Sum
from django.urls import reverse
from django.utils import timezone

from monitors import rollups
from monitors.models import CheckResult, CheckRollupHourly, Monitor
from monitors.services import daily_bars, long_term_uptime, monitor_stats, response_time_series, uptime_by_monitor
from monitors.tasks import build_rollups, cleanup_old_checks
from reports.tasks import build_rows

T0 = datetime(2026, 11, 12, 13, 27, 40, tzinfo=UTC)


def at(dt, **kw):
    return dt + timedelta(**kw)


def check(m, when, up=True, ms=100):
    return CheckResult(monitor=m, checked_at=when, is_up=up, response_time_ms=ms if up else None)


@pytest.fixture
def two(user):
    return (
        Monitor.objects.create(user=user, name="A", url="https://a.example.com"),
        Monitor.objects.create(user=user, name="B", url="https://b.example.com"),
    )


# ---------------- build_rollups ----------------
@time_machine.travel(T0, tick=False)
def test_build_aggregates_counts_sums_min_max_per_monitor_hour(two):
    a, b = two
    h = datetime(2026, 11, 12, 12, tzinfo=UTC)
    CheckResult.objects.bulk_create(
        [
            check(a, at(h, minutes=1), True, 100),
            check(a, at(h, minutes=2), True, 300),
            check(a, at(h, minutes=3), False),  # failed: no timing
            check(a, at(h, minutes=4), True, None),  # up but untimed
            check(b, at(h, minutes=5), True, 50),
        ]
    )
    build_rollups()
    r = CheckRollupHourly.objects.get(monitor=a, hour=h)
    assert (r.checks_total, r.checks_up, r.sum_response_ms) == (4, 3, 400)
    assert r.response_count == 2  # NOT 4: only two checks carry a timing
    assert (r.min_response_ms, r.max_response_ms) == (100, 300)
    assert CheckRollupHourly.objects.get(monitor=b, hour=h).sum_response_ms == 50


@time_machine.travel(T0, tick=False)
def test_hour_without_any_timing_has_null_min_max_not_zero(two):
    a, _ = two
    CheckResult.objects.create(monitor=a, checked_at=at(T0, minutes=-5), is_up=False)
    build_rollups()
    r = CheckRollupHourly.objects.get()
    assert (r.sum_response_ms, r.response_count, r.min_response_ms, r.max_response_ms) == (0, 0, None, None)


@time_machine.travel(T0, tick=False)
def test_build_is_idempotent_and_recomputes_instead_of_appending(two):
    a, _ = two
    CheckResult.objects.create(monitor=a, checked_at=at(T0, minutes=-1), is_up=True, response_time_ms=10)
    build_rollups()
    build_rollups()
    assert CheckRollupHourly.objects.get().checks_total == 1
    CheckResult.objects.create(monitor=a, checked_at=at(T0, minutes=-2), is_up=True, response_time_ms=30)
    build_rollups()
    r = CheckRollupHourly.objects.get()
    assert (r.checks_total, r.sum_response_ms) == (2, 40)


def test_a_late_result_lands_in_its_own_hour_not_the_current_one(two):
    a, _ = two
    with time_machine.travel(datetime(2026, 11, 12, 12, 58, tzinfo=UTC), tick=False):
        CheckResult.objects.create(
            monitor=a, checked_at=datetime(2026, 11, 12, 12, 30, tzinfo=UTC), is_up=True, response_time_ms=5
        )
        build_rollups()
    # the worker was stuck: this result (checked at 12:40) is saved after the clock moved to 13:04
    with time_machine.travel(datetime(2026, 11, 12, 13, 4, tzinfo=UTC), tick=False):
        CheckResult.objects.create(monitor=a, checked_at=datetime(2026, 11, 12, 12, 40, tzinfo=UTC), is_up=False)
        build_rollups()
    h12 = CheckRollupHourly.objects.get(hour=datetime(2026, 11, 12, 12, tzinfo=UTC))
    assert (h12.checks_total, h12.checks_up) == (2, 1)
    assert not CheckRollupHourly.objects.filter(hour=datetime(2026, 11, 12, 13, tzinfo=UTC)).exists()


@time_machine.travel(T0, tick=False)
def test_regular_run_recomputes_only_the_previous_and_current_hour(two):
    a, _ = two
    old = datetime(2026, 11, 12, 9, 10, tzinfo=UTC)
    CheckResult.objects.create(monitor=a, checked_at=old, is_up=True, response_time_ms=1)
    CheckRollupHourly.objects.create(monitor=a, hour=old.replace(minute=0), checks_total=77, checks_up=77)
    build_rollups()
    assert CheckRollupHourly.objects.get(monitor=a, hour=old.replace(minute=0)).checks_total == 77  # untouched


def test_task_is_scheduled_every_five_minutes(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["build-rollups"]
    assert entry["task"] == "monitors.tasks.build_rollups"
    assert entry["schedule"]._orig_minute == "*/5"


def test_hours_are_cut_in_utc_even_with_a_half_hour_timezone(two, settings):
    a, _ = two
    settings.TIME_ZONE = "Asia/Kolkata"  # +05:30 would shift ORM TruncHour buckets by half an hour
    with timezone.override("Asia/Kolkata"), time_machine.travel(T0, tick=False):
        CheckResult.objects.create(monitor=a, checked_at=datetime(2026, 11, 12, 12, 45, tzinfo=UTC), is_up=True)
        build_rollups()
    assert CheckRollupHourly.objects.get().hour == datetime(2026, 11, 12, 12, tzinfo=UTC)


# ---------------- reading: rollup == raw ----------------
def seed(monitors, now, days=10, seed_value=7):
    rng = random.Random(seed_value)
    rows = []
    for m in monitors:
        t = now - timedelta(days=days)
        while t < now:
            up = rng.random() > 0.15
            rows.append(check(m, t, up, rng.randint(40, 900) if up else None))
            t += timedelta(minutes=rng.choice([5, 7, 11]))
    CheckResult.objects.bulk_create(rows, batch_size=2000)


def raw_aggregate(m, start, end):
    qs = CheckResult.objects.filter(monitor=m, checked_at__gte=start, checked_at__lt=end)
    return qs.aggregate(
        total=Count("id"), up=Count("id", filter=Q(is_up=True)), s=Sum("response_time_ms"),
        n=Count("response_time_ms"), lo=Min("response_time_ms"), hi=Max("response_time_ms"),
    )  # fmt: skip


NOWS = [
    datetime(2026, 11, 12, 0, 3, 10, tzinfo=UTC),  # just after midnight: previous hour not yet "final" by wall clock
    datetime(2026, 11, 12, 13, 27, 40, tzinfo=UTC),
    datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC),
]


@pytest.mark.parametrize("now", NOWS)
def test_rollup_and_raw_agree_for_every_reader(two, now):
    with time_machine.travel(now, tick=False):
        seed(two, now)
        call_command("backfill_rollups")
        a = two[0]
        start = rollups.floor_hour(now) - timedelta(days=7)
        expected = raw_aggregate(a, start, now + timedelta(seconds=1))

        stats = monitor_stats(a, "7d")
        assert (stats["total"], stats["up"]) == (expected["total"], expected["up"])
        assert (stats["rt_min"], stats["rt_max"]) == (expected["lo"], expected["hi"])
        assert stats["rt_avg"] == round(expected["s"] / expected["n"])  # weighted by timed checks
        assert uptime_by_monitor([a.pk], "7d")[a.pk] == round(expected["up"] * 100 / expected["total"], 2)

        first_day = now.date() - timedelta(days=6)
        bars = daily_bars([a.pk], 7)[a.pk]
        for bar in bars:
            d0 = datetime.combine(bar["day"], datetime.min.time(), tzinfo=UTC)
            day = raw_aggregate(a, d0, min(d0 + timedelta(days=1), now + timedelta(seconds=1)))
            expected_day = round(day["up"] * 100 / day["total"], 2) if day["total"] else None  # empty day: no data
            assert bar["uptime"] == expected_day, bar["day"]
        assert bars[0]["day"] == first_day

        pts = response_time_series(a, "7d")
        timed = [p for p in pts if p["ms"] is not None]
        assert timed and len(pts) <= 170

        d30 = raw_aggregate(a, rollups.floor_hour(now) - timedelta(days=30), now + timedelta(seconds=1))
        assert long_term_uptime(a, 30) == round(d30["up"] * 100 / d30["total"], 2)


@time_machine.travel(T0, tick=False)
def test_old_hours_are_read_from_the_rollup_not_from_raw(two):
    a, _ = two
    seed(two, T0, days=9)
    call_command("backfill_rollups")
    before = monitor_stats(a, "7d")
    boundary = rollups.reader_boundary(T0)
    CheckResult.objects.filter(checked_at__lt=boundary).delete()  # raw gone, rollup stays
    assert monitor_stats(a, "7d") == before
    # and a doctored rollup row is visible, i.e. the rollup really is the source for that hour
    row = CheckRollupHourly.objects.filter(monitor=a, hour__gte=T0 - timedelta(days=6), hour__lt=boundary).first()
    CheckRollupHourly.objects.filter(pk=row.pk).update(checks_total=row.checks_total + 1000)
    assert monitor_stats(a, "7d")["total"] == before["total"] + 1000


@time_machine.travel(T0, tick=False)
def test_the_current_partial_hour_comes_from_raw_immediately(two):
    a, _ = two
    build_rollups()
    CheckResult.objects.create(monitor=a, checked_at=at(T0, minutes=-3), is_up=False)  # not rolled up yet
    stats = monitor_stats(a, "7d")
    assert (stats["total"], stats["down"]) == (1, 1)


@time_machine.travel(T0, tick=False)
def test_average_is_weighted_not_an_average_of_hourly_averages(two):
    a, _ = two
    h1, h2 = datetime(2026, 11, 12, 8, tzinfo=UTC), datetime(2026, 11, 12, 9, tzinfo=UTC)
    CheckResult.objects.bulk_create(
        [check(a, at(h1, minutes=i), True, 100) for i in range(1, 10)] + [check(a, at(h2, minutes=1), True, 1000)]
    )
    call_command("backfill_rollups")
    # hourly averages are 100 and 1000 (mean 550); the true mean over 10 checks is 190
    assert monitor_stats(a, "7d")["rt_avg"] == 190


@time_machine.travel(T0, tick=False)
def test_no_data_is_none_not_a_division_by_zero(two):
    a, _ = two
    stats = monitor_stats(a, "7d")
    assert stats == {"total": 0, "up": 0, "down": 0, "uptime": None, "rt_avg": None, "rt_min": None, "rt_max": None}
    assert uptime_by_monitor([a.pk], "7d") == {a.pk: None}
    assert long_term_uptime(a, 90) is None


@time_machine.travel(T0, tick=False)
def test_reports_use_the_same_numbers(user, two):
    a, _ = two
    seed(two, T0, days=9)
    call_command("backfill_rollups")
    start_day = (T0 - timedelta(days=5)).date()
    end_day = (T0 - timedelta(days=1)).date()
    rows = {r[0]: r for r in build_rows(user, start_day, end_day)}
    lo = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)
    hi = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    exp = raw_aggregate(a, lo, hi)
    assert rows["A"][2] == exp["total"] and rows["A"][3] == exp["total"] - exp["up"]
    assert rows["A"][5] == round(exp["s"] / exp["n"])


@time_machine.travel(T0, tick=False)
def test_a_report_can_cover_days_whose_raw_rows_are_long_gone(user, two):
    a, _ = two
    old = datetime(2026, 10, 1, 10, tzinfo=UTC)
    CheckRollupHourly.objects.create(
        monitor=a, hour=old, checks_total=10, checks_up=9, sum_response_ms=900, response_count=9
    )
    rows = {r[0]: r for r in build_rows(user, old.date(), old.date())}
    assert (rows["A"][2], rows["A"][3], rows["A"][4], rows["A"][5]) == (10, 1, 90.0, 100)


# ---------------- retention ----------------
@time_machine.travel(T0, tick=False)
def test_raw_lives_seven_days_cut_on_an_hour_boundary_and_rollups_a_year(two, settings):
    a, _ = two
    assert settings.CHECK_RETENTION_DAYS == 7 and settings.ROLLUP_RETENTION_DAYS == 365
    cutoff = rollups.floor_hour(T0 - timedelta(days=7))
    keep_edge = check(a, at(cutoff, minutes=1))  # inside the cutoff hour: kept, so no half-hour is ever left
    drop = check(a, at(cutoff, minutes=-1))
    CheckResult.objects.bulk_create([keep_edge, drop])
    CheckRollupHourly.objects.create(monitor=a, hour=T0 - timedelta(days=366), checks_total=1, checks_up=1)
    fresh = CheckRollupHourly.objects.create(monitor=a, hour=T0 - timedelta(days=200), checks_total=1, checks_up=1)
    cleanup_old_checks()
    assert list(CheckResult.objects.values_list("checked_at", flat=True)) == [at(cutoff, minutes=1)]
    assert list(CheckRollupHourly.objects.all()) == [fresh]


# ---------------- backfill ----------------
@time_machine.travel(T0, tick=False)
def test_backfill_overwrites_a_corrupt_rollup_but_leaves_hours_without_raw_alone(two):
    a, _ = two
    seed([a], T0, days=3)
    call_command("backfill_rollups")
    good = list(CheckRollupHourly.objects.order_by("hour").values_list("hour", "checks_total"))
    victim = CheckRollupHourly.objects.order_by("hour")[5]
    CheckRollupHourly.objects.filter(pk=victim.pk).update(checks_total=999999)
    orphan_hour = T0 - timedelta(days=40)
    CheckRollupHourly.objects.create(monitor=a, hour=orphan_hour, checks_total=42, checks_up=42)  # raw is gone
    call_command(
        "backfill_rollups", "--from", (T0 - timedelta(days=4)).date().isoformat(), "--to", T0.date().isoformat()
    )
    after = list(
        CheckRollupHourly.objects.filter(hour__gte=T0 - timedelta(days=4))
        .order_by("hour")
        .values_list("hour", "checks_total")
    )
    assert after == good  # rebuilt from raw
    assert CheckRollupHourly.objects.get(hour=orphan_hour).checks_total == 42  # no raw: not wiped


@time_machine.travel(T0, tick=False)
def test_backfill_works_in_day_batches(two, django_assert_max_num_queries):
    seed(two, T0, days=5)
    with django_assert_max_num_queries(20):  # one statement per day (+ a few lookups), not per row
        call_command("backfill_rollups")


def test_backfill_rejects_bad_ranges(db):
    with pytest.raises(CommandError):
        call_command("backfill_rollups", "--from", "2026-11-10", "--to", "2026-11-01")
    with pytest.raises(CommandError):
        call_command("backfill_rollups", "--from", "not-a-date")


# ---------------- pages still render ----------------
@time_machine.travel(T0, tick=False)
def test_monitor_page_and_api_show_rollup_backed_numbers(auth_client, user):
    m = Monitor.objects.create(user=user, name="Z", url="https://z.example.com")
    seed([m], T0, days=9)
    call_command("backfill_rollups")
    assert auth_client.get(reverse("monitor_detail", args=[m.pk])).status_code == 200
    r = auth_client.get(f"/api/v1/monitors/{m.pk}/stats?period=7d").json()
    assert r["total"] > 1000 and r["rt_min"] is not None


def test_a_reader_just_after_the_hour_never_sees_a_half_finished_rollup_row(two):
    """12:58 build saw 1 check of hour 12; a 2nd arrives at 12:59; at 13:02 nobody has rebuilt hour 12 yet.
    The reader must still count both: that is why hour H-1 is read raw, not from the rollup."""
    a, _ = two
    with time_machine.travel(datetime(2026, 11, 12, 12, 58, tzinfo=UTC), tick=False):
        CheckResult.objects.create(monitor=a, checked_at=datetime(2026, 11, 12, 12, 50, tzinfo=UTC), is_up=True)
        build_rollups()
    CheckResult.objects.create(monitor=a, checked_at=datetime(2026, 11, 12, 12, 59, tzinfo=UTC), is_up=False)
    assert CheckRollupHourly.objects.get().checks_total == 1  # the rollup row is stale...
    with time_machine.travel(datetime(2026, 11, 12, 13, 2, tzinfo=UTC), tick=False):
        stats = monitor_stats(a, "7d")
    assert (stats["total"], stats["down"]) == (2, 1)  # ...but the number the user sees is not
