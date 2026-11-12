from datetime import timedelta

import pytest
from django.utils import timezone

from monitors.models import CheckResult, Incident, Monitor

STATUSES = {"up", "down", "unknown", "maintenance", "paused"}


def test_anonymous_is_401(client, db):
    assert client.get("/api/v1/dashboard").status_code == 401


def test_shape_for_a_user_with_data(client, user):
    up = Monitor.objects.create(user=user, name="Up", url="https://up.com")
    down = Monitor.objects.create(user=user, name="Down", url="https://down.com")
    Monitor.objects.create(user=user, name="New", url="https://new.com")
    now = timezone.now()
    CheckResult.objects.create(monitor=up, checked_at=now, is_up=True, response_time_ms=120)
    CheckResult.objects.create(monitor=down, checked_at=now, is_up=False)
    Incident.objects.create(monitor=down, started_at=now - timedelta(minutes=7))
    client.force_login(user)
    body = client.get("/api/v1/dashboard").json()
    assert body["summary"] == {"up": 1, "down": 1, "unknown": 1, "maintenance": 0, "paused": 0, "open_incidents": 1}
    rows = {r["name"]: r for r in body["monitors"]}
    assert set(rows) == {"Up", "Down", "New"}
    for r in rows.values():
        assert r["status"] in STATUSES and len(r["spark"]) == 24
    assert rows["Down"]["down_minutes"] in (6, 7) and rows["Up"]["down_minutes"] is None
    assert rows["Up"]["uptime_24h"] == 100.0 and rows["Up"]["spark"][-1] == 120
    assert rows["New"]["uptime_24h"] is None and rows["New"]["last_checked_at"] is None
    assert rows["New"]["spark"] == [None] * 24


def test_only_own_monitors(client, user, other_user):
    Monitor.objects.create(user=other_user, name="Secret", url="https://secret.com")
    client.force_login(user)
    body = client.get("/api/v1/dashboard").json()
    assert body["monitors"] == []
    assert body["summary"] == {"up": 0, "down": 0, "unknown": 0, "maintenance": 0, "paused": 0, "open_incidents": 0}


@pytest.mark.parametrize("n", [2, 15])
def test_query_count_does_not_grow_with_monitors(client, user, n, django_assert_max_num_queries):
    now = timezone.now()
    for i in range(n):
        m = Monitor.objects.create(user=user, name=f"m{i}", url=f"https://m{i}.com")
        CheckResult.objects.create(monitor=m, checked_at=now, is_up=bool(i % 2), response_time_ms=10)
        if not i % 2:
            Incident.objects.create(monitor=m, started_at=now)
    client.force_login(user)
    with django_assert_max_num_queries(6):
        assert client.get("/api/v1/dashboard").status_code == 200
