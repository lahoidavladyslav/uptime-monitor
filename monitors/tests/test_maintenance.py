"""H6: maintenance windows (recurring, DST-safe, user-wide) and what they mean for checks, incidents, uptime."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import time_machine
from django.core.management import call_command
from django.utils import timezone

from monitors import maintenance, rollups
from monitors.models import CheckResult, Incident, MaintenanceWindow, Monitor
from monitors.services import dashboard_rows, monitor_stats, uptime_by_monitor
from monitors.tasks import check_monitor, due_monitors, expand_maintenance, record_check
from monitors.tests.helpers import outcome
from reports.tasks import build_rows

KYIV = "Europe/Kyiv"


def utc(*a):
    return datetime(*a, tzinfo=UTC)


@pytest.fixture
def mon(user):
    m = Monitor.objects.create(user=user, name="Сайт", url="https://example.com")
    return m


def window(user, start, end, monitor=None, **kw):
    return MaintenanceWindow.objects.create(user=user, monitor=monitor, starts_at=start, ends_at=end, **kw)


def weekly_sunday_2am_kyiv(user, monitor=None):
    """Every Sunday 02:00-03:00 Kyiv time; the template's own first occurrence is Sunday 1 March 2026 (winter)."""
    return window(user, utc(2026, 3, 1, 0), utc(2026, 3, 1, 1), monitor, rrule="FREQ=WEEKLY;BYDAY=SU", timezone=KYIV)


def spans(template):
    return [(w.starts_at, w.ends_at) for w in MaintenanceWindow.objects.filter(parent=template).order_by("starts_at")]


# ---------------- recurrence and DST ----------------
def test_weekly_window_follows_local_time_across_the_spring_dst_change(user):
    t = weekly_sunday_2am_kyiv(user)
    expand_maintenance(now=utc(2026, 3, 20, 12), horizon_days=30)
    got = dict(spans(t))
    assert got[utc(2026, 3, 22, 0)] == utc(2026, 3, 22, 1)  # winter: 02:00 EET = 00:00 UTC
    assert got[utc(2026, 3, 29, 0)] == utc(2026, 3, 29, 1)  # the night of the change: 03:00 does not exist
    assert got[utc(2026, 4, 4, 23)] == utc(2026, 4, 5, 0)  # summer: 02:00 EEST = 23:00 UTC the evening before
    assert utc(2026, 4, 5, 0) not in got  # i.e. it did NOT stay glued to 00:00 UTC


def test_weekly_window_follows_local_time_across_the_autumn_dst_change(user):
    t = weekly_sunday_2am_kyiv(user)
    expand_maintenance(now=utc(2026, 10, 10, 12), horizon_days=30)
    got = dict(spans(t))
    assert got[utc(2026, 10, 17, 23)] == utc(2026, 10, 18, 0)  # summer
    assert got[utc(2026, 10, 24, 23)] == utc(2026, 10, 25, 0)  # change night: 03:00 happens twice, first one wins
    assert got[utc(2026, 11, 1, 0)] == utc(2026, 11, 1, 1)  # winter again: back to 00:00 UTC


def test_every_occurrence_is_exactly_one_hour_of_wall_clock_time_in_kyiv(user):
    t = weekly_sunday_2am_kyiv(user)
    expand_maintenance(now=utc(2026, 3, 1, 12), horizon_days=30)
    kyiv = ZoneInfo(KYIV)
    for start, end in spans(t):
        s = start.astimezone(kyiv)
        assert (s.hour, s.minute, s.weekday()) == (2, 0, 6)
        assert end - start == timedelta(hours=1)


def test_a_local_time_that_does_not_exist_still_gives_a_non_empty_window(user):
    """03:30-04:30 daily: on 29 Mar 2026 Kyiv jumps 03:00 -> 04:00, so 03:30 never happens."""
    t = window(
        user, utc(2026, 3, 27, 1, 30), utc(2026, 3, 27, 2, 30), rrule="FREQ=DAILY", timezone=KYIV
    )  # 03:30-04:30 EET
    expand_maintenance(now=utc(2026, 3, 27, 12), horizon_days=4)
    got = {s: e for s, e in spans(t)}
    start = utc(2026, 3, 29, 1, 30)  # pre-transition offset applied to the missing 03:30
    assert start in got and got[start] > start and got[start] - start == timedelta(hours=1)
    assert all(e > s for s, e in got.items())


