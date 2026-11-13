"""H3: CheckResult is range-partitioned by day. Needs a real PostgreSQL (the whole suite already runs on one)."""

from datetime import UTC, date, datetime, timedelta

import pytest
import time_machine
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from monitors import partitions, rollups
from monitors.models import CheckResult, CheckRollupHourly, Monitor
from monitors.tasks import manage_partitions

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 12, 13, 27, 40, tzinfo=UTC)
TODAY = NOW.date()


def q(sql, *params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def where_is(pk_id):
    """Name of the partition holding the row with this id."""
    return q("SELECT tableoid::regclass::text FROM monitors_checkresult WHERE id = %s", pk_id)[0][0]


def part(day):
    return f"monitors_checkresult_p{day:%Y%m%d}"


def settle():
    """The suite runs in ONE transaction, so the rows it inserted still have deferred FK checks pending, which
    would block a DROP. In production the housekeeping task runs after those rows were committed."""
    q("SET CONSTRAINTS ALL IMMEDIATE")


def exists(name):
    return q("SELECT to_regclass(%s) IS NOT NULL", name)[0][0]


def at(day, hour=12):
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Сайт", url="https://example.com")


def add(mon, when, up=True, ms=10):
    return CheckResult.objects.create(monitor=mon, checked_at=when, is_up=up, response_time_ms=ms if up else None)


# ---------------- shape ----------------
def test_the_table_is_range_partitioned_on_checked_at():
    row = q(
        "SELECT partstrat, pg_get_partkeydef(partrelid) FROM pg_partitioned_table "
        "WHERE partrelid = 'monitors_checkresult'::regclass"
    )
    assert row == [("r", "RANGE (checked_at)")]


def test_the_primary_key_includes_the_partition_key():
    cols = q(
        """SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
           WHERE i.indrelid = 'monitors_checkresult'::regclass AND i.indisprimary ORDER BY a.attnum"""
    )
    assert [c[0] for c in cols] == ["id", "checked_at"]
    assert CheckResult._meta.pk.__class__.__name__ == "CompositePrimaryKey"


def test_a_default_partition_exists_as_a_safety_net():
    assert exists(partitions.DEFAULT)


# ---------------- the ORM still works ----------------
def test_orm_roundtrip_with_generated_ids_and_composite_pk(mon):
    a = add(mon, at(TODAY))
    b = add(mon, at(TODAY, 13))
    assert a.id and b.id and b.id > a.id  # ids come from one sequence, not one per partition
    assert CheckResult.objects.get(pk=a.pk).id == a.id
    a.error = "changed"
    a.save()
    assert CheckResult.objects.get(pk=a.pk).error == "changed"
    CheckResult.objects.bulk_create(
        [CheckResult(monitor=mon, checked_at=at(TODAY - timedelta(days=i)), is_up=True) for i in range(5)]
    )
    assert CheckResult.objects.count() == 7
    b.delete()
    assert CheckResult.objects.count() == 6


def test_deleting_a_monitor_removes_its_checks_from_every_partition(mon, user):
    other = Monitor.objects.create(user=user, name="B", url="https://b.example.com")
    for i in range(3):
        add(mon, at(TODAY - timedelta(days=i)))
    keep = add(other, at(TODAY))
    mon.delete()
    assert list(CheckResult.objects.values_list("pk", flat=True)) == [keep.pk]


def test_the_ordering_and_filtering_used_by_the_pages_still_work(mon):
    for h in (9, 10, 11):
        add(mon, at(TODAY, h))
    assert [c.checked_at.hour for c in mon.checks.all()] == [11, 10, 9]
    assert mon.checks.filter(checked_at__gte=at(TODAY, 10)).count() == 2


# ---------------- manage_partitions ----------------
def drop_all_daily_partitions():
    """The test database was migrated on the real 'today', which has nothing to do with the frozen NOW."""
    for name in partitions.daily_partitions().values():
        q(f"DROP TABLE {name}")


@time_machine.travel(NOW, tick=False)
def test_partitions_are_created_for_today_and_three_days_ahead_and_it_is_idempotent():
    drop_all_daily_partitions()  # started failing the day after the test DB was built (it assumed "today" == NOW)
    manage_partitions()
    for i in range(4):
        assert exists(part(TODAY + timedelta(days=i)))
    assert not exists(part(TODAY + timedelta(days=4)))
    before = q("SELECT count(*) FROM pg_inherits WHERE inhparent = 'monitors_checkresult'::regclass")
    manage_partitions()
    assert q("SELECT count(*) FROM pg_inherits WHERE inhparent = 'monitors_checkresult'::regclass") == before


@time_machine.travel(NOW, tick=False)
def test_each_row_lands_in_its_own_days_partition(mon):
    manage_partitions()
    today, tomorrow = add(mon, at(TODAY, 23)), add(mon, at(TODAY + timedelta(days=1), 0))
    assert where_is(today.id) == part(TODAY) and where_is(tomorrow.id) == part(TODAY + timedelta(days=1))
    edge = add(mon, datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=UTC))  # midnight belongs to the NEW day
    assert where_is(edge.id) == part(TODAY)


