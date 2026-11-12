import ssl
from datetime import timedelta

import pytest
import responses
from django.core import mail
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from monitors.checks import evaluate, headers_to_lines, parse_header_lines, parse_status_spec, status_ok
from monitors.forms import MaintenanceForm, MonitorForm
from monitors.models import CheckResult, MaintenanceWindow, Monitor
from monitors.tasks import (
    check_monitor,
    check_ssl,
    due_monitors,
    perform_http_check,
    schedule_ssl_checks,
    ssl_level,
)

URL = "https://example.com"


# ---------------- pure helpers ----------------
@pytest.mark.parametrize(
    "spec,code,ok",
    [
        ("", 200, True), ("", 301, True), ("", 404, False), ("", 500, False),
        ("200", 200, True), ("200", 204, False),
        ("200,204", 204, True), ("200-299,404", 404, True), ("200-299,404", 301, False),
        ("401", 401, True),  # a protected endpoint can be "up" when it answers 401
    ],
)  # fmt: skip
def test_status_spec(spec, code, ok):
    assert status_ok(code, spec) is ok


@pytest.mark.parametrize("bad", ["abc", "20", "200-", "600", "300-200", "99", "200;204"])
def test_status_spec_rejects_garbage(bad):
    with pytest.raises(ValidationError):
        parse_status_spec(bad)


def test_headers_parse_roundtrip_and_rules():
    parsed = parse_header_lines("Authorization: Bearer x:y\nAccept: application/json\n\n")
    assert parsed == {"Authorization": "Bearer x:y", "Accept": "application/json"}  # value may contain ':'
    assert parse_header_lines(headers_to_lines(parsed)) == parsed
    for bad in ["no colon here", "Host: evil.example", "content-length: 5", "Bad Name: x", ": empty"]:
        with pytest.raises(ValidationError):
            parse_header_lines(bad)
    with pytest.raises(ValidationError):
        parse_header_lines("\n".join(f"H{i}: v" for i in range(11)))


def test_evaluate_messages():
    assert evaluate(200, "", "", "") == (True, None)
    assert evaluate(500, "", "", "") == (False, "HTTP 500")
    assert evaluate(404, "200,204", "", "") == (False, "Очікувався код 200,204, отримано 404")
    assert evaluate(200, "", "welcome", "hello welcome") == (True, None)
    assert evaluate(200, "", "welcome", "an empty page") == (False, "Очікуваний текст не знайдено у відповіді")


# ---------------- HTTP behaviour ----------------
@responses.activate
def test_method_headers_and_body_are_sent():
    responses.add(responses.POST, URL, status=200)
    out = perform_http_check(
        URL, method="POST", headers={"X-Api-Key": "k", "Content-Type": "application/json"}, body='{"a":1}'
    )
    assert out[2] is True
    req = responses.calls[0].request
    assert req.method == "POST" and req.body == b'{"a":1}'
    assert req.headers["X-Api-Key"] == "k" and req.headers["User-Agent"] == "UptimeMonitor/1.0"


@responses.activate
def test_head_does_not_send_body_or_read_it():
    responses.add(responses.HEAD, URL, status=200)
    assert perform_http_check(URL, method="HEAD", body="ignored")[2] is True
    assert responses.calls[0].request.body is None


@responses.activate
def test_expected_text_present_and_missing():
    responses.add(responses.GET, URL, body="<h1>Welcome back</h1>", status=200)
    assert perform_http_check(URL, expected_text="Welcome")[2] is True
    up = perform_http_check(URL, expected_text="Maintenance")
    assert up[2] is False and "не знайдено" in up[3]
    assert up[0] == 200  # server answered 200 but the content is wrong -> still down


@responses.activate
def test_expected_text_search_is_capped(monkeypatch):
    monkeypatch.setattr("monitors.tasks.MAX_BODY_BYTES", 100)
    responses.add(responses.GET, URL, body="a" * 200 + "NEEDLE", status=200)
    assert perform_http_check(URL, expected_text="NEEDLE")[2] is False  # beyond the cap


@responses.activate
def test_expected_status_overrides_default():
    responses.add(responses.GET, URL, status=401)
    assert perform_http_check(URL)[2] is False
    assert perform_http_check(URL, expected_status="401")[2] is True
    responses.replace(responses.GET, URL, status=200)
    out = perform_http_check(URL, expected_status="401")
    assert out[2] is False and "Очікувався код 401" in out[3]