def test_expansion_is_idempotent_and_bounded_by_the_horizon(user):
    t = weekly_sunday_2am_kyiv(user)
    first = expand_maintenance(now=utc(2026, 3, 1, 12), horizon_days=21)
    again = expand_maintenance(now=utc(2026, 3, 1, 12), horizon_days=21)
    assert first == 3 and again == 0
    assert MaintenanceWindow.objects.filter(parent=t).count() == 3
    expand_maintenance(now=utc(2026, 3, 1, 12), horizon_days=35)
    assert MaintenanceWindow.objects.filter(parent=t).count() == 5


def test_deleting_the_template_removes_its_occurrences(user):
    t = weekly_sunday_2am_kyiv(user)
    expand_maintenance(now=utc(2026, 3, 1, 12), horizon_days=21)
    t.delete()
    assert not MaintenanceWindow.objects.exists()


def test_the_template_itself_is_never_a_window(user, mon):
    weekly_sunday_2am_kyiv(user, mon)
    with time_machine.travel(utc(2026, 3, 1, 0, 30), tick=False):  # inside the template's own first slot
        assert not maintenance.is_active(mon, timezone.now())  # only expanded occurrences count


@pytest.mark.parametrize(
    "rule",
    ["FREQ=SECONDLY", "FREQ=MINUTELY;INTERVAL=5", "FREQ=HOURLY", "BYDAY=SU", "nonsense", "FREQ=WEEKLY;COUNT=100000",
     "DTSTART:20260101T000000\nRRULE:FREQ=DAILY", "FREQ=DAILY;INTERVAL=0", "FREQ=WEEKLY;BYSECOND=1"],
)  # fmt: skip
def test_dangerous_or_malformed_rules_are_rejected(rule):
    with pytest.raises(ValueError):
        maintenance.validate_rrule(rule)


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=WEEKLY;BYDAY=SU",
        "FREQ=DAILY",
        "FREQ=MONTHLY;BYMONTHDAY=1;COUNT=12",
        "RRULE:FREQ=WEEKLY;UNTIL=20271231T000000Z",
    ],
)
def test_sensible_rules_are_accepted(rule):
    assert maintenance.validate_rrule(rule)


def test_unknown_timezone_is_rejected():
    with pytest.raises(ValueError):
        maintenance.validate_timezone("Mars/Olympus")
    assert maintenance.validate_timezone(KYIV) == KYIV


def test_the_periodic_task_is_scheduled(settings):
    assert settings.CELERY_BEAT_SCHEDULE["expand-maintenance"]["task"] == "monitors.tasks.expand_maintenance"


# ---------------- scope ----------------
def test_a_user_wide_window_covers_every_monitor_of_that_user_only(user, other_user, mon):
    second = Monitor.objects.create(user=user, name="B", url="https://b.example.com")
    foreign = Monitor.objects.create(user=other_user, name="F", url="https://f.example.com")
    now = timezone.now()
    window(user, now - timedelta(hours=1), now + timedelta(hours=1))  # monitor=None
    assert maintenance.is_active(mon, now) and maintenance.is_active(second, now)
    assert not maintenance.is_active(foreign, now)


def test_a_monitor_window_covers_only_that_monitor(user, mon):
    other = Monitor.objects.create(user=user, name="B", url="https://b.example.com")
    now = timezone.now()
    window(user, now - timedelta(hours=1), now + timedelta(hours=1), monitor=mon)
    assert maintenance.is_active(mon, now) and not maintenance.is_active(other, now)


def test_windows_are_half_open_start_inclusive_end_exclusive(user, mon):
    s, e = utc(2026, 6, 1, 10), utc(2026, 6, 1, 11)
    window(user, s, e, monitor=mon)
    assert maintenance.is_active(mon, s) and maintenance.is_active(mon, e - timedelta(seconds=1))
    assert not maintenance.is_active(mon, e) and not maintenance.is_active(mon, s - timedelta(seconds=1))


