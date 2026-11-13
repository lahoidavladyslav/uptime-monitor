import pytest
from django.test import Client

from api.errors import field_errors_from_django
from monitors.forms import MonitorForm
from monitors.models import CheckResult, Monitor

VALID = {
    "name": "Мій сайт",
    "url": "https://new.example.org",
    "interval_seconds": 300,
    "is_active": True,
    "is_public": False,
    "public_title": "",
    "public_logo_url": "",
    "http_method": "GET",
    "request_headers": "",
    "request_body": "",
    "check_type": "http_status",
    "check_config": {},
}


def post(client, body):
    return client.post("/api/v1/monitors", body, content_type="application/json")


def put(client, pk, body):
    return client.put(f"/api/v1/monitors/{pk}", body, content_type="application/json")


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Site", url="https://example.com")


@pytest.fixture
def foreign(other_user):
    return Monitor.objects.create(user=other_user, name="Чужий", url="https://secret.test")


# ---------------- create ----------------
def test_create_saves_the_monitor_for_the_caller_and_parses_headers(auth_client, user):
    r = post(auth_client, {**VALID, "request_headers": "Authorization: Bearer x\nAccept: application/json"})
    assert r.status_code == 201
    m = Monitor.objects.get(pk=r.json()["id"])
    assert m.user == user and m.request_headers == {"Authorization": "Bearer x", "Accept": "application/json"}
    assert (
        r.json()["request_headers"] == "Authorization: Bearer x\nAccept: application/json"
    )  # lines again, like the form


def test_a_public_monitor_gets_a_slug_and_a_private_one_does_not(auth_client):
    pub = post(auth_client, {**VALID, "is_public": True, "public_title": "Публічно"}).json()
    priv = post(auth_client, {**VALID, "url": "https://other.example.org"}).json()
    assert Monitor.objects.get(pk=pub["id"]).public_slug and not Monitor.objects.get(pk=priv["id"]).public_slug


def test_create_requires_login_and_csrf(client, db, user):
    assert post(client, VALID).status_code == 401
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    assert post(strict, VALID).status_code == 403
    assert not Monitor.objects.exists()


@pytest.mark.parametrize(
    "override,field",
    [
        ({"url": "ftp://x.com"}, "url"),
        ({"url": "http://127.0.0.1/"}, "url"),
        ({"url": "http://169.254.169.254/latest/meta-data/"}, "url"),
        ({"url": "https://example.com"}, "url"),  # duplicate of the fixture monitor
        ({"name": ""}, "name"),
        ({"interval_seconds": 7}, "interval_seconds"),
        ({"check_type": "keyword", "check_config": {}}, "check_config"),
        ({"check_config": {"expected_statuses": ["abc"]}}, "check_config"),
        ({"check_type": "telnet"}, "check_type"),
        ({"check_type": "ssl_expiry", "url": "http://plain.example.org"}, "url"),
        ({"request_headers": "Host: x"}, "request_headers"),
        ({"http_method": "HEAD", "check_type": "keyword", "check_config": {"keyword": "hello"}}, "check_type"),
        ({"http_method": "GET", "request_body": "x"}, "request_body"),
    ],
)
def test_invalid_input_is_a_422_with_exactly_the_forms_errors(auth_client, user, mon, override, field):
    body = {**VALID, **override}
    r = post(auth_client, body)
    assert r.status_code == 422 and r.json()["errors"][field]
    assert r.json()["errors"] == field_errors_from_django(MonitorForm(body, user=user).errors)
    assert Monitor.objects.count() == 1  # nothing was created


def test_the_same_url_is_fine_for_another_user(auth_client, other_user):
    Monitor.objects.create(user=other_user, name="x", url="https://new.example.org")
    assert post(auth_client, VALID).status_code == 201


# ---------------- config + update ----------------
def test_config_returns_every_editable_field_and_nothing_secret_of_others(auth_client, mon, foreign):
    body = auth_client.get(f"/api/v1/monitors/{mon.pk}/config").json()
    assert set(body) == set(VALID) | {"id", "public_slug"}
    assert body["url"] == mon.url
    assert auth_client.get(f"/api/v1/monitors/{foreign.pk}/config").status_code == 404


