from datetime import timedelta

import pytest
from django.utils import timezone

from monitors.models import CheckResult, Incident, MaintenanceWindow, Monitor


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Сайт", url="https://example.com")


@pytest.fixture
def foreign(other_user):
    return Monitor.objects.create(user=other_user, name="Чужий", url="https://secret.test")


def seed_checks(m, n=30, failing_every=5):
    now = timezone.now()
    for i in range(n):
        CheckResult.objects.create(
            monitor=m,
            checked_at=now - timedelta(minutes=i),
            is_up=bool(i % failing_every),
            status_code=200 if i % failing_every else 503,
            response_time_ms=100 + i if i % failing_every else None,
            error=None if i % failing_every else "HTTP 503",
        )


# ---------------- isolation & errors ----------------
@pytest.mark.parametrize(
    "suffix", ["", "/stats?period=24h", "/series?period=24h", "/checks", "/incidents", "/maintenance"]
)
def test_foreign_monitor_is_a_404_in_the_common_error_shape(auth_client, foreign, suffix):
    r = auth_client.get(f"/api/v1/monitors/{foreign.pk}{suffix}")
    assert r.status_code == 404 and r.json() == {"message": "Не знайдено.", "errors": {}}


@pytest.mark.parametrize("suffix", ["", "/stats", "/series", "/checks", "/incidents"])
def test_anonymous_gets_401(client, mon, suffix):
    assert client.get(f"/api/v1/monitors/{mon.pk}{suffix}").status_code == 401


def test_unknown_monitor_is_404(auth_client):
    assert auth_client.get("/api/v1/monitors/999999").status_code == 404


# ---------------- detail ----------------
def test_detail_for_a_monitor_without_data_has_nulls_not_zeros(auth_client, mon):
    body = auth_client.get(f"/api/v1/monitors/{mon.pk}").json()
    assert body["status"] == "unknown" and body["last_checked_at"] is None
    assert body["uptime"] == {"h24": None, "d7": None, "d30": None, "d90": None}
    assert body["ssl_days_left"] is None and body["down_minutes"] is None
    assert body["url"] == "https://example.com"  # the owner may see it


def test_detail_reports_status_uptime_ssl_and_open_incident(auth_client, mon):
    seed_checks(mon, n=10, failing_every=5)  # newest check (i=0) is a failure
    Monitor.objects.filter(pk=mon.pk).update(ssl_expires_at=timezone.now() + timedelta(days=20, hours=1))
    Incident.objects.create(monitor=mon, started_at=timezone.now() - timedelta(minutes=42, seconds=5))
    body = auth_client.get(f"/api/v1/monitors/{mon.pk}").json()
    assert body["status"] == "down" and body["down_minutes"] == 42
    assert body["uptime"]["h24"] == 80.0
    assert body["ssl_days_left"] == 20