@time_machine.travel(NOW, tick=False)
def test_a_row_for_a_day_without_a_partition_lands_in_default_instead_of_failing(mon):
    manage_partitions()
    stray = add(mon, at(TODAY + timedelta(days=30)))
    assert where_is(stray.id) == partitions.DEFAULT
    assert partitions.default_rows() == 1


@time_machine.travel(NOW, tick=False)
def test_the_next_run_adopts_default_rows_into_a_proper_partition(mon):
    day = TODAY - timedelta(days=2)  # inside retention, and nobody has created its partition
    assert not exists(part(day))
    stray = [add(mon, at(day, h)) for h in (4, 5)]
    assert all(where_is(r.id) == partitions.DEFAULT for r in stray)
    manage_partitions()  # CREATE ... PARTITION OF would fail here (default holds rows of that day): adoption handles it
    assert all(where_is(r.id) == part(day) for r in stray)
    assert partitions.default_rows() == 0
    assert CheckResult.objects.count() == 2  # nothing lost, nothing duplicated
    assert {r.id for r in stray} == set(CheckResult.objects.values_list("id", flat=True))


@time_machine.travel(NOW, tick=False)
def test_a_bogus_far_future_or_far_past_timestamp_cannot_mint_partitions(mon):
    manage_partitions()
    far = [add(mon, at(date(2099, 1, 1))), add(mon, at(date(2001, 1, 1)))]
    settle()
    manage_partitions()
    created = q("SELECT relname FROM pg_class WHERE relname ~ '^monitors_checkresult_p(2099|2001)'")
    assert created == []
    assert partitions.default_rows() >= 1  # the 2099 row stays visible in the safety net (and in /metrics)
    assert not CheckResult.objects.filter(
        pk__in=[f.pk for f in far if f.checked_at.year == 2001]
    ).exists()  # older than retention: purged


@time_machine.travel(NOW, tick=False)
def test_partitions_beyond_retention_are_dropped_but_not_the_cutoff_day(mon, settings):
    settings.CHECK_RETENTION_DAYS = 7
    cutoff_day = rollups.floor_hour(NOW - timedelta(days=7)).date()  # 2026-11-05
    old_day, edge_day = cutoff_day - timedelta(days=3), cutoff_day
    partitions.ensure_partition(old_day)
    partitions.ensure_partition(edge_day)
    add(mon, at(old_day, 5))
    add(mon, at(edge_day, 20))
    settle()
    manage_partitions()
    assert not exists(part(old_day))  # DROP TABLE, not a DELETE of its rows
    assert exists(part(edge_day))  # still inside the window (the cutoff falls in the middle of that day)
    assert CheckResult.objects.count() == 1


@time_machine.travel(NOW, tick=False)
def test_a_partition_is_rolled_up_before_it_is_dropped(mon, settings):
    settings.CHECK_RETENTION_DAYS = 7
    old_day = TODAY - timedelta(days=12)
    partitions.ensure_partition(old_day)
    for m in range(5):
        add(mon, at(old_day, 6) + timedelta(minutes=m), up=m != 0)
    assert not CheckRollupHourly.objects.exists()  # the periodic job never ran for that day
    settle()
    manage_partitions()
    assert not exists(part(old_day))
    r = CheckRollupHourly.objects.get()  # ...so the drop did it, and the history survived
    assert (r.checks_total, r.checks_up) == (5, 4)