def test_dashboard_shows_maintenance_for_a_user_wide_window(user, mon):
    now = timezone.now()
    window(user, now - timedelta(minutes=5), now + timedelta(hours=1))
    assert [r.status for r in dashboard_rows(user)] == ["maintenance"]


# ---------------- checks keep running ----------------
def inside(user, mon, **kw):
    now = timezone.now()
    return window(user, now - timedelta(hours=1), now + timedelta(hours=1), monitor=mon, **kw)


def test_checks_are_still_scheduled_and_executed_during_a_window(user, mon):
    inside(user, mon)
    assert mon.pk in set(due_monitors().values_list("pk", flat=True))
    import responses

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, mon.url, status=200)
        assert check_monitor(mon.pk) == "up"
    r = CheckResult.objects.get()
    assert r.in_maintenance is True  # stored, but flagged


def test_a_check_outside_any_window_is_not_flagged(user, mon):
    record_check(mon.pk, "eu", timezone.now(), outcome(True))
    assert CheckResult.objects.get().in_maintenance is False


# ---------------- incidents ----------------
def down(mon, at=None):
    return record_check(mon.pk, "eu", at or timezone.now(), outcome(False))


def up(mon, at=None):
    return record_check(mon.pk, "eu", at or timezone.now(), outcome(True, ms=20))


def test_no_incident_is_opened_inside_a_window_and_one_opens_after_it(user, mon):
    start = utc(2026, 6, 1, 10)
    window(user, start, start + timedelta(hours=1), monitor=mon)
    down(mon, start + timedelta(minutes=10))
    down(mon, start + timedelta(minutes=50))
    assert not Incident.objects.exists()
    down(mon, start + timedelta(hours=1, minutes=1))  # still down after the window
    inc = Incident.objects.get()
    assert inc.started_at == start + timedelta(hours=1, minutes=1) and inc.during_maintenance is False


def test_an_incident_opened_before_the_window_is_not_closed_artificially(user, mon):
    start = utc(2026, 6, 1, 10)
    down(mon, start - timedelta(minutes=30))
    window(user, start, start + timedelta(hours=1), monitor=mon)
    down(mon, start + timedelta(minutes=10))  # still down while maintenance runs
    inc = Incident.objects.get()
    assert inc.resolved_at is None and inc.during_maintenance is True


def test_real_recovery_during_a_window_still_closes_the_incident(user, mon):
    start = utc(2026, 6, 1, 10)
    down(mon, start - timedelta(minutes=30))
    window(user, start, start + timedelta(hours=1), monitor=mon)
    up(mon, start + timedelta(minutes=20))
    assert Incident.objects.get().resolved_at == start + timedelta(minutes=20)


def test_an_incident_that_never_overlapped_a_window_is_not_flagged(user, mon):
    down(mon, utc(2026, 6, 1, 8))
    down(mon, utc(2026, 6, 1, 9))
    assert Incident.objects.get().during_maintenance is False


# ---------------- uptime ----------------
def test_samples_taken_inside_a_window_do_not_count_towards_uptime(user, mon):
    now = timezone.now()
    window(user, now - timedelta(minutes=40), now + timedelta(hours=1), monitor=mon)
    for i in range(10):
        up(mon, now - timedelta(hours=3, minutes=i))  # healthy, outside the window
    for i in range(30):
        down(mon, now - timedelta(minutes=30 - i))  # the site was being worked on: down, inside the window
    s = monitor_stats(mon, "24h")
    assert (s["total"], s["down"], s["uptime"]) == (10, 0, 100.0)
    assert dashboard_rows(user)[0].uptime_24h == 100.0
    assert CheckResult.objects.count() == 40  # nothing was thrown away


