"""H5 tooling: metrics for backlog/throughput, seed_load, the fake target server and the load compose file."""

import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from django.core.management import call_command
from django.utils import timezone

from monitors import loadstats
from monitors.models import CheckResult, Monitor

ROOT = Path(__file__).resolve().parents[2]


def metrics(client, settings):
    settings.METRICS_TOKEN = "t"
    return client.get("/metrics/", headers={"Authorization": "Bearer t"}).content.decode()


# ---------------- metrics ----------------
def test_metrics_report_queue_length_throughput_percentiles_and_overdue(client, user, settings, monkeypatch):
    monkeypatch.setattr(loadstats, "queue_lengths", lambda: {"checks.eu": 42, "notifications": 0})
    m = Monitor.objects.create(user=user, name="a", url="https://a.example.com", interval_seconds=60)
    now = timezone.now()
    for i, ms in enumerate([100, 200, 300, 400, 1000]):
        CheckResult.objects.create(
            monitor=m, checked_at=now - timedelta(seconds=10 + i), is_up=True, response_time_ms=ms
        )
    Monitor.objects.filter(pk=m.pk).update(last_checked_at=now - timedelta(minutes=10))  # long overdue
    text = metrics(client, settings)
    assert 'uptime_queue_length{queue="checks.eu"} 42' in text
    assert "uptime_checks_last_1m 5" in text
    assert 'uptime_response_time_ms{quantile="0.5"} 300' in text
    assert 'uptime_response_time_ms{quantile="0.95"} 880' in text  # interpolated: 400 + 0.8*(1000-400)
    assert "uptime_overdue_checks 1" in text


def test_a_broker_outage_does_not_break_the_metrics_endpoint(client, user, settings, monkeypatch):
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    assert loadstats.queue_lengths() == {}
    assert "uptime_checks_last_1m" in metrics(client, settings)


def test_a_fresh_monitor_is_not_counted_as_overdue(user):
    Monitor.objects.create(
        user=user, name="a", url="https://a.example.com", last_checked_at=timezone.now() - timedelta(seconds=40)
    )
    assert loadstats.overdue_checks() == 0


def test_percentiles_ignore_maintenance_and_untimed_checks(user):
    m = Monitor.objects.create(user=user, name="a", url="https://a.example.com")
    now = timezone.now()
    CheckResult.objects.create(monitor=m, checked_at=now, is_up=True, response_time_ms=100)
    CheckResult.objects.create(monitor=m, checked_at=now, is_up=True, response_time_ms=99999, in_maintenance=True)
    CheckResult.objects.create(monitor=m, checked_at=now, is_up=False)
    assert loadstats.response_percentiles() == (100, 100)
    CheckResult.objects.all().delete()
    assert loadstats.response_percentiles() is None


def test_load_report_prints_throughput_and_backlog(user, capsys, monkeypatch):
    monkeypatch.setattr(loadstats, "queue_lengths", lambda: {"checks.eu": 3})
    m = Monitor.objects.create(user=user, name="a", url="https://a.example.com")
    CheckResult.objects.create(
        monitor=m, checked_at=timezone.now() - timedelta(minutes=2), is_up=True, response_time_ms=50
    )
    call_command("load_report", "--minutes", "5")
    out = capsys.readouterr().out
    assert "checks/min" in out and "overdue" in out and "checks.eu" in out


# ---------------- seed_load ----------------
def test_seed_load_creates_monitors_spread_over_hosts_and_is_idempotent(db):
    call_command("seed_load", "--monitors", "120", "--hosts", "5")
    ms = Monitor.objects.filter(name__startswith="load-")
    assert ms.count() == 120
    hosts = {m.url.split("//")[1].split(":")[0] for m in ms}
    assert hosts == {f"fake-0{i}" for i in range(5)}
    assert len({m.url for m in ms}) == 120  # unique (user, url)
    call_command("seed_load", "--monitors", "150", "--hosts", "5")  # top up, don't duplicate
    assert Monitor.objects.filter(name__startswith="load-").count() == 150


def test_seed_load_clean_removes_only_load_monitors(db, user):
    keep = Monitor.objects.create(user=user, name="real", url="https://real.example.com")
    call_command("seed_load", "--monitors", "10")
    call_command("seed_load", "--clean")
    assert list(Monitor.objects.all()) == [keep]


def test_seed_load_rejects_nonsense(db):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("seed_load", "--monitors", "0")


# ---------------- the fake target ----------------
def load_fake():
    spec = importlib.util.spec_from_file_location("fake_target", ROOT / "loadtest" / "fake_target.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fake_target_has_random_latency_and_about_five_percent_errors(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    fake = load_fake()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(
        fake, "asyncio", SimpleNamespace(sleep=fake_sleep)
    )  # only the fake server's sleeps, not aiohttp's
    fake.rng.seed(1)

    async def go():
        async with TestClient(TestServer(fake.make_app())) as client:
            codes = []
            for _ in range(600):
                try:
                    r = await client.get("/m/x")
                    codes.append(r.status)
                except Exception:
                    codes.append(0)
            return codes

    codes = asyncio.run(go())
    assert all(0.05 <= s <= 3.0 for s in sleeps) and max(sleeps) - min(sleeps) > 2  # spread over the whole range
    bad = sum(1 for c in codes if c != 200)
    assert 0.02 * 600 <= bad <= 0.09 * 600  # ~5%
    assert {c for c in codes if c} <= {200, 500, 503}


def test_the_load_compose_file_adds_the_fake_server_with_fifty_host_aliases_and_allows_private_targets():
    compose = yaml.safe_load((ROOT / "docker-compose.load.yml").read_text())
    aliases = compose["services"]["fake-target"]["networks"]["default"]["aliases"]
    assert aliases == [f"fake-{i:02d}" for i in range(50)]
    for svc in ("web", "worker", "worker-eu", "worker-us", "worker-asia", "beat"):
        assert compose["services"][svc]["environment"]["ALLOW_PRIVATE_TARGETS"] == "1"
