"""O1: SSL expiry. A daily task (not every minute), thresholds 14/7/1 each notified ONCE, reset after renewal."""

import ssl
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from monitors.models import CheckResult, Incident, Monitor
from monitors.tasks import check_ssl, due_monitors, enqueue_checks, schedule_ssl_checks


@pytest.fixture
def https_monitor(user):
    return Monitor.objects.create(user=user, name="s", url="https://secure.example.com")


@pytest.fixture
def ssl_monitor(user):
    return Monitor.objects.create(
        user=user,
        name="cert",
        url="https://cert.example.com",
        check_type="ssl_expiry",
        check_config={"min_days_left": 14},
    )


def fake_expiry(monkeypatch, days):
    monkeypatch.setattr(
        "monitors.tasks.fetch_ssl_expiry", lambda host, port=443: timezone.now() + timedelta(days=days, hours=1)
    )


def alerts():
    return [m for m in mail.outbox if "SSL" in m.subject]


# ---------------- thresholds 14 / 7 / 1, each once ----------------
def test_each_threshold_sends_exactly_one_mail_as_the_certificate_ages(https_monitor, monkeypatch):
    sent = []
    for days in (30, 15, 14, 14, 13, 9, 8, 7, 7, 6, 2, 1, 1, 0):
        fake_expiry(monkeypatch, days)
        check_ssl(https_monitor.pk)
        sent.append(len(alerts()))
    # 30, 15: nothing | 14: first | 14, 13, 9, 8: no more | 7: second | 7, 6, 2: no more | 1: third | 1: no more | 0: expired
    assert sent == [0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 4]
    https_monitor.refresh_from_db()
    assert sorted(https_monitor.ssl_notified) == [0, 1, 7, 14]


def test_the_same_day_checked_twice_never_sends_twice(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 10)
    for _ in range(5):
        check_ssl(https_monitor.pk)
    assert len(alerts()) == 1 and "10 дн." in alerts()[0].subject


def test_first_sight_of_a_nearly_expired_certificate_sends_one_mail_and_records_every_crossed_threshold(
    https_monitor, monkeypatch
):
    fake_expiry(monkeypatch, 5)  # already past 14 and 7
    check_ssl(https_monitor.pk)
    check_ssl(https_monitor.pk)
    assert len(alerts()) == 1
    https_monitor.refresh_from_db()
    assert sorted(https_monitor.ssl_notified) == [7, 14]
    fake_expiry(monkeypatch, 1)
    check_ssl(https_monitor.pk)
    assert len(alerts()) == 2  # the next threshold (1) is still announced


def test_after_the_certificate_is_renewed_the_cycle_starts_again(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 3)
    check_ssl(https_monitor.pk)
    assert len(alerts()) == 1
    fake_expiry(monkeypatch, 90)  # renewed
    check_ssl(https_monitor.pk)
    https_monitor.refresh_from_db()
    assert https_monitor.ssl_notified == [] and len(alerts()) == 1  # reset, and no mail for good news
    fake_expiry(monkeypatch, 13)  # ...and the new certificate ages: alerted again
    check_ssl(https_monitor.pk)
    assert len(alerts()) == 2


def test_a_renewal_that_is_still_short_also_restarts_the_cycle(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 2)
    check_ssl(https_monitor.pk)
    fake_expiry(monkeypatch, 10)  # replaced by a cert valid for only 10 days: still worth a warning
    check_ssl(https_monitor.pk)
    assert len(alerts()) == 2


def test_two_workers_racing_on_the_same_threshold_send_one_mail(https_monitor, monkeypatch):
    """The state change is a conditional UPDATE: of two workers that read the same row, only the winner sends."""
    from monitors import tasks

    fake_expiry(monkeypatch, 10)
    stale = Monitor.objects.get(pk=https_monitor.pk)  # the second worker read the row before the first one wrote
    check_ssl(https_monitor.pk)
    assert len(alerts()) == 1
    tasks._apply_ssl_thresholds(stale, timezone.now() + timedelta(days=10, hours=1), 10)  # its stale copy says "[]"
    assert len(alerts()) == 1


def test_a_healthy_certificate_is_stored_and_silent(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 80)
    assert check_ssl(https_monitor.pk) == 80
    https_monitor.refresh_from_db()
    assert https_monitor.ssl_expires_at and not alerts() and https_monitor.ssl_error == ""


def test_an_expired_certificate_alerts_once(https_monitor, monkeypatch):
    def boom(host, port=443):
        err = ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")
        err.verify_message = "certificate has expired"
        raise err

    monkeypatch.setattr("monitors.tasks.fetch_ssl_expiry", boom)
    assert check_ssl(https_monitor.pk) == "invalid"
    check_ssl(https_monitor.pk)
    https_monitor.refresh_from_db()
    assert "прострочений" in https_monitor.ssl_error
    assert len(alerts()) == 1 and "прострочений" in alerts()[0].subject


