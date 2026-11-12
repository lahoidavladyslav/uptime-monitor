from datetime import timedelta

import pytest
import requests
import responses
import time_machine
from django.utils import timezone

from monitors.models import CheckResult, Monitor
from monitors.services import monitor_stats
from monitors.tasks import check_monitor, cleanup_old_checks, schedule_checks

URL = "https://example.com"


@responses.activate
@pytest.mark.parametrize("code,is_up", [(200, True), (204, True), (301, True), (404, False), (500, False)])
def test_check_status_codes(monitor, code, is_up):
    responses.add(responses.GET, URL, status=code)
    assert check_monitor(monitor.pk) == ("up" if is_up else "down")
    r = CheckResult.objects.get()
    assert r.status_code == code and r.is_up is is_up
    assert r.response_time_ms is not None
    monitor.refresh_from_db()
    assert monitor.last_checked_at == r.checked_at


@responses.activate
def test_check_timeout_and_connection_error(monitor):
    responses.add(responses.GET, URL, body=requests.Timeout())
    assert check_monitor(monitor.pk) == "down"
    r = CheckResult.objects.get()
    assert r.status_code is None and r.response_time_ms is None
    assert "Timeout" in r.error

    responses.replace(responses.GET, URL, body=requests.ConnectionError("boom"))
    check_monitor(monitor.pk, force=True)
    assert CheckResult.objects.filter(is_up=False).count() == 2


@responses.activate
def test_duplicate_enqueue_does_not_create_duplicate_check(monitor):
    responses.add(responses.GET, URL, status=200)
    assert check_monitor(monitor.pk) == "up"
    assert check_monitor(monitor.pk) == "skipped"  # not due again yet
    assert CheckResult.objects.count() == 1
    assert check_monitor(monitor.pk, force=True) == "up"  # "check now" bypasses
    assert CheckResult.objects.count() == 2


@responses.activate
def test_inactive_monitor_is_skipped(monitor):
    Monitor.objects.filter(pk=monitor.pk).update(is_active=False)
    assert check_monitor(monitor.pk) == "skipped"
    assert not CheckResult.objects.exists()


def test_schedule_checks_picks_only_due_monitors(user, monkeypatch):
    now = timezone.now()

    def mk(n, **kw):
        return Monitor.objects.create(user=user, name=n, url=f"https://{n}.com", **kw)

    never = mk("never", interval_seconds=300)
    due = mk("due", interval_seconds=300, last_checked_at=now - timedelta(seconds=301))
    fresh = mk("fresh", interval_seconds=300, last_checked_at=now - timedelta(seconds=100))
    long_ago_15m = mk("slow", interval_seconds=900, last_checked_at=now - timedelta(seconds=400))
    inactive = mk("off", is_active=False)

    queued = []
    monkeypatch.setattr("monitors.tasks.check_monitor.delay", lambda pk, **kw: queued.append(pk))
    assert schedule_checks() == 2
    assert set(queued) == {never.pk, due.pk}
    assert not {fresh.pk, long_ago_15m.pk, inactive.pk} & set(queued)


def test_schedule_checks_respects_time(user, monkeypatch):
    queued = []
    monkeypatch.setattr("monitors.tasks.check_monitor.delay", lambda pk, **kw: queued.append(pk))
    t0 = timezone.now()
    Monitor.objects.create(user=user, name="a", url="https://a.com", interval_seconds=60, last_checked_at=t0)
    with time_machine.travel(t0 + timedelta(seconds=30), tick=False):
        assert schedule_checks() == 0
    with time_machine.travel(t0 + timedelta(seconds=61), tick=False):
        assert schedule_checks() == 1


def test_stats_percentage_and_no_data(monitor):
    assert monitor_stats(monitor, "24h")["uptime"] is None  # no ZeroDivisionError
    now = timezone.now()
    for i, (up, ms) in enumerate([(True, 100), (True, 300), (True, None), (False, None)]):
        CheckResult.objects.create(
            monitor=monitor, checked_at=now - timedelta(minutes=i), is_up=up, response_time_ms=ms
        )
    # an old check, outside the 24h window but inside 7d
    CheckResult.objects.create(monitor=monitor, checked_at=now - timedelta(days=3), is_up=False)
    s24 = monitor_stats(monitor, "24h")
    assert (s24["total"], s24["down"], s24["uptime"]) == (4, 1, 75.0)
    assert (s24["rt_avg"], s24["rt_min"], s24["rt_max"]) == (200, 100, 300)
    s7 = monitor_stats(monitor, "7d")
    assert (s7["total"], s7["down"], s7["uptime"]) == (5, 2, 60.0)


def test_cleanup_deletes_old_in_batches(monitor, settings):
    settings.CLEANUP_BATCH_SIZE = 3
    now = timezone.now()
    for i in range(8):
        CheckResult.objects.create(monitor=monitor, checked_at=now - timedelta(days=31 + i), is_up=True)
    keep = CheckResult.objects.create(monitor=monitor, checked_at=now - timedelta(days=29), is_up=True)
    assert cleanup_old_checks() == 8
    assert list(CheckResult.objects.all()) == [keep]


def test_check_now_button_queues_and_flashes(auth_client, monitor, monkeypatch):
    calls = []
    monkeypatch.setattr("monitors.views.check_monitor.delay", lambda pk, **kw: calls.append((pk, kw)))
    r = auth_client.post(f"/monitors/{monitor.pk}/check/", follow=True)
    assert calls == [(monitor.pk, {"force": True})]
    assert "Перевірку запущено" in r.content.decode()


def test_detail_page_filter_and_pagination(auth_client, monitor):
    now = timezone.now()
    for i in range(25):
        CheckResult.objects.create(monitor=monitor, checked_at=now - timedelta(minutes=i), is_up=(i % 5 != 0))
    r = auth_client.get(f"/monitors/{monitor.pk}/")
    assert len(r.context["page"]) == 20
    r = auth_client.get(f"/monitors/{monitor.pk}/?only_failed=1")
    assert len(r.context["page"]) == 5
    assert all(not c.is_up for c in r.context["page"])
    assert auth_client.get(f"/monitors/{monitor.pk}/?period=bogus").status_code == 200
