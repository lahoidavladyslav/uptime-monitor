import re

import pyotp
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.models import TOTPDevice
from api.api import api

User = get_user_model()
PW = "Str0ng-pass-123"


class ApiClient:
    """A browser-like client that enforces CSRF and always sends the *current* token."""

    def __init__(self):
        self.c = Client(enforce_csrf_checks=True)
        self.c.get("/api/v1/auth/csrf")

    def post(self, path, data=None, csrf=True):
        extra = {"HTTP_X_CSRFTOKEN": self.c.cookies["csrftoken"].value} if csrf else {}
        return self.c.post("/api/v1" + path, data or {}, content_type="application/json", **extra)

    def get(self, path):
        return self.c.get("/api/v1" + path)


@pytest.fixture
def ac():
    return ApiClient()


def signup(ac, username="carol", email="c@example.com", **over):
    return ac.post("/auth/signup", {"username": username, "email": email, "password1": PW, "password2": PW, **over})


def link(prefix):
    return re.search(re.escape(settings.FRONTEND_URL) + prefix + r"\S+", mail.outbox[-1].body).group(0)


@pytest.fixture
def two_fa(user):
    device = TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed=True)
    return device


# ---------------- signup / confirm / resend ----------------
def test_signup_creates_inactive_user_and_mails_spa_link(ac, db):
    r = signup(ac)
    assert r.status_code == 201 and r.json() == {"status": "confirmation_sent"}
    assert User.objects.get(username="carol").is_active is False
    assert len(mail.outbox) == 1
    assert link("/confirm/")


def test_signup_validation_errors_use_common_format(ac, user):
    r = signup(ac, password2="different")
    assert r.status_code == 422 and r.json()["errors"]["password2"]
    assert not User.objects.filter(username="carol").exists()


def test_signup_with_a_taken_email_is_indistinguishable_and_creates_nothing(ac, user):
    fresh = signup(ac, "newbie", "new@example.com")
    taken = signup(ac, "other", "ALICE@example.com")
    assert (
        (taken.status_code, taken.json()) == (fresh.status_code, fresh.json()) == (201, {"status": "confirmation_sent"})
    )
    assert not User.objects.filter(username="other").exists()
    assert mail.outbox[-1].to == ["alice@example.com"]
    assert settings.FRONTEND_URL + "/forgot-password" in mail.outbox[-1].body  # the SPA route, not a Django page


def test_signup_with_an_unconfirmed_email_resends_the_spa_confirmation_link(ac, db):
    signup(ac)
    mail.outbox.clear()
    assert signup(ac, "other", "c@example.com").status_code == 201
    assert User.objects.count() == 1 and link("/confirm/")


def test_confirm_activates_and_garbage_is_400(ac, db):
    signup(ac)
    token = link("/confirm/").rsplit("/", 1)[1]
    assert ac.post("/auth/confirm", {"token": "garbage"}).status_code == 400
    assert ac.post("/auth/confirm", {"token": token}).json() == {"status": "confirmed"}
    assert User.objects.get(username="carol").is_active