@responses.activate
def test_check_monitor_uses_the_monitor_configuration(user):
    m = Monitor.objects.create(
        user=user, name="api", url=URL, http_method="POST", request_headers={"X-A": "1"},
        request_body="ping", expected_status="201", expected_text="created",
    )  # fmt: skip
    responses.add(responses.POST, URL, status=201, body="resource created")
    assert check_monitor(m.pk) == "up"
    assert responses.calls[0].request.headers["X-A"] == "1"
    assert CheckResult.objects.get().status_code == 201


# ---------------- form ----------------
def form(user, **over):
    data = {"name": "x", "url": "https://a.com", "interval_seconds": 60, "http_method": "GET", **over}
    return MonitorForm(data, user=user)


def test_form_accepts_valid_advanced_config(user):
    f = form(
        user,
        http_method="POST",
        request_body="x",
        request_headers="X-A: 1",
        expected_status="200,204",
        expected_text="ok",
    )
    assert f.is_valid(), f.errors
    m = f.save(commit=False)
    assert m.request_headers == {"X-A": "1"}


@pytest.mark.parametrize(
    "over,field",
    [
        ({"expected_status": "abc"}, "expected_status"),
        ({"request_headers": "Host: x"}, "request_headers"),
        ({"http_method": "HEAD", "expected_text": "x"}, "expected_text"),
        ({"http_method": "GET", "request_body": "x"}, "request_body"),
        ({"http_method": "DELETE"}, "http_method"),
    ],
)
def test_form_rejects_bad_advanced_config(user, over, field):
    f = form(user, **over)
    assert not f.is_valid() and field in f.errors


def test_edit_form_shows_headers_as_lines(user):
    m = Monitor.objects.create(user=user, name="x", url=URL, request_headers={"A": "1", "B": "2"})
    f = MonitorForm(instance=m, user=user)
    assert f.initial["request_headers"] == "A: 1\nB: 2"


# ---------------- maintenance ----------------
def window(monitor, start=-10, end=+60):
    now = timezone.now()
    return MaintenanceWindow.objects.create(
        monitor=monitor, starts_at=now + timedelta(minutes=start), ends_at=now + timedelta(minutes=end)
    )


def test_due_monitors_skips_those_in_maintenance(user, monitor):
    assert monitor in due_monitors()
    w = window(monitor)
    assert monitor not in due_monitors()
    w.delete()
    window(monitor, start=-120, end=-60)  # past window
    window(monitor, start=30, end=90)  # future window
    assert monitor in due_monitors()


@responses.activate
def test_no_check_runs_during_maintenance_even_when_forced(monitor):
    responses.add(responses.GET, monitor.url, status=500)
    window(monitor)
    assert check_monitor(monitor.pk) == "skipped"
    assert check_monitor(monitor.pk, force=True) == "skipped"
    assert not CheckResult.objects.exists()
    assert len(responses.calls) == 0


def test_check_now_button_explains_maintenance(auth_client, monitor, monkeypatch):
    calls = []
    monkeypatch.setattr("monitors.views.enqueue_checks", lambda *a, **k: calls.append(a))
    window(monitor)
    r = auth_client.post(reverse("monitor_check", args=[monitor.pk]), follow=True)
    assert calls == [] and "на обслуговуванні" in r.content.decode()


def test_maintenance_form_validation(db):
    now = timezone.now()
    fmt = "%Y-%m-%dT%H:%M"

    def f(start, end):
        return MaintenanceForm({"starts_at": (now + start).strftime(fmt), "ends_at": (now + end).strftime(fmt)})

    assert f(timedelta(hours=1), timedelta(hours=2)).is_valid()
    assert not f(timedelta(hours=2), timedelta(hours=1)).is_valid()  # end before start
    assert not f(-timedelta(hours=3), -timedelta(hours=1)).is_valid()  # already over
    assert not f(timedelta(0), timedelta(days=40)).is_valid()  # too long


def test_create_and_delete_window_via_views_and_ownership(client, user, other_user, monitor):
    client.force_login(user)
    now = timezone.now()
    data = {
        "starts_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "ends_at": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "reason": "db upgrade",
    }
    assert client.post(reverse("maintenance_create", args=[monitor.pk]), data).status_code == 302
    w = MaintenanceWindow.objects.get()
    assert "db upgrade" in client.get(reverse("monitor_detail", args=[monitor.pk])).content.decode()

    client.force_login(other_user)  # someone else's monitor/window
    assert client.post(reverse("maintenance_create", args=[monitor.pk]), data).status_code == 404
    assert client.post(reverse("maintenance_delete", args=[monitor.pk, w.pk])).status_code == 404
    assert MaintenanceWindow.objects.count() == 1

    client.force_login(user)
    client.post(reverse("maintenance_delete", args=[monitor.pk, w.pk]))
    assert not MaintenanceWindow.objects.exists()


