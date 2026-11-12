import os
import re
import secrets
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from monitors.models import CheckResult, HourlyStat, Incident, Monitor
from monitors.services import long_term_uptime
from monitors.tasks import cleanup_old_checks, rollup_hourly, rollup_range
from reports.models import ReportExport

ROOT = Path(__file__).resolve().parents[2]


def add_check(monitor, minutes_ago=0, up=True, ms=100, days_ago=0):
    return CheckResult.objects.create(
        monitor=monitor,
        checked_at=timezone.now() - timedelta(minutes=minutes_ago, days=days_ago),
        is_up=up,
        response_time_ms=ms if up else None,
    )


# ---------------- /healthz/ ----------------
def test_healthz_ok_without_login(client, db):
    r = client.get(reverse("healthz"))
    assert r.status_code == 200
    assert r.json() == {"db": "ok", "cache": "ok", "checks": "ok", "status": "ok"}
    assert "no-cache" in r["Cache-Control"] or "no-store" in r["Cache-Control"]


def test_healthz_503_when_db_is_down(client, db, monkeypatch):
    class DeadConnection:
        def cursor(self):
            raise RuntimeError("db down")

    monkeypatch.setattr("monitors.ops.connection", DeadConnection())
    r = client.get(reverse("healthz"))
    assert r.status_code == 503 and r.json()["db"] == "error"


def test_healthz_503_when_cache_is_down(client, db, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr("monitors.ops.cache.set", boom)
    r = client.get(reverse("healthz"))
    assert r.status_code == 503 and r.json()["cache"] == "error"


def test_healthz_flags_stale_pipeline_but_stays_200(client, monitor):
    Monitor.objects.filter(pk=monitor.pk).update(last_checked_at=timezone.now() - timedelta(minutes=30))
    r = client.get(reverse("healthz"))
    assert r.status_code == 200 and r.json()["checks"] == "stale"  # a dead worker must not restart web
    Monitor.objects.filter(pk=monitor.pk).update(last_checked_at=timezone.now())
    assert client.get(reverse("healthz")).json()["checks"] == "ok"


def test_healthz_not_stale_when_no_active_monitors(client, db):
    assert client.get(reverse("healthz")).json()["checks"] == "ok"


# ---------------- /metrics/ ----------------
def test_metrics_disabled_without_token(client, db, settings):
    settings.METRICS_TOKEN = ""
    assert client.get(reverse("metrics")).status_code == 404


def test_metrics_requires_the_right_bearer_token(client, db, settings):
    settings.METRICS_TOKEN = "s3cret"
    assert client.get(reverse("metrics")).status_code == 401
    assert client.get(reverse("metrics"), HTTP_AUTHORIZATION="Bearer nope").status_code == 401
    assert client.get(reverse("metrics"), HTTP_AUTHORIZATION="Bearer s3cret").status_code == 200


def metric(text, name, **labels):
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        n, _, value = line.rpartition(" ")
        want = name + ("{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else "")
        if n == want:
            return float(value)
    raise AssertionError(f"{want} not found in:\n{text}")


def test_metrics_values(client, user, settings):
    settings.METRICS_TOKEN = "t"
    up = Monitor.objects.create(user=user, name="u", url="https://u.com")
    down = Monitor.objects.create(user=user, name="d", url="https://d.com")
    Monitor.objects.create(user=user, name="never", url="https://n.com")
    Monitor.objects.create(user=user, name="paused", url="https://p.com", is_active=False)
    add_check(up)
    add_check(down, up=False)
    Incident.objects.create(monitor=down, started_at=timezone.now())
    ReportExport.objects.create(user=user, period_start="2026-01-01", period_end="2026-01-02", status="pending")
    Monitor.objects.filter(pk=up.pk).update(ssl_expires_at=timezone.now() + timedelta(days=5))

    text = client.get(reverse("metrics"), HTTP_AUTHORIZATION="Bearer t").content.decode()
    assert metric(text, "uptime_monitors", state="up") == 1
    assert metric(text, "uptime_monitors", state="down") == 1
    assert metric(text, "uptime_monitors", state="unknown") == 1
    assert metric(text, "uptime_monitors_inactive") == 1
    assert metric(text, "uptime_open_incidents") == 1
    assert metric(text, "uptime_checks_last_5m") == 2
    assert metric(text, "uptime_report_exports", status="pending") == 1
    assert metric(text, "uptime_report_exports", status="done") == 0
    assert metric(text, "uptime_ssl_expiring_14d") == 1
    assert "# TYPE uptime_open_incidents gauge" in text


def test_metrics_scheduler_lag_grows_when_checks_stop(client, monitor, settings):
    settings.METRICS_TOKEN = "t"
    Monitor.objects.filter(pk=monitor.pk).update(
        last_checked_at=timezone.now() - timedelta(minutes=20), interval_seconds=60
    )
    text = client.get(reverse("metrics"), HTTP_AUTHORIZATION="Bearer t").content.decode()
    assert 18 * 60 <= metric(text, "uptime_scheduler_lag_seconds") <= 20 * 60  # due 19 min ago


def test_metrics_query_count_is_constant(client, user, settings, django_assert_max_num_queries):
    settings.METRICS_TOKEN = "t"
    for i in range(15):
        m = Monitor.objects.create(user=user, name=f"m{i}", url=f"https://m{i}.com")
        add_check(m)
    with django_assert_max_num_queries(9):
        assert client.get(reverse("metrics"), HTTP_AUTHORIZATION="Bearer t").status_code == 200


# ---------------- queues ----------------
def test_tasks_are_routed_to_separate_queues():
    from config.celery import app

    routes = app.conf.task_routes
    assert routes["reports.tasks.generate_report"] == {"queue": "reports"}
    assert routes["monitors.tasks.send_incident_notification"] == {"queue": "notifications"}
    assert routes["monitors.tasks.send_ssl_alert"] == {"queue": "notifications"}
    assert app.conf.task_default_queue == "checks"  # check loop stays on its own queue


# ---------------- hourly rollups ----------------
def test_rollup_aggregates_per_monitor_hour_and_is_idempotent(monitor, user):
    other = Monitor.objects.create(user=user, name="o", url="https://o.com")
    base = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    for minute, up, ms in [(1, True, 100), (2, True, 300), (3, False, None), (4, True, None)]:
        CheckResult.objects.create(
            monitor=monitor, checked_at=base + timedelta(minutes=minute), is_up=up, response_time_ms=ms
        )
    CheckResult.objects.create(monitor=other, checked_at=base + timedelta(minutes=5), is_up=True, response_time_ms=50)

    rollup_range(base, base + timedelta(hours=1))
    rollup_range(base, base + timedelta(hours=1))  # again: same result, no duplicates
    stat = HourlyStat.objects.get(monitor=monitor, hour=base)
    assert (stat.total, stat.up, stat.rt_sum, stat.rt_count) == (4, 3, 400, 2)
    assert HourlyStat.objects.count() == 2
    assert HourlyStat.objects.get(monitor=other).rt_sum == 50


def test_rollup_hourly_task_picks_up_late_rows(monitor):
    now = timezone.now()
    add_check(monitor, minutes_ago=0)
    rollup_hourly()
    hour = now.replace(minute=0, second=0, microsecond=0)
    assert HourlyStat.objects.get(monitor=monitor, hour=hour).total == 1
    add_check(monitor, minutes_ago=0)
    rollup_hourly()  # recompute, not append
    assert HourlyStat.objects.get(monitor=monitor, hour=hour).total == 2


def test_long_term_uptime_survives_raw_cleanup(monitor, settings):
    settings.CLEANUP_BATCH_SIZE = 100
    old = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=45)
    for i in range(10):
        CheckResult.objects.create(monitor=monitor, checked_at=old + timedelta(minutes=i), is_up=i < 8)  # 80%
    rollup_range(old, old + timedelta(hours=1))
    assert cleanup_old_checks() == 10  # raw rows are older than 30 days -> gone
    assert not CheckResult.objects.exists()
    assert long_term_uptime(monitor, 90) == 80.0  # ...but the history is still there
    assert long_term_uptime(monitor, 30) is None  # the data is older than 30d


def test_cleanup_prunes_very_old_rollups(monitor, settings):
    old = timezone.now() - timedelta(days=settings.ROLLUP_RETENTION_DAYS + 5)
    HourlyStat.objects.create(monitor=monitor, hour=old, total=1, up=1)
    fresh = HourlyStat.objects.create(monitor=monitor, hour=timezone.now() - timedelta(days=60), total=1, up=1)
    cleanup_old_checks()
    assert list(HourlyStat.objects.all()) == [fresh]


def test_backfill_command_fills_gaps_without_overwriting(monitor):
    add_check(monitor, days_ago=3)
    add_check(monitor, days_ago=3, minutes_ago=1)
    call_command("backfill_rollups")
    assert HourlyStat.objects.get().total == 2
    HourlyStat.objects.update(total=999)  # pretend this hour's raw data was already cleaned up
    add_check(monitor, days_ago=2)
    call_command("backfill_rollups")
    assert sorted(HourlyStat.objects.values_list("total", flat=True)) == [1, 999]  # existing row untouched


def test_detail_page_shows_long_term_uptime(auth_client, monitor):
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)
    HourlyStat.objects.create(monitor=monitor, hour=hour, total=10, up=9)
    html = auth_client.get(reverse("monitor_detail", args=[monitor.pk])).content.decode()
    assert re.search(r"90[.,]0%", html)