def test_detail_status_reflects_maintenance_and_pause(auth_client, mon):
    now = timezone.now()
    MaintenanceWindow.objects.create(monitor=mon, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    assert auth_client.get(f"/api/v1/monitors/{mon.pk}").json()["status"] == "maintenance"
    mon.maintenance_windows.all().delete()
    Monitor.objects.filter(pk=mon.pk).update(is_active=False)
    assert auth_client.get(f"/api/v1/monitors/{mon.pk}").json()["status"] == "paused"


# ---------------- stats / series ----------------
def test_stats_match_the_database_aggregates(auth_client, mon):
    seed_checks(mon, n=10, failing_every=5)  # 2 failures of 10
    body = auth_client.get(f"/api/v1/monitors/{mon.pk}/stats?period=24h").json()
    assert (body["total"], body["up"], body["down"], body["uptime"]) == (10, 8, 2, 80.0)
    assert body["rt_min"] == 101 and body["rt_max"] == 109


def test_stats_without_checks_are_null_not_a_crash(auth_client, mon):
    body = auth_client.get(f"/api/v1/monitors/{mon.pk}/stats?period=7d").json()
    assert body == {"total": 0, "up": 0, "down": 0, "uptime": None, "rt_avg": None, "rt_min": None, "rt_max": None}


def test_unknown_period_is_a_422(auth_client, mon):
    r = auth_client.get(f"/api/v1/monitors/{mon.pk}/stats?period=1y")
    assert r.status_code == 422 and "period" in r.json()["errors"]


def test_series_marks_failures_as_gaps(auth_client, mon):
    seed_checks(mon, n=6, failing_every=5)
    points = auth_client.get(f"/api/v1/monitors/{mon.pk}/series?period=24h").json()
    assert len(points) == 6
    assert sum(p["down"] for p in points) == 2 and all((p["ms"] is None) == p["down"] for p in points)


def test_series_without_checks_is_empty(auth_client, mon):
    assert auth_client.get(f"/api/v1/monitors/{mon.pk}/series?period=7d").json() == []


# ---------------- checks ----------------
def test_checks_are_paginated_newest_first(auth_client, mon):
    seed_checks(mon, n=45)
    r = auth_client.get(f"/api/v1/monitors/{mon.pk}/checks?page=2&page_size=20").json()
    assert (r["total"], r["page"], r["pages"], len(r["items"])) == (45, 2, 3, 20)
    times = [i["checked_at"] for i in r["items"]]
    assert times == sorted(times, reverse=True)
    assert set(r["items"][0]) == {"id", "checked_at", "status_code", "response_time_ms", "is_up", "error"}


def test_only_failed_filters_in_the_database(auth_client, mon):
    seed_checks(mon, n=20, failing_every=5)
    r = auth_client.get(f"/api/v1/monitors/{mon.pk}/checks?only_failed=true").json()
    assert r["total"] == 4 and all(not i["is_up"] and i["error"] == "HTTP 503" for i in r["items"])


@pytest.mark.parametrize("qs", ["page=999", "page=0", "page=-3", "page_size=1000", "page_size=0"])
def test_out_of_range_paging_is_clamped_never_a_500(auth_client, mon, qs):
    seed_checks(mon, n=5)
    r = auth_client.get(f"/api/v1/monitors/{mon.pk}/checks?{qs}")
    assert r.status_code == 200 and len(r.json()["items"]) <= 100


def test_empty_checks_page(auth_client, mon):
    assert auth_client.get(f"/api/v1/monitors/{mon.pk}/checks").json() == {
        "items": [], "total": 0, "page": 1, "pages": 1,
    }  # fmt: skip


# ---------------- incidents ----------------
def test_incidents_list_open_and_resolved(auth_client, mon):
    now = timezone.now()
    Incident.objects.create(
        monitor=mon, started_at=now - timedelta(hours=3), resolved_at=now - timedelta(hours=2), first_error="boom"
    )
    Incident.objects.create(monitor=mon, started_at=now - timedelta(minutes=10), first_error="again")
    items = auth_client.get(f"/api/v1/monitors/{mon.pk}/incidents").json()
    assert [i["first_error"] for i in items] == ["again", "boom"]  # newest first
    assert items[0]["resolved_at"] is None and items[0]["minutes"] == 10
    assert items[1]["minutes"] == 60


def test_incidents_are_capped_at_50(auth_client, mon):
    now = timezone.now()
    for i in range(55):
        Incident.objects.create(
            monitor=mon, started_at=now - timedelta(hours=2 * i + 2), resolved_at=now - timedelta(hours=2 * i + 1)
        )
    assert len(auth_client.get(f"/api/v1/monitors/{mon.pk}/incidents").json()) == 50


# ---------------- queries ----------------
def test_read_endpoints_use_a_constant_number_of_queries(auth_client, mon, django_assert_max_num_queries):
    seed_checks(mon, n=60)
    for suffix in ("", "/stats?period=7d", "/series?period=7d", "/checks?page_size=50", "/incidents"):
        with django_assert_max_num_queries(8):
            assert auth_client.get(f"/api/v1/monitors/{mon.pk}{suffix}").status_code == 200


# ======================= actions =======================
@pytest.fixture
def queued(monkeypatch):
    calls = {"check": [], "ssl": []}
    monkeypatch.setattr(
        "api.routers.monitors.check_monitor.delay", lambda pk, force=False: calls["check"].append((pk, force))
    )
    monkeypatch.setattr("api.routers.monitors.check_ssl.delay", lambda pk: calls["ssl"].append(pk))
    return calls


def post(client, path, data=None):
    return client.post(f"/api/v1/monitors{path}", data or {}, content_type="application/json")


def test_check_now_queues_one_forced_check(auth_client, mon, queued):
    r = post(auth_client, f"/{mon.pk}/check")
    assert r.status_code == 202 and r.json() == {"status": "queued"}
    assert queued["check"] == [(mon.pk, True)] and queued["ssl"] == [mon.pk]  # https -> SSL refresh too


def test_check_now_skips_ssl_for_plain_http(auth_client, user, queued):
    m = Monitor.objects.create(user=user, name="Plain", url="http://plain.example.com")
    post(auth_client, f"/{m.pk}/check")
    assert queued["check"] == [(m.pk, True)] and queued["ssl"] == []


def test_a_second_click_inside_the_cooldown_is_429_and_queues_nothing(auth_client, mon, queued):
    assert post(auth_client, f"/{mon.pk}/check").status_code == 202
    r = post(auth_client, f"/{mon.pk}/check")
    assert r.status_code == 429 and r.json()["message"] and len(queued["check"]) == 1


def test_check_now_during_maintenance_is_409(auth_client, mon, queued):
    now = timezone.now()
    MaintenanceWindow.objects.create(monitor=mon, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    r = post(auth_client, f"/{mon.pk}/check")
    assert r.status_code == 409 and queued["check"] == []


def test_check_now_on_a_foreign_monitor_is_404_and_queues_nothing(auth_client, foreign, queued):
    assert post(auth_client, f"/{foreign.pk}/check").status_code == 404 and queued["check"] == []


def test_check_now_requires_login(client, mon, queued):
    assert post(client, f"/{mon.pk}/check").status_code == 401 and queued["check"] == []


# ---------------- maintenance ----------------
def window(start_h=1, end_h=3, reason="Оновлення"):
    now = timezone.now()
    return {
        "starts_at": (now + timedelta(hours=start_h)).isoformat(),
        "ends_at": (now + timedelta(hours=end_h)).isoformat(),
        "reason": reason,
    }


def test_create_maintenance_window_then_list_it(auth_client, mon):
    r = post(auth_client, f"/{mon.pk}/maintenance", window())
    assert r.status_code == 201 and r.json()["reason"] == "Оновлення"
    listed = auth_client.get(f"/api/v1/monitors/{mon.pk}/maintenance").json()
    assert [w["id"] for w in listed] == [r.json()["id"]]


def test_finished_windows_are_not_listed(auth_client, mon):
    now = timezone.now()
    MaintenanceWindow.objects.create(monitor=mon, starts_at=now - timedelta(hours=3), ends_at=now - timedelta(hours=1))
    assert auth_client.get(f"/api/v1/monitors/{mon.pk}/maintenance").json() == []


@pytest.mark.parametrize(
    "start_h,end_h,fragment",
    [(3, 1, "пізніше"), (-5, -2, "минуло"), (1, 24 * 31 + 2, "30 днів")],
)
def test_invalid_windows_are_422_with_the_forms_messages(auth_client, mon, start_h, end_h, fragment):
    r = post(auth_client, f"/{mon.pk}/maintenance", window(start_h, end_h))
    assert r.status_code == 422
    assert fragment in " ".join(sum(r.json()["errors"].values(), []))
    assert not mon.maintenance_windows.exists()


def test_missing_or_garbage_dates_are_422_field_errors(auth_client, mon):
    r = post(auth_client, f"/{mon.pk}/maintenance", {"starts_at": "not-a-date", "ends_at": "", "reason": ""})
    assert r.status_code == 422 and {"starts_at", "ends_at"} <= set(r.json()["errors"])


def test_delete_window(auth_client, mon):
    w = MaintenanceWindow.objects.create(
        monitor=mon, starts_at=timezone.now(), ends_at=timezone.now() + timedelta(hours=1)
    )
    r = auth_client.delete(f"/api/v1/monitors/{mon.pk}/maintenance/{w.pk}")
    assert r.status_code == 200 and not MaintenanceWindow.objects.filter(pk=w.pk).exists()


def test_window_of_another_monitor_or_user_cannot_be_deleted(auth_client, user, mon, foreign):
    mine_other = Monitor.objects.create(user=user, name="Other", url="https://other.example.com")
    theirs = MaintenanceWindow.objects.create(
        monitor=foreign, starts_at=timezone.now(), ends_at=timezone.now() + timedelta(hours=1)
    )
    cross = MaintenanceWindow.objects.create(
        monitor=mine_other, starts_at=timezone.now(), ends_at=timezone.now() + timedelta(hours=1)
    )
    assert auth_client.delete(f"/api/v1/monitors/{mon.pk}/maintenance/{theirs.pk}").status_code == 404
    assert auth_client.delete(f"/api/v1/monitors/{foreign.pk}/maintenance/{theirs.pk}").status_code == 404
    assert auth_client.delete(f"/api/v1/monitors/{mon.pk}/maintenance/{cross.pk}").status_code == 404  # wrong monitor
    assert MaintenanceWindow.objects.count() == 2


def test_unsafe_methods_enforce_csrf(user, mon):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert strict.post(f"/api/v1/monitors/{mon.pk}/check", content_type="application/json").status_code == 403
    assert (
        strict.post(f"/api/v1/monitors/{mon.pk}/maintenance", window(), content_type="application/json").status_code
        == 403
    )