def test_exclusion_is_by_timestamp_not_by_an_expected_number_of_checks(user, mon):
    """A one-hour window with 30 samples inside (the interval was changed, or checks were missed): exactly those
    30 are excluded. Subtracting 'window / interval = 60 expected checks' would eat 30 healthy ones too."""
    now = timezone.now()
    window(user, now - timedelta(hours=1), now, monitor=mon)
    for i in range(100):
        up(mon, now - timedelta(hours=2, minutes=i))  # outside, all healthy
    for i in range(30):
        down(mon, now - timedelta(minutes=2 * i + 1))  # inside, sparse
    assert monitor_stats(mon, "24h")["total"] == 100 and monitor_stats(mon, "24h")["uptime"] == 100.0


def test_the_hourly_rollup_and_the_raw_tail_agree_on_maintenance(user, mon):
    now = utc(2026, 6, 10, 12, 30)
    with time_machine.travel(now, tick=False):
        window(user, utc(2026, 6, 8, 0), utc(2026, 6, 8, 6), monitor=mon)  # a window days ago (rollup territory)
        window(user, utc(2026, 6, 10, 12, 0), utc(2026, 6, 10, 13, 0), monitor=mon)  # one running now (raw tail)
        for h in range(0, 24 * 4):
            t = utc(2026, 6, 6, 12) + timedelta(hours=h)
            (down if t.day == 8 and t.hour < 6 else up)(mon, t)
        down(mon, utc(2026, 6, 10, 12, 10))  # inside the running window
        call_command("backfill_rollups")
        stats = monitor_stats(mon, "7d")
        assert stats["down"] == 0 and stats["uptime"] == 100.0
        assert uptime_by_monitor([mon.pk], "7d")[mon.pk] == 100.0
        raw = CheckResult.objects.filter(monitor=mon, in_maintenance=False, checked_at__gte=rollups.window_start(7))
        assert stats["total"] == raw.count()


def test_report_downtime_subtracts_the_overlap_with_maintenance(user, mon):
    s = utc(2026, 6, 1, 10)
    Incident.objects.create(monitor=mon, started_at=s, resolved_at=s + timedelta(hours=2), first_error="x")
    window(user, s + timedelta(hours=1), s + timedelta(hours=2, minutes=30), monitor=mon)  # covers the second hour
    with time_machine.travel(utc(2026, 6, 2, 12), tick=False):
        row = next(r for r in build_rows(user, s.date(), s.date()) if r[0] == "Сайт")
    assert row[7] == 60.0  # 120 min of incident minus 60 min inside maintenance


def test_overlap_minutes_merges_overlapping_windows_instead_of_double_counting(user, mon):
    s = utc(2026, 6, 1, 10)
    window(user, s, s + timedelta(minutes=40), monitor=mon)
    window(user, s + timedelta(minutes=20), s + timedelta(minutes=60), monitor=mon)  # overlaps the first
    window(user, s, s + timedelta(minutes=60))  # user-wide, covers both
    assert maintenance.overlap_minutes(mon, s, s + timedelta(hours=2)) == 60.0


# ---------------- creating windows through the API ----------------
def api_post(client, monitor, body):
    return client.post(f"/api/v1/monitors/{monitor.pk}/maintenance", body, content_type="application/json")


def iso(dt):
    return dt.isoformat()


def test_api_creates_a_recurring_window_and_expands_it_immediately(auth_client, user, mon):
    with time_machine.travel(utc(2026, 3, 20, 12), tick=False):
        body = {
            "starts_at": iso(utc(2026, 3, 1, 0)), "ends_at": iso(utc(2026, 3, 1, 1)), "reason": "щонеділі",
            "rrule": "FREQ=WEEKLY;BYDAY=SU", "timezone": KYIV,
        }  # fmt: skip
        r = api_post(auth_client, mon, body)
        assert r.status_code == 201, r.content
        template = MaintenanceWindow.objects.get(pk=r.json()["id"])
        assert (template.rrule, template.timezone, template.monitor_id) == ("FREQ=WEEKLY;BYDAY=SU", KYIV, mon.pk)
        assert MaintenanceWindow.objects.filter(parent=template).count() >= 4  # materialised right away


