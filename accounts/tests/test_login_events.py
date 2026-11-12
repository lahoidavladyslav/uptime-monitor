import logging
from datetime import timedelta

import pyotp
import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import LoginEvent, TOTPDevice
from accounts.tasks import cleanup_login_events

PW = "pw12345678"


def csrf_client(client):
    client.get("/api/v1/auth/csrf")
    return {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}


def api_login(client, username, password):
    return client.post(
        "/api/v1/auth/login",
        {"username": username, "password": password},
        content_type="application/json",
        **csrf_client(client),
    )


# ---------------- recording ----------------
def test_web_login_success_is_recorded_with_ip_and_user_agent(client, user):
    client.post(
        reverse("login"),
        {"username": "alice", "password": PW},
        REMOTE_ADDR="198.51.100.4",
        HTTP_USER_AGENT="Mozilla/5.0 X",
    )
    ev = LoginEvent.objects.get(user=user)
    assert (ev.kind, ev.ip, ev.user_agent) == ("login", "198.51.100.4", "Mozilla/5.0 X")


def test_wrong_password_is_recorded_against_the_account(client, user):
    client.post(reverse("login"), {"username": "alice", "password": "nope"}, REMOTE_ADDR="198.51.100.4")
    assert list(LoginEvent.objects.values_list("user_id", "kind")) == [(user.pk, "failed")]


def test_api_login_goes_through_the_same_recording(client, user):
    assert api_login(client, "alice", "bad").status_code == 422
    assert api_login(client, "alice", PW).status_code == 200
    assert list(LoginEvent.objects.order_by("id").values_list("kind", flat=True)) == ["failed", "login"]


def test_unknown_username_is_not_stored_but_is_logged_without_the_name(client, db, caplog):
    caplog.set_level(logging.INFO, logger="security")
    client.post(reverse("login"), {"username": "hunter2-typed-in-wrong-box", "password": "x"})
    assert not LoginEvent.objects.exists()  # no row for strangers: bots would grow the table unboundedly
    rec = next(r for r in caplog.records if r.name == "security")
    assert rec.event == "login_failed" and rec.known_user is False
    assert "hunter2" not in str(rec.__dict__)  # people paste their password into the username box


def test_two_factor_failures_and_lockout_are_recorded(client, user):
    TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed=True)
    client.post(reverse("login"), {"username": "alice", "password": PW})
    assert not LoginEvent.objects.filter(kind="login").exists()  # password alone is not a login yet
    for _ in range(6):
        client.post(reverse("two_factor_verify"), {"code": "000000"})
    kinds = list(LoginEvent.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == ["two_factor_failed"] * 5 + ["two_factor_locked"]


def test_successful_2fa_login_is_recorded_once(client, user):
    device = TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed=True)
    client.post(reverse("login"), {"username": "alice", "password": PW})
    client.post(reverse("two_factor_verify"), {"code": pyotp.TOTP(device.secret).now()})
    assert list(LoginEvent.objects.values_list("kind", flat=True)) == ["login"]


def test_forwarded_ip_is_used_only_behind_our_proxy(client, user, settings):
    settings.TRUST_PROXY_HEADERS = True
    client.post(
        reverse("login"), {"username": "alice", "password": PW}, REMOTE_ADDR="10.0.0.2", HTTP_X_REAL_IP="203.0.113.7"
    )
    assert LoginEvent.objects.get().ip == "203.0.113.7"


def test_user_agent_is_truncated(client, user):
    client.post(reverse("login"), {"username": "alice", "password": PW}, HTTP_USER_AGENT="A" * 5000)
    assert len(LoginEvent.objects.get().user_agent) == 255


def test_events_are_logged_as_structured_records_without_pii(client, user, caplog):
    caplog.set_level(logging.INFO, logger="security")
    client.post(reverse("login"), {"username": "alice", "password": PW}, REMOTE_ADDR="198.51.100.4")
    rec = next(r for r in caplog.records if r.name == "security")
    assert (rec.event, rec.user_id, rec.ip) == ("login", user.pk, "198.51.100.4")
    assert "alice" not in str(rec.__dict__.get("message", "")) and "alice@example.com" not in str(rec.__dict__)


# ---------------- who can see them ----------------
def test_security_page_lists_my_events_and_nobody_elses(auth_client, user, other_user):
    LoginEvent.objects.create(user=user, kind="login", ip="198.51.100.4", user_agent="MyBrowser")
    LoginEvent.objects.create(user=other_user, kind="login", ip="203.0.113.99", user_agent="TheirBrowser")
    html = auth_client.get(reverse("security")).content.decode()
    assert "198.51.100.4" in html and "MyBrowser" in html
    assert "203.0.113.99" not in html and "TheirBrowser" not in html


def test_api_endpoint_returns_only_my_events_newest_first_and_capped(auth_client, user, other_user):
    now = timezone.now()
    for _ in range(55):
        LoginEvent.objects.create(user=user, kind="failed", ip="198.51.100.4")
    LoginEvent.objects.filter(user=user).update(created_at=now - timedelta(hours=1))
    newest = LoginEvent.objects.create(user=user, kind="login", ip="198.51.100.9", user_agent="Fresh")
    LoginEvent.objects.create(user=other_user, kind="login", ip="203.0.113.99")
    r = auth_client.get("/api/v1/account/login-events")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 50 and body[0]["id"] == newest.pk
    assert set(body[0]) == {"id", "kind", "ip", "user_agent", "created_at"}
    assert all(e["ip"] != "203.0.113.99" for e in body)


def test_api_endpoint_requires_login(client, db):
    assert client.get("/api/v1/account/login-events").status_code == 401


# ---------------- retention ----------------
def test_cleanup_deletes_only_events_older_than_retention(user, settings):
    settings.LOGIN_EVENT_RETENTION_DAYS = 90
    old = LoginEvent.objects.create(user=user, kind="login")
    fresh = LoginEvent.objects.create(user=user, kind="login")
    LoginEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=91))
    assert cleanup_login_events() == 1
    assert list(LoginEvent.objects.values_list("pk", flat=True)) == [fresh.pk]


@pytest.mark.parametrize("kind", ["login", "failed"])
def test_deleting_a_user_deletes_their_events(user, kind):
    LoginEvent.objects.create(user=user, kind=kind)
    user.delete()
    assert not LoginEvent.objects.exists()