def test_resend_answers_identically_and_mails_only_inactive(ac, db, user):
    signup(ac)
    mail.outbox.clear()
    a = ac.post("/auth/resend", {"email": "c@example.com"})
    b = ac.post("/auth/resend", {"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json() == {"status": "ok"}
    assert len(mail.outbox) == 1


# ---------------- login / logout ----------------
def test_login_success_creates_session(ac, user):
    r = ac.post("/auth/login", {"username": "alice", "password": "pw12345678"})
    assert r.json() == {"status": "ok"}
    assert ac.get("/auth/me").json()["username"] == "alice"


def test_login_wrong_password_and_unconfirmed(ac, user, db):
    r = ac.post("/auth/login", {"username": "alice", "password": "nope"})
    assert r.status_code == 422 and r.json()["errors"]["__all__"]
    signup(ac)
    r = ac.post("/auth/login", {"username": "carol", "password": PW})
    assert r.status_code == 422 and "активований" in r.json()["errors"]["__all__"][0]
    assert ac.get("/auth/me").status_code == 401


def test_login_with_2fa_does_not_create_a_session(ac, user, two_fa):
    r = ac.post("/auth/login", {"username": "alice", "password": "pw12345678"})
    assert r.json() == {"status": "2fa_required"}
    assert ac.get("/auth/me").status_code == 401


def test_logout_ends_session_and_anonymous_logout_is_401(ac, user):
    assert ac.post("/auth/logout").status_code == 401
    ac.post("/auth/login", {"username": "alice", "password": "pw12345678"})
    assert ac.post("/auth/logout").json() == {"status": "ok"}  # CSRF token was rotated by login: still works
    assert ac.get("/auth/me").status_code == 401


# ---------------- 2FA ----------------
def start_challenge(ac):
    return ac.post("/auth/login", {"username": "alice", "password": "pw12345678"})


def test_2fa_valid_code_logs_in(ac, user, two_fa):
    start_challenge(ac)
    r = ac.post("/auth/login/2fa", {"code": pyotp.TOTP(two_fa.secret).now()})
    assert r.json() == {"status": "ok"}
    assert ac.get("/auth/me").status_code == 200


def test_2fa_wrong_code_is_422_on_code_field(ac, user, two_fa):
    start_challenge(ac)
    r = ac.post("/auth/login/2fa", {"code": "000000"})
    assert r.status_code == 422 and r.json()["errors"]["code"]
    assert ac.get("/auth/me").status_code == 401


def test_2fa_locks_after_five_tries(ac, user, two_fa):
    start_challenge(ac)
    for _ in range(5):
        assert ac.post("/auth/login/2fa", {"code": "000000"}).status_code == 422
    assert ac.post("/auth/login/2fa", {"code": pyotp.TOTP(two_fa.secret).now()}).status_code == 401
    assert ac.get("/auth/me").status_code == 401


def test_2fa_without_challenge_is_401(ac, user):
    assert ac.post("/auth/login/2fa", {"code": "123456"}).status_code == 401


def test_2fa_code_cannot_be_replayed(ac, user, two_fa):
    code = pyotp.TOTP(two_fa.secret).now()
    start_challenge(ac)
    assert ac.post("/auth/login/2fa", {"code": code}).status_code == 200
    ac.post("/auth/logout")
    start_challenge(ac)
    assert ac.post("/auth/login/2fa", {"code": code}).status_code == 422


def test_2fa_recovery_code_works_once(ac, user, two_fa):
    codes = two_fa.new_recovery_codes()
    two_fa.save()
    start_challenge(ac)
    assert ac.post("/auth/login/2fa", {"code": codes[0]}).status_code == 200
    ac.post("/auth/logout")
    start_challenge(ac)
    assert ac.post("/auth/login/2fa", {"code": codes[0]}).status_code == 422


def test_new_login_replaces_a_pending_2fa_challenge(ac, user, two_fa, db):
    """Review Focus: A starts a 2FA challenge, then B (no 2FA) logs in on the same browser."""
    User.objects.create_user("bob", "bob@example.com", "pw12345678")
    start_challenge(ac)
    assert ac.post("/auth/login", {"username": "bob", "password": "pw12345678"}).json() == {"status": "ok"}
    assert ac.get("/auth/me").json()["username"] == "bob"
    assert ac.post("/auth/login/2fa", {"code": pyotp.TOTP(two_fa.secret).now()}).status_code == 401


# ---------------- password reset ----------------
def test_password_reset_identical_answers_and_spa_link(ac, user):
    known = ac.post("/auth/password-reset", {"email": "alice@example.com"})
    unknown = ac.post("/auth/password-reset", {"email": "ghost@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json() == {"status": "ok"}
    assert len(mail.outbox) == 1 and link("/reset-password/")


def test_password_reset_confirm_changes_password_and_token_is_single_use(ac, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    new = "Another-pass-987"
    r = ac.post(
        "/auth/password-reset/confirm", {"uid": uid, "token": token, "new_password1": new, "new_password2": new}
    )
    assert r.json() == {"status": "ok"}
    assert ac.post("/auth/login", {"username": "alice", "password": new}).json() == {"status": "ok"}
    again = ac.post(
        "/auth/password-reset/confirm", {"uid": uid, "token": token, "new_password1": PW, "new_password2": PW}
    )
    assert again.status_code == 400 and "message" in again.json()


def test_password_reset_confirm_weak_password_and_bad_link(ac, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    weak = ac.post(
        "/auth/password-reset/confirm", {"uid": uid, "token": token, "new_password1": "123", "new_password2": "123"}
    )
    assert weak.status_code == 422 and weak.json()["errors"]["new_password2"]
    bad = ac.post("/auth/password-reset/confirm", {"uid": "zz", "token": "x", "new_password1": PW, "new_password2": PW})
    assert bad.status_code == 400


# ---------------- CSRF guard for every POST ----------------
def test_every_unsafe_endpoint_rejects_requests_without_csrf(ac, user):
    """Guards against a future route forgetting CSRF enforcement (POST, PUT, PATCH, DELETE alike)."""
    unsafe = [
        (method, path.replace("{monitor_id}", "1").replace("{window_id}", "1"))
        for path, ops in api.get_openapi_schema()["paths"].items()
        for method in ops
        if method in ("post", "put", "patch", "delete")
    ]
    assert len(unsafe) >= 10  # a silently empty list would make this guard vacuous
    for method, path in unsafe:
        r = getattr(Client(enforce_csrf_checks=True), method)(path, {}, content_type="application/json")
        assert r.status_code == 403, f"{method.upper()} {path} accepted a request without a CSRF token"