# ---------------- production settings fail fast ----------------
def run_settings(env):
    clean = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "ALLOWED_HOSTS", "DJANGO_ENV", "DEBUG")}
    return subprocess.run(
        [sys.executable, "-c", "import config.settings as s; print(s.DEBUG, s.SESSION_COOKIE_SECURE)"],
        cwd=ROOT, env={**clean, **env}, capture_output=True, text=True,
    )  # fmt: skip


def test_production_refuses_to_start_without_secret_key_or_hosts():
    r = run_settings({"DJANGO_ENV": "production"})
    assert r.returncode != 0 and "SECRET_KEY" in r.stderr
    r = run_settings({"DJANGO_ENV": "production", "SECRET_KEY": "x" * 60})
    assert r.returncode != 0 and "ALLOWED_HOSTS" in r.stderr


def test_production_is_hardened():
    r = run_settings({"DJANGO_ENV": "production", "SECRET_KEY": "x" * 60, "ALLOWED_HOSTS": "example.com", "DEBUG": "1"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["False", "True"]  # DEBUG is forced off, cookies are Secure


@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
def test_django_deploy_check_has_no_warnings():
    env = {
        "DJANGO_ENV": "production",
        "SECRET_KEY": secrets.token_urlsafe(60),
        "ALLOWED_HOSTS": "example.com",
        "DATABASE_URL": "postgres://localhost/uptime",
    }
    clean = {k: v for k, v in os.environ.items() if k not in env}
    r = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy"],
        cwd=ROOT,
        env={**clean, **env},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "WARNINGS" not in r.stdout + r.stderr, r.stdout + r.stderr