def test_put_updates_and_keeping_the_same_url_is_not_a_duplicate(auth_client, mon):
    r = put(auth_client, mon.pk, {**VALID, "url": mon.url, "name": "Renamed", "interval_seconds": 60})
    assert r.status_code == 200
    mon.refresh_from_db()
    assert (mon.name, mon.interval_seconds) == ("Renamed", 60)


def test_put_to_another_of_my_urls_is_a_duplicate(auth_client, user, mon):
    Monitor.objects.create(user=user, name="B", url="https://b.example.com")
    r = put(auth_client, mon.pk, {**VALID, "url": "https://b.example.com"})
    assert r.status_code == 422 and r.json()["errors"]["url"]
    mon.refresh_from_db()
    assert mon.url == "https://example.com"


def test_a_failed_update_changes_nothing(auth_client, mon):
    r = put(auth_client, mon.pk, {**VALID, "name": "Changed", "url": "http://10.0.0.1/"})
    assert r.status_code == 422
    mon.refresh_from_db()
    assert mon.name == "Site"


def test_the_public_slug_is_stable_and_going_private_closes_the_page_at_once(auth_client, client, mon):
    put(auth_client, mon.pk, {**VALID, "url": mon.url, "is_public": True})
    mon.refresh_from_db()
    slug = mon.public_slug
    assert client.get(f"/status/{slug}/").status_code == 200
    put(auth_client, mon.pk, {**VALID, "url": mon.url, "is_public": False})
    put(auth_client, mon.pk, {**VALID, "url": mon.url, "is_public": True})
    mon.refresh_from_db()
    assert mon.public_slug == slug  # never regenerated
    put(auth_client, mon.pk, {**VALID, "url": mon.url, "is_public": False})
    assert client.get(f"/status/{slug}/").status_code == 404  # no stale cache


def test_put_on_a_foreign_monitor_is_404_and_changes_nothing(auth_client, foreign):
    assert put(auth_client, foreign.pk, {**VALID, "url": foreign.url, "name": "Hacked"}).status_code == 404
    foreign.refresh_from_db()
    assert foreign.name == "Чужий"


# ---------------- delete ----------------
def test_delete_removes_the_monitor_and_its_history(auth_client, mon):
    from django.utils import timezone

    CheckResult.objects.create(monitor=mon, checked_at=timezone.now(), is_up=True)
    r = auth_client.delete(f"/api/v1/monitors/{mon.pk}")
    assert r.status_code == 200 and r.json() == {"status": "deleted"}
    assert not Monitor.objects.filter(pk=mon.pk).exists() and not CheckResult.objects.exists()


def test_delete_foreign_is_404_and_anonymous_is_401(auth_client, client, foreign, mon):
    assert auth_client.delete(f"/api/v1/monitors/{foreign.pk}").status_code == 404
    assert Monitor.objects.filter(pk=foreign.pk).exists()
    client.logout()
    assert client.delete(f"/api/v1/monitors/{mon.pk}").status_code == 401


def test_a_new_monitor_shows_up_on_the_dashboard(auth_client):
    post(auth_client, VALID)
    assert [m["name"] for m in auth_client.get("/api/v1/dashboard").json()["monitors"]] == ["Мій сайт"]


@pytest.mark.parametrize(
    "kind,config",
    [
        ("keyword", {"keyword": "Welcome", "must_contain": True}),
        ("json_path", {"path": "status.db", "equals": "ok"}),
        ("http_status", {"expected_statuses": [200, "300-399"]}),
        ("ssl_expiry", {"min_days_left": 7}),
    ],
)
def test_every_check_type_can_be_created_and_read_back(auth_client, kind, config):
    r = post(auth_client, {**VALID, "check_type": kind, "check_config": config})
    assert r.status_code == 201, r.content
    back = auth_client.get(f"/api/v1/monitors/{r.json()['id']}/config").json()
    assert (back["check_type"], back["check_config"]) == (kind, config)


def test_the_stored_config_is_the_normalised_one(auth_client):
    r = post(auth_client, {**VALID, "check_type": "keyword", "check_config": {"keyword": "x"}})
    assert r.json()["check_config"] == {"keyword": "x", "must_contain": True}
