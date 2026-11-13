from datetime import timedelta

import pytest
import responses
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from monitors.checks import headers_to_lines, parse_header_lines
from monitors.forms import MaintenanceForm, MonitorForm
from monitors.models import CheckResult, Incident, MaintenanceWindow, Monitor
from monitors.tasks import (
    check_monitor,
    due_monitors,
    perform_http_check,
)

URL = "https://example.com"


# ---------------- pure helpers ----------------
def test_headers_parse_roundtrip_and_rules():
    parsed = parse_header_lines("Authorization: Bearer x:y\nAccept: application/json\n\n")
    assert parsed == {"Authorization": "Bearer x:y", "Accept": "application/json"}  # value may contain ':'
    assert parse_header_lines(headers_to_lines(parsed)) == parsed
    for bad in ["no colon here", "Host: evil.example", "content-length: 5", "Bad Name: x", ": empty"]:
        with pytest.raises(ValidationError):
            parse_header_lines(bad)
    with pytest.raises(ValidationError):
        parse_header_lines("\n".join(f"H{i}: v" for i in range(11)))


# ---------------- HTTP behaviour ----------------
@responses.activate
def test_method_headers_and_body_are_sent():
    responses.add(responses.POST, URL, status=200)
    out = perform_http_check(
        URL, method="POST", headers={"X-Api-Key": "k", "Content-Type": "application/json"}, body='{"a":1}'
    )
    assert out.is_up is True
    req = responses.calls[0].request
    assert req.method == "POST" and req.body == b'{"a":1}'
    assert req.headers["X-Api-Key"] == "k" and req.headers["User-Agent"] == "UptimeMonitor/1.0"


@responses.activate
def test_head_does_not_send_body_or_read_it():
    responses.add(responses.HEAD, URL, status=200)
    assert perform_http_check(URL, method="HEAD", body="ignored").is_up is True
    assert responses.calls[0].request.body is None


KEYWORD = {"keyword": "Welcome", "must_contain": True}


@responses.activate
def test_keyword_present_and_missing():
    responses.add(responses.GET, URL, body="<h1>Welcome back</h1>", status=200)
    assert perform_http_check(URL, check_type="keyword", check_config=KEYWORD).is_up is True
    out = perform_http_check(URL, check_type="keyword", check_config={"keyword": "Maintenance", "must_contain": True})
    assert out.is_up is False and out.error == "keyword 'Maintenance' not found"
    assert out.status_code == 200  # server answered 200 but the content is wrong -> still down
    assert out.details["type"] == "keyword"


@responses.activate
def test_keyword_search_is_capped_and_says_it_was_truncated(monkeypatch):
    monkeypatch.setattr("monitors.tasks.MAX_BODY_BYTES", 100)
    responses.add(responses.GET, URL, body="a" * 200 + "NEEDLE", status=200)
    out = perform_http_check(URL, check_type="keyword", check_config={"keyword": "NEEDLE", "must_contain": True})
    assert out.is_up is False and "truncated" in out.error  # beyond the cap


@responses.activate
def test_a_huge_body_is_not_read_for_a_plain_status_check(monkeypatch):
    read = []
    monkeypatch.setattr("monitors.tasks._read_body", lambda *a, **k: read.append(1) or ("", False))
    responses.add(responses.GET, URL, body="x" * 1000, status=200)
    assert perform_http_check(URL).is_up is True and read == []


@responses.activate
def test_json_path_through_the_real_request_path():
    responses.add(responses.GET, URL, json={"status": {"db": "degraded"}}, status=200)
    out = perform_http_check(URL, check_type="json_path", check_config={"path": "status.db", "equals": "ok"})
    assert not out.is_up and out.error == "status.db = 'degraded', expected 'ok'"


@responses.activate
def test_expected_statuses_override_the_default():
    responses.add(responses.GET, URL, status=401)
    assert perform_http_check(URL).is_up is False
    assert perform_http_check(URL, check_config={"expected_statuses": [401]}).is_up is True
    responses.replace(responses.GET, URL, status=200)
    out = perform_http_check(URL, check_config={"expected_statuses": [401]})
    assert out.is_up is False and out.error == "HTTP 200, expected one of [401]"


@responses.activate
def test_check_monitor_uses_the_monitor_configuration(user):
    m = Monitor.objects.create(
        user=user, name="api", url=URL, http_method="POST", request_headers={"X-A": "1"},
        request_body="ping", check_type="keyword",
        check_config={"keyword": "created", "must_contain": True, "expected_statuses": [201]},
    )  # fmt: skip
    responses.add(responses.POST, URL, status=201, body="resource created")
    assert check_monitor(m.pk) == "up"
    assert responses.calls[0].request.headers["X-A"] == "1"
    result = CheckResult.objects.get()
    assert result.status_code == 201 and result.details["type"] == "keyword"


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
        check_type="keyword",
        check_config={"keyword": "ok", "expected_statuses": [200, 204]},
    )
    assert f.is_valid(), f.errors
    m = f.save(commit=False)
    assert m.request_headers == {"X-A": "1"}


@pytest.mark.parametrize(
    "over,field",
    [
        ({"check_type": "keyword", "check_config": {}}, "check_config"),
        ({"check_type": "keyword", "check_config": {"keyword": "x", "oops": 1}}, "check_config"),
        ({"check_type": "ssl_expiry", "url": "http://a.com"}, "url"),
        ({"request_headers": "Host: x"}, "request_headers"),
        ({"http_method": "HEAD", "check_type": "keyword", "check_config": {"keyword": "x"}}, "check_type"),
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


def test_maintenance_does_not_stop_checks_from_being_scheduled(user, monitor):
    """H6: a window used to stop the checks; now they keep running (flagged) and just open no incident."""
    window(monitor)
    assert monitor in due_monitors()


@responses.activate
def test_checks_run_and_are_stored_but_flagged_during_maintenance(monitor):
    responses.add(responses.GET, monitor.url, status=500)
    window(monitor)
    assert check_monitor(monitor.pk) == "down"
    result = CheckResult.objects.get()
    assert result.in_maintenance is True and not result.is_up
    assert not Incident.objects.exists()  # ...and no incident, no alert


def test_check_now_works_during_maintenance(auth_client, monitor, monkeypatch):
    calls = []
    monkeypatch.setattr("monitors.views.enqueue_checks", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("monitors.views.check_ssl.delay", lambda pk: None)
    window(monitor)
    auth_client.post(reverse("monitor_check", args=[monitor.pk]), follow=True)
    assert len(calls) == 1


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