def test_a_network_error_is_recorded_not_raised_and_sends_nothing(https_monitor, monkeypatch):
    def boom(host, port=443):
        raise ConnectionRefusedError()

    monkeypatch.setattr("monitors.tasks.fetch_ssl_expiry", boom)
    assert check_ssl(https_monitor.pk) == "error"
    assert not alerts()


def test_plain_http_and_private_targets_are_skipped(user, monkeypatch):
    plain = Monitor.objects.create(user=user, name="p", url="http://plain.example.com")
    assert check_ssl(plain.pk) == "skipped"
    monkeypatch.setattr("monitors.tasks.blocked_reason", lambda url: "private")
    priv = Monitor.objects.create(user=user, name="i", url="https://internal.example.com")
    assert check_ssl(priv.pk) == "blocked"


# ---------------- the daily schedule ----------------
def test_the_daily_task_covers_active_https_monitors_only(user, monkeypatch):
    queued = []
    monkeypatch.setattr("monitors.tasks.check_ssl.delay", lambda pk: queued.append(pk))
    a = Monitor.objects.create(user=user, name="a", url="https://a.com")
    Monitor.objects.create(user=user, name="b", url="http://b.com")
    Monitor.objects.create(user=user, name="c", url="https://c.com", is_active=False)
    assert schedule_ssl_checks() == 1 and queued == [a.pk]


def test_it_is_scheduled_once_a_day_not_every_minute(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["ssl-expiry-checks"]
    assert entry["task"] == "monitors.tasks.schedule_ssl_checks"
    assert (entry["schedule"]._orig_hour, entry["schedule"]._orig_minute) == (4, 0)  # crontab(hour=4, minute=0)


# ---------------- the ssl_expiry check type ----------------
def test_an_ssl_expiry_monitor_is_never_in_the_minute_by_minute_schedule(ssl_monitor, https_monitor):
    due = set(due_monitors().values_list("pk", flat=True))
    assert https_monitor.pk in due and ssl_monitor.pk not in due


def test_the_daily_check_records_an_up_result_with_the_days_left(ssl_monitor, monkeypatch):
    fake_expiry(monkeypatch, 40)
    check_ssl(ssl_monitor.pk)
    r = CheckResult.objects.get()
    assert r.is_up and r.details["days_left"] == 40 and r.details["type"] == "ssl_expiry"
    ssl_monitor.refresh_from_db()
    assert ssl_monitor.last_checked_at is not None and not Incident.objects.exists()


def test_below_the_minimum_the_result_is_down_with_a_reason_and_an_incident_opens(ssl_monitor, monkeypatch):
    fake_expiry(monkeypatch, 5)
    check_ssl(ssl_monitor.pk)
    r = CheckResult.objects.get()
    assert not r.is_up and r.error == "certificate expires in 5 days, need at least 14"
    assert r.details["message"] == r.error
    assert Incident.objects.get().first_error == r.error


def test_renewal_closes_the_incident_on_the_next_daily_check(ssl_monitor, monkeypatch):
    fake_expiry(monkeypatch, 5)
    check_ssl(ssl_monitor.pk)
    fake_expiry(monkeypatch, 80)
    check_ssl(ssl_monitor.pk)
    assert Incident.objects.get().resolved_at is not None and CheckResult.objects.filter(is_up=True).count() == 1


def test_an_expired_certificate_is_a_down_result_for_an_ssl_expiry_monitor(ssl_monitor, monkeypatch):
    def boom(host, port=443):
        err = ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")
        err.verify_message = "certificate has expired"
        raise err

    monkeypatch.setattr("monitors.tasks.fetch_ssl_expiry", boom)
    check_ssl(ssl_monitor.pk)
    r = CheckResult.objects.get()
    assert not r.is_up and "expired" in r.error


def test_a_plain_https_monitor_gets_no_check_result_from_the_daily_task(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 40)
    check_ssl(https_monitor.pk)
    assert not CheckResult.objects.exists()  # it only maintains ssl_expires_at / alerts


def test_check_now_on_an_ssl_expiry_monitor_does_not_try_an_http_request(ssl_monitor, monkeypatch):
    sent = []
    monkeypatch.setattr("monitors.tasks.check_monitor.apply_async", lambda *a, **k: sent.append(1))
    enqueue_checks(ssl_monitor.pk, force=True)
    assert sent == []


def test_detail_page_shows_the_type_and_the_certificate_state(auth_client, ssl_monitor):
    from django.urls import reverse

    ssl_monitor.ssl_expires_at = timezone.now() + timedelta(days=2, hours=1)
    ssl_monitor.save()
    html = auth_client.get(reverse("monitor_detail", args=[ssl_monitor.pk])).content.decode()
    assert "Термін SSL-сертифіката" in html and "спливає через 2 дн." in html
