"""The dashboard optimisations must keep the numbers identical and the query count constant (docs/perf.md)."""

from datetime import UTC, datetime, timedelta

import time_machine

from monitors.models import CheckResult, Monitor
from monitors.services import (
    attach_counts_24h,
    dashboard_queryset,
    dashboard_rows,
    last_is_up_expr,
)

NOW = datetime(2026, 3, 10, 12, 30, tzinfo=UTC)


def check(monitor, when, up=True, in_maintenance=False):
    return CheckResult.objects.create(
        monitor=monitor, checked_at=when, is_up=up, response_time_ms=10 if up else None, in_maintenance=in_maintenance
    )


def make_data(user):
    """Rows inside and outside the 24h window, failures and maintenance samples, plus a never-checked monitor."""
    a = Monitor.objects.create(user=user, name="a", url="https://a.com")
    b = Monitor.objects.create(user=user, name="b", url="https://b.com")
    Monitor.objects.create(user=user, name="never", url="https://n.com")
    for h in (1, 2, 3):
        check(a, NOW - timedelta(hours=h))  # 3 up
    check(a, NOW - timedelta(hours=4), up=False)  # 1 down
    check(a, NOW - timedelta(hours=5), in_maintenance=True)  # counts for neither side
    check(a, NOW - timedelta(hours=30))  # outside the window
    check(b, NOW - timedelta(hours=1), up=False)
    return a, b


@time_machine.travel(NOW, tick=False)
def test_counters_ignore_old_and_maintenance_rows(user):
    a, b = make_data(user)
    by_name = {m.name: m for m in dashboard_queryset(user, NOW)}
    assert (by_name["a"].checks_24h, by_name["a"].up_24h) == (4, 3)
    assert (by_name["b"].checks_24h, by_name["b"].up_24h) == (1, 0)
    assert (by_name["never"].checks_24h, by_name["never"].up_24h) == (0, 0)  # NULL subquery -> 0, not None


@time_machine.travel(NOW, tick=False)
def test_list_page_counters_equal_the_per_row_counters(user):
    make_data(user)
    per_row = {m.pk: (m.checks_24h, m.up_24h) for m in dashboard_queryset(user, NOW)}
    grouped = attach_counts_24h(dashboard_queryset(user, NOW, counts=False), NOW)
    assert {m.pk: (m.checks_24h, m.up_24h) for m in grouped} == per_row


@time_machine.travel(NOW, tick=False)
def test_last_is_up_is_the_newest_check_even_when_it_is_old(user):
    a = Monitor.objects.create(user=user, name="recent", url="https://a.com")
    paused = Monitor.objects.create(user=user, name="paused", url="https://p.com")
    Monitor.objects.create(user=user, name="never", url="https://n.com")
    check(a, NOW - timedelta(days=5), up=False)  # older, must lose against the newer one
    check(a, NOW - timedelta(minutes=5), up=True)
    check(paused, NOW - timedelta(days=5), up=False)  # outside the fast window: the fallback must still find it
    check(paused, NOW - timedelta(days=9), up=True)
    got = {m.name: m.last_is_up for m in Monitor.objects.filter(user=user).annotate(last_is_up=last_is_up_expr(NOW))}
    assert got == {"recent": True, "paused": False, "never": None}


@time_machine.travel(NOW, tick=False)
def test_every_check_subquery_is_bounded_in_time(user):
    """The partition pruning is the whole point: no `checked_at` bound in a subquery = every partition is read.
    last_is_up (fast path) + checks_24h + up_24h => three bounded subqueries."""
    sql = str(dashboard_queryset(user, NOW).query)
    assert sql.count('"checked_at" >=') >= 3


@time_machine.travel(NOW, tick=False)
def test_dashboard_query_count_does_not_grow_with_monitors(user, django_assert_max_num_queries):
    make_data(user)
    with django_assert_max_num_queries(3):  # monitors + counters + sparklines
        dashboard_rows(user, NOW)
    for i in range(20):
        check(Monitor.objects.create(user=user, name=f"m{i}", url=f"https://m{i}.com"), NOW - timedelta(minutes=i))
    with django_assert_max_num_queries(3):
        assert len(dashboard_rows(user, NOW)) == 23