@time_machine.travel(NOW, tick=False)
def test_rollups_older_than_a_year_are_pruned_by_the_same_task(mon):
    CheckRollupHourly.objects.create(monitor=mon, hour=NOW - timedelta(days=400), checks_total=1, checks_up=1)
    fresh = CheckRollupHourly.objects.create(monitor=mon, hour=NOW - timedelta(days=100), checks_total=1, checks_up=1)
    manage_partitions()
    assert list(CheckRollupHourly.objects.all()) == [fresh]


def test_the_task_is_scheduled_and_the_batched_cleanup_is_gone(settings):
    assert settings.CELERY_BEAT_SCHEDULE["manage-partitions"]["task"] == "monitors.tasks.manage_partitions"
    assert "cleanup-old-checks" not in settings.CELERY_BEAT_SCHEDULE
    from monitors import tasks

    assert not hasattr(tasks, "cleanup_old_checks")


# ---------------- monitoring ----------------
def test_metrics_expose_the_default_partition_so_it_can_be_alerted_on(client, mon, settings):
    settings.METRICS_TOKEN = "t"
    r = client.get("/metrics/", headers={"Authorization": "Bearer t"})
    assert "uptime_checkresult_default_partition_rows 0" in r.content.decode()
    add(mon, at(date(2099, 1, 1)))
    r = client.get("/metrics/", headers={"Authorization": "Bearer t"})
    assert "uptime_checkresult_default_partition_rows 1" in r.content.decode()


def test_manage_partitions_warns_when_the_default_partition_is_not_empty(mon, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="monitors.partitions")
    add(mon, at(date(2099, 1, 1)))
    manage_partitions()
    assert any("default partition" in r.getMessage() for r in caplog.records)


# ---------------- partition pruning ----------------
def plan(sql, *params):
    return "\n".join(r[0] for r in q("EXPLAIN " + sql, *params))


@time_machine.travel(NOW, tick=False)
def test_a_time_range_query_only_touches_the_partitions_of_that_range(mon):
    manage_partitions()
    for i in range(4):
        partitions.ensure_partition(TODAY - timedelta(days=i))
    text = plan(
        "SELECT count(*) FROM monitors_checkresult WHERE monitor_id = %s AND checked_at >= %s AND checked_at < %s",
        mon.pk,
        at(TODAY - timedelta(days=1), 0),
        at(TODAY, 0),
    )
    assert part(TODAY - timedelta(days=1)) in text
    for other in (TODAY, TODAY - timedelta(days=2), TODAY - timedelta(days=3)):
        assert part(other) not in text, f"{part(other)} was scanned: no partition pruning\n{text}"


# ---------------- migration ----------------
@pytest.mark.django_db(transaction=True)
def test_the_migration_reverses_and_goes_forward_again_without_losing_rows(user):
    mon = Monitor.objects.create(user=user, name="M", url="https://m.example.com")
    when = timezone.now() - timedelta(days=2)
    made = [add(mon, when + timedelta(minutes=i), up=i % 2 == 0) for i in range(6)]
    ids = sorted(c.id for c in made)
    try:
        call_command("migrate", "monitors", "0009", verbosity=0)
        assert q("SELECT relkind FROM pg_class WHERE relname = 'monitors_checkresult'") == [("r",)]  # plain table again
        assert sorted(r[0] for r in q("SELECT id FROM monitors_checkresult")) == ids
        newer = q(
            "INSERT INTO monitors_checkresult (monitor_id, checked_at, is_up, region, in_maintenance) VALUES (%s, now(), true, 'eu', false) RETURNING id",
            mon.pk,
        )[0][0]
        assert newer > max(ids)  # the identity sequence continued from where it was
    finally:
        call_command("migrate", "monitors", verbosity=0)
    assert q("SELECT relkind FROM pg_class WHERE relname = 'monitors_checkresult'") == [("p",)]
    assert sorted(CheckResult.objects.values_list("id", flat=True)) == sorted(ids + [newer])
    assert exists(partitions.DEFAULT)
