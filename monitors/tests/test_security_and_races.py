import socket

import pytest
import responses
from django.db import connection
from django.urls import reverse

from monitors.forms import MonitorForm
from monitors.models import CheckResult, Incident, Monitor
from monitors.security import blocked_reason
from monitors.tasks import check_monitor


def resolve_to(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(
        "monitors.security._resolve",
        lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, 0))],
    )


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",
        "fd00::1",
    ],
)
def test_private_addresses_are_blocked(monkeypatch, ip):
    resolve_to(monkeypatch, ip)
    assert blocked_reason("http://whatever.example") is not None


def test_public_address_allowed_and_unresolvable_not_blocked(monkeypatch):
    resolve_to(monkeypatch, "93.184.216.34")
    assert blocked_reason("https://example.com") is None

    def nxdomain(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr("monitors.security._resolve", nxdomain)
    assert blocked_reason("http://nonexistent.invalid") is None  # just reports "down" later


def test_one_private_record_among_public_ones_blocks(monkeypatch):
    monkeypatch.setattr(
        "monitors.security._resolve",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
        ],
    )
    assert blocked_reason("http://rebind.example") is not None


def test_ip_literal_urls_are_blocked_without_dns(user):
    for url in ("http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/"):
        form = MonitorForm({"name": "x", "url": url, "interval_seconds": 60}, user=user)
        assert not form.is_valid() and "url" in form.errors, url


def test_allow_private_targets_setting(monkeypatch, settings):
    resolve_to(monkeypatch, "127.0.0.1")
    settings.ALLOW_PRIVATE_TARGETS = True
    assert blocked_reason("http://localhost") is None


@responses.activate
def test_check_time_guard_blocks_even_if_saved_earlier(monitor, monkeypatch):
    """DNS can change after the monitor was saved (rebinding): re-checked on every run."""
    resolve_to(monkeypatch, "10.1.2.3")
    responses.add(responses.GET, monitor.url, status=200)  # must never be requested
    assert check_monitor(monitor.pk) == "down"
    r = CheckResult.objects.get()
    assert not r.is_up and "Заблоковано" in r.error
    assert len(responses.calls) == 0


@responses.activate
def test_http_request_runs_outside_a_transaction(monitor, monkeypatch):
    responses.add(responses.GET, monitor.url, status=200)
    seen = {}

    import monitors.tasks as tasks

    original = tasks.perform_http_check

    def spy(url, **kw):
        # The test itself runs in one outer transaction; check_monitor opening its own
        # atomic() block would add a savepoint. None must be open during the request.
        seen["savepoints"] = list(connection.savepoint_ids)
        return original(url, **kw)

    monkeypatch.setattr(tasks, "perform_http_check", spy)
    assert check_monitor(monitor.pk) == "up"
    assert seen["savepoints"] == []  # no transaction/row lock held during the request


@responses.activate
def test_concurrent_duplicate_during_request_is_skipped(monitor, monkeypatch):
    """A second task arriving while the first is mid-request must not run a second check."""
    responses.add(responses.GET, monitor.url, status=200)
    import monitors.tasks as tasks

    original = tasks.perform_http_check
    inner = {}

    def spy(url, **kw):
        inner["result"] = check_monitor(monitor.pk)  # the "duplicate"
        return original(url, **kw)

    monkeypatch.setattr(tasks, "perform_http_check", spy)
    assert check_monitor(monitor.pk) == "up"
    assert inner["result"] == "skipped"
    assert CheckResult.objects.count() == 1


@responses.activate
def test_monitor_deleted_mid_request_is_handled(monitor, monkeypatch):
    responses.add(responses.GET, monitor.url, status=200)
    import monitors.tasks as tasks

    original = tasks.perform_http_check

    def spy(url, **kw):
        Monitor.objects.filter(pk=monitor.pk).delete()
        return original(url, **kw)

    monkeypatch.setattr(tasks, "perform_http_check", spy)
    assert check_monitor(monitor.pk) == "skipped"
    assert not CheckResult.objects.exists() and not Incident.objects.exists()


def test_check_now_cooldown(auth_client, monitor, monkeypatch):
    calls = []
    monkeypatch.setattr("monitors.views.check_monitor.delay", lambda pk, **kw: calls.append(pk))
    url = reverse("monitor_check", args=[monitor.pk])
    auth_client.post(url)
    r = auth_client.post(url, follow=True)
    assert len(calls) == 1
    assert "Зачекайте" in r.content.decode()