def test_api_all_monitors_creates_a_user_wide_window(auth_client, user, mon):
    now = timezone.now()
    body = {"starts_at": iso(now + timedelta(hours=1)), "ends_at": iso(now + timedelta(hours=2)), "all_monitors": True}
    r = api_post(auth_client, mon, body)
    assert r.status_code == 201
    w = MaintenanceWindow.objects.get()
    assert w.monitor_id is None and w.user_id == user.pk and r.json()["all_monitors"] is True


@pytest.mark.parametrize(
    "extra,field",
    [
        ({"rrule": "FREQ=SECONDLY"}, "rrule"),
        ({"rrule": "FREQ=DAILY", "timezone": "Mars/Base"}, "timezone"),
        ({"timezone": "Nope/Nope"}, "timezone"),
    ],
)
def test_api_rejects_dangerous_rules_and_unknown_zones_with_field_errors(auth_client, mon, extra, field):
    now = timezone.now()
    body = {"starts_at": iso(now + timedelta(hours=1)), "ends_at": iso(now + timedelta(hours=2)), **extra}
    r = api_post(auth_client, mon, body)
    assert r.status_code == 422 and field in r.json()["errors"]
    assert not MaintenanceWindow.objects.exists()


def test_a_recurring_template_may_start_in_the_past_but_a_one_off_window_may_not(auth_client, mon):
    past = timezone.now() - timedelta(days=30)
    rec = api_post(
        auth_client, mon, {"starts_at": iso(past), "ends_at": iso(past + timedelta(hours=1)), "rrule": "FREQ=WEEKLY"}
    )
    one_off = api_post(auth_client, mon, {"starts_at": iso(past), "ends_at": iso(past + timedelta(hours=1))})
    assert rec.status_code == 201 and one_off.status_code == 422


def test_api_lists_templates_and_upcoming_one_off_windows_but_not_their_occurrences(auth_client, user, mon):
    with time_machine.travel(utc(2026, 3, 20, 12), tick=False):
        api_post(
            auth_client,
            mon,
            {
                "starts_at": iso(utc(2026, 3, 1, 0)),
                "ends_at": iso(utc(2026, 3, 1, 1)),
                "rrule": "FREQ=WEEKLY",
                "timezone": KYIV,
            },
        )
        api_post(auth_client, mon, {"starts_at": iso(utc(2026, 3, 21, 0)), "ends_at": iso(utc(2026, 3, 21, 1))})
        listed = auth_client.get(f"/api/v1/monitors/{mon.pk}/maintenance").json()
    assert sorted(bool(w["rrule"]) for w in listed) == [False, True]  # one template + one one-off, no occurrences
    assert all({"rrule", "timezone", "all_monitors"} <= set(w) for w in listed)


def test_a_user_wide_window_is_listed_on_every_monitor_and_deletable_from_there(auth_client, user, mon):
    now = timezone.now()
    w = window(user, now + timedelta(hours=1), now + timedelta(hours=2))
    assert [x["id"] for x in auth_client.get(f"/api/v1/monitors/{mon.pk}/maintenance").json()] == [w.pk]
    assert auth_client.delete(f"/api/v1/monitors/{mon.pk}/maintenance/{w.pk}").status_code == 200
    assert not MaintenanceWindow.objects.exists()


def test_a_foreign_users_user_wide_window_is_neither_listed_nor_deletable(auth_client, other_user, mon):
    now = timezone.now()
    theirs = window(other_user, now + timedelta(hours=1), now + timedelta(hours=2))
    assert auth_client.get(f"/api/v1/monitors/{mon.pk}/maintenance").json() == []
    assert auth_client.delete(f"/api/v1/monitors/{mon.pk}/maintenance/{theirs.pk}").status_code == 404


def test_the_public_page_shows_maintenance_for_a_user_wide_window(client, user, mon):
    Monitor.objects.filter(pk=mon.pk).update(is_public=True, public_slug="pub-slug-1")
    now = timezone.now()
    window(user, now - timedelta(minutes=5), now + timedelta(hours=1))
    html = client.get("/status/pub-slug-1/").content.decode()
    assert "Планові роботи" in html
