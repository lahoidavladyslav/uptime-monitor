"""O3: seed_volume builds a realistic data set fast, in SQL, and reproducibly."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import Sum
from django.utils import timezone

from monitors import partitions
from monitors.models import CheckResult, CheckRollupHourly, Incident, Monitor

pytestmark = pytest.mark.django_db

ARGS = ["--monitors", "6", "--checks", "1200", "--incidents", "12", "--days", "3"]


def seed(*extra):
    out = StringIO()
    call_command("seed_volume", *ARGS, *extra, stdout=out, stderr=StringIO())
    return out.getvalue()


def test_it_creates_exactly_the_requested_volume():
    seed()
    perf = Monitor.objects.filter(name__startswith="perf-")
    assert perf.count() == 6
    assert CheckResult.objects.filter(monitor__in=perf).count() == 1200
    assert Incident.objects.filter(monitor__in=perf).count() == 12


def test_checks_spread_over_the_whole_period_and_over_every_monitor():
    seed()
    first, last = (
        CheckResult.objects.order_by("checked_at").values_list("checked_at", flat=True)[0],
        CheckResult.objects.order_by("-checked_at").values_list("checked_at", flat=True)[0],
    )
    assert timezone.now() - first <= timedelta(days=3, hours=1) and last <= timezone.now()
    assert (last - first) > timedelta(days=2, hours=12)
    per_monitor = CheckResult.objects.values("monitor").annotate(n=Sum(1)).values_list("n", flat=True)
    assert set(per_monitor) == {200}  # 1200 / 6


def test_a_check_is_down_exactly_when_it_falls_inside_an_incident():
    seed()
    windows = {}
    for inc in Incident.objects.filter(monitor__name__startswith="perf-"):
        windows.setdefault(inc.monitor_id, []).append((inc.started_at, inc.resolved_at))
    bad = 0
    for c in CheckResult.objects.filter(monitor__name__startswith="perf-").iterator():
        inside = any(s <= c.checked_at < e for s, e in windows.get(c.monitor_id, []))
        bad += inside == c.is_up  # a down check outside, or an up check inside
    assert bad == 0
    assert CheckResult.objects.filter(is_up=False).exists()  # the data is not trivially all green


def test_down_checks_carry_an_error_and_no_timing_and_up_checks_the_opposite():
    seed()
    down = CheckResult.objects.filter(is_up=False).first()
    assert down.response_time_ms is None and down.error and down.details["type"] == "http_status"
    up = CheckResult.objects.filter(is_up=True).first()
    assert up.response_time_ms and up.error is None


def test_rollups_are_built_and_agree_with_the_raw_rows():
    seed()
    raw = CheckResult.objects.filter(monitor__name__startswith="perf-").count()
    assert CheckRollupHourly.objects.aggregate(t=Sum("checks_total"))["t"] == raw


def test_nothing_lands_in_the_default_partition():
    seed()
    assert partitions.default_rows() == 0  # the day partitions were created first


def test_the_same_seed_gives_the_same_data():
    seed("--seed", "0.7")
    a = list(CheckResult.objects.order_by("monitor_id", "checked_at").values_list("is_up", "response_time_ms")[:300])
    call_command("seed_volume", "--clean", stdout=StringIO())
    seed("--seed", "0.7")
    b = list(CheckResult.objects.order_by("monitor_id", "checked_at").values_list("is_up", "response_time_ms")[:300])
    assert a == b


def test_running_twice_refuses_instead_of_doubling_the_volume():
    seed()
    with pytest.raises(CommandError, match="--clean"):
        seed()
    assert CheckResult.objects.count() == 1200


def test_clean_removes_everything_it_created_and_nothing_else(user):
    mine = Monitor.objects.create(user=user, name="real", url="https://real.example.com")
    CheckResult.objects.create(monitor=mine, checked_at=timezone.now(), is_up=True)
    seed()
    call_command("seed_volume", "--clean", stdout=StringIO())
    assert list(Monitor.objects.all()) == [mine] and CheckResult.objects.count() == 1
    assert not Incident.objects.exists() and not CheckRollupHourly.objects.exclude(monitor=mine).exists()


def test_it_warns_when_retention_would_drop_the_data(settings):
    settings.CHECK_RETENTION_DAYS = 1
    out = StringIO()
    err = StringIO()
    call_command("seed_volume", *ARGS, stdout=out, stderr=err)
    assert "CHECK_RETENTION_DAYS" in err.getvalue()


def test_the_data_is_inserted_in_sql_not_one_orm_row_at_a_time(django_assert_max_num_queries):
    with django_assert_max_num_queries(80):  # a few statements per day + setup, however many rows
        seed()


def test_it_is_fast_enough_for_the_real_volume_to_take_minutes_not_hours():
    import time

    start = time.monotonic()
    call_command(
        "seed_volume",
        "--monitors",
        "20",
        "--checks",
        "60000",
        "--incidents",
        "60",
        "--days",
        "10",
        stdout=StringIO(),
        stderr=StringIO(),
    )
    elapsed = time.monotonic() - start
    assert CheckResult.objects.count() == 60000 and elapsed < 20, f"{60000 / elapsed:.0f} rows/s"
    assert connection.queries_log is not None
