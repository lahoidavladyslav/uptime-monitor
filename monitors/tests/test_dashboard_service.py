from datetime import UTC, datetime, timedelta

import time_machine
from django.utils import timezone

from monitors.models import CheckResult, Incident, MaintenanceWindow, Monitor
from monitors.services import dashboard_rows, sparklines, summarize

NOW = datetime(2026, 3, 10, 12, 30, tzinfo=UTC)


def check(monitor, when, ms=100, up=True):
    return CheckResult.objects.create(monitor=monitor, checked_at=when, is_up=up, response_time_ms=ms if up else None)


@time_machine.travel(NOW, tick=False)
def test_sparklines_are_24_hourly_buckets_from_one_query(user, django_assert_num_queries):
    a = Monitor.objects.create(user=user, name="a", url="https://a.com")
    b = Monitor.objects.create(user=user, name="b", url="https://b.com")
    c = Monitor.objects.create(user=user, name="c", url="https://c.com")
    check(a, NOW.replace(hour=11, minute=10), ms=100)
    check(a, NOW.replace(hour=11, minute=40), ms=300)  # same hour -> average 200
    check(a, NOW.replace(hour=12, minute=5), ms=50)
    check(a, NOW - timedelta(hours=30), ms=999)  # outside the window
    check(b, NOW.replace(hour=12, minute=2), up=False)  # failed check: no timing -> gap
    with django_assert_num_queries(1):
        result = sparklines([a.pk, b.pk, c.pk], NOW)
    assert set(result) == {a.pk, b.pk, c.pk}
    assert all(len(v) == 24 for v in result.values())
    assert result[a.pk][23] == 50 and result[a.pk][22] == 200  # oldest -> newest, last = current hour
    assert [x for x in result[a.pk][:22] if x is not None] == []
    assert result[b.pk] == [None] * 24 and result[c.pk] == [None] * 24


def test_sparklines_without_ids_does_no_query(db, django_assert_num_queries):
    with django_assert_num_queries(0):
        assert sparklines([]) == {}


@time_machine.travel(NOW, tick=False)
def test_status_precedence(user):
    def mk(name, **kw):
        return Monitor.objects.create(user=user, name=name, url=f"https://{name}.com", **kw)

    up, down, paused, maint = mk("up"), mk("down"), mk("paused", is_active=False), mk("maint")
    mk("fresh")  # never checked
    check(up, NOW - timedelta(minutes=1))
    check(down, NOW - timedelta(minutes=1), up=False)
    check(paused, NOW - timedelta(minutes=1), up=False)
    check(maint, NOW - timedelta(minutes=1), up=False)
    MaintenanceWindow.objects.create(
        monitor=maint, starts_at=NOW - timedelta(minutes=5), ends_at=NOW + timedelta(hours=1)
    )
    Incident.objects.create(monitor=down, started_at=NOW - timedelta(minutes=42))

    by_name = {r.name: r for r in dashboard_rows(user, NOW)}
    assert {n: r.status for n, r in by_name.items()} == {
        "up": "up", "down": "down", "fresh": "unknown", "paused": "paused", "maint": "maintenance",
    }  # fmt: skip
    assert by_name["down"].down_minutes == 42 and by_name["up"].down_minutes is None
    assert by_name["up"].uptime_24h == 100.0 and by_name["down"].uptime_24h == 0.0


@time_machine.travel(NOW, tick=False)
def test_monitor_without_checks_has_null_uptime_and_empty_spark(user):
    Monitor.objects.create(user=user, name="new", url="https://new.com")
    (row,) = dashboard_rows(user, NOW)
    assert row.uptime_24h is None and row.last_checked_at is None
    assert row.spark == [None] * 24 and row.status == "unknown"


def test_new_user_has_no_rows_and_zero_summary(user):
    assert dashboard_rows(user) == []
    assert summarize([]) == {"up": 0, "down": 0, "unknown": 0, "maintenance": 0, "paused": 0, "open_incidents": 0}


@time_machine.travel(NOW, tick=False)
def test_summary_counts_statuses_and_open_incidents(user):
    a = Monitor.objects.create(user=user, name="a", url="https://a.com")
    b = Monitor.objects.create(user=user, name="b", url="https://b.com")
    Monitor.objects.create(user=user, name="c", url="https://c.com")
    check(a, NOW)
    check(b, NOW, up=False)
    Incident.objects.create(monitor=b, started_at=NOW - timedelta(minutes=3))
    assert summarize(dashboard_rows(user, NOW)) == {
        "up": 1, "down": 1, "unknown": 1, "maintenance": 0, "paused": 0, "open_incidents": 1,
    }  # fmt: skip


def test_rows_are_scoped_to_the_user(user, other_user):
    Monitor.objects.create(user=other_user, name="theirs", url="https://t.com")
    assert dashboard_rows(user) == []


def test_default_now_is_used_when_omitted(user):
    m = Monitor.objects.create(user=user, name="m", url="https://m.com")
    check(m, timezone.now())
    assert dashboard_rows(user)[0].uptime_24h == 100.0