def test_dashboard_and_public_page_show_maintenance(auth_client, client, monitor):
    monitor.is_public = True
    monitor.save()
    window(monitor)
    assert "🛠 обслуговування" in auth_client.get(reverse("dashboard")).content.decode()
    assert "Планові роботи" in client.get(reverse("public_status", args=[monitor.public_slug])).content.decode()


# ---------------- SSL ----------------
@pytest.fixture
def https_monitor(user):
    return Monitor.objects.create(user=user, name="s", url="https://secure.example.com")


def fake_expiry(monkeypatch, days):
    monkeypatch.setattr(
        "monitors.tasks.fetch_ssl_expiry", lambda host, port=443: timezone.now() + timedelta(days=days, hours=1)
    )


@pytest.mark.parametrize("days,level", [(60, None), (15, None), (14, 14), (10, 14), (3, 3), (1, 3), (0, 0)])
def test_ssl_level(days, level):
    assert ssl_level(days) == level


def test_ssl_alert_sent_once_per_threshold_then_reset_on_renewal(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 10)
    check_ssl(https_monitor.pk)
    check_ssl(https_monitor.pk)  # same day again: no second mail
    assert len(mail.outbox) == 1 and "10 дн." in mail.outbox[0].subject

    fake_expiry(monkeypatch, 2)  # crossed the next threshold -> another mail
    check_ssl(https_monitor.pk)
    check_ssl(https_monitor.pk)
    assert len(mail.outbox) == 2 and "2 дн." in mail.outbox[1].subject

    fake_expiry(monkeypatch, 90)  # certificate renewed -> state resets, no mail
    check_ssl(https_monitor.pk)
    https_monitor.refresh_from_db()
    assert https_monitor.ssl_alerted_level is None and len(mail.outbox) == 2

    fake_expiry(monkeypatch, 5)  # and the next expiry alerts again
    check_ssl(https_monitor.pk)
    assert len(mail.outbox) == 3


def test_healthy_certificate_sends_nothing_and_is_stored(https_monitor, monkeypatch):
    fake_expiry(monkeypatch, 80)
    assert check_ssl(https_monitor.pk) == 80
    https_monitor.refresh_from_db()
    assert https_monitor.ssl_expires_at and not mail.outbox and https_monitor.ssl_error == ""


def test_expired_certificate_alerts(https_monitor, monkeypatch):
    def boom(host, port=443):
        err = ssl.SSLCertVerificationError(1, "certificate verify failed: certificate has expired")
        err.verify_message = "certificate has expired"
        raise err

    monkeypatch.setattr("monitors.tasks.fetch_ssl_expiry", boom)
    assert check_ssl(https_monitor.pk) == "invalid"
    https_monitor.refresh_from_db()
    assert "прострочений" in https_monitor.ssl_error
    assert len(mail.outbox) == 1 and "прострочений" in mail.outbox[0].subject


def test_ssl_network_error_is_recorded_not_raised(https_monitor, monkeypatch):
    def boom(host, port=443):
        raise ConnectionRefusedError()

    monkeypatch.setattr("monitors.tasks.fetch_ssl_expiry", boom)
    assert check_ssl(https_monitor.pk) == "error"
    assert not mail.outbox


def test_ssl_check_skips_http_and_private_targets(user, monkeypatch):
    plain = Monitor.objects.create(user=user, name="p", url="http://plain.example.com")
    assert check_ssl(plain.pk) == "skipped"
    monkeypatch.setattr("monitors.tasks.blocked_reason", lambda url: "private")
    priv = Monitor.objects.create(user=user, name="i", url="https://internal.example.com")
    assert check_ssl(priv.pk) == "blocked"


def test_schedule_ssl_checks_only_active_https(user, monkeypatch):
    queued = []
    monkeypatch.setattr("monitors.tasks.check_ssl.delay", lambda pk: queued.append(pk))
    a = Monitor.objects.create(user=user, name="a", url="https://a.com")
    Monitor.objects.create(user=user, name="b", url="http://b.com")
    Monitor.objects.create(user=user, name="c", url="https://c.com", is_active=False)
    assert schedule_ssl_checks() == 1 and queued == [a.pk]


def test_detail_page_shows_ssl_state(auth_client, https_monitor):
    https_monitor.ssl_expires_at = timezone.now() + timedelta(days=2, hours=1)
    https_monitor.save()
    html = auth_client.get(reverse("monitor_detail", args=[https_monitor.pk])).content.decode()
    assert "спливає через 2 дн." in html
