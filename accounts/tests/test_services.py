import time

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.test import RequestFactory

from accounts import services
from accounts.models import TOTPDevice

User = get_user_model()


def make_request():
    request = RequestFactory().post("/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    return request


@pytest.fixture
def inactive(db):
    return User.objects.create_user("carol", "carol@example.com", "pw12345678", is_active=False)


@pytest.fixture
def two_fa_user(user):
    device = TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed=True)
    return user, device


def test_confirmation_token_roundtrip_activates_user(inactive):
    token = services.make_confirmation_token(inactive)
    assert services.confirm_token(token) == inactive
    inactive.refresh_from_db()
    assert inactive.is_active


@pytest.mark.parametrize("mangle", [lambda t: t[:-2] + "xx", lambda t: "garbage", lambda t: ""])
def test_bad_confirmation_token_returns_none(inactive, mangle):
    assert services.confirm_token(mangle(services.make_confirmation_token(inactive))) is None
    inactive.refresh_from_db()
    assert not inactive.is_active


def test_resend_mails_only_inactive_matching_user(inactive, user):
    links = []

    def link_for(u):
        links.append(u)
        return f"https://x/confirm/{services.make_confirmation_token(u)}"

    services.resend_confirmation("carol@example.com", link_for)
    services.resend_confirmation("alice@example.com", link_for)  # active -> nothing
    services.resend_confirmation("nobody@example.com", link_for)  # unknown -> nothing
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["carol@example.com"]
    assert links == [inactive]


def test_has_two_factor(user, two_fa_user):
    assert services.has_two_factor(two_fa_user[0]) is True
    other = User.objects.create_user("dave", "d@example.com", "pw12345678")
    assert services.has_two_factor(other) is False


def test_finish_without_pending_state_is_expired(db):
    assert services.finish_two_factor(make_request(), "123456") == "expired"


def test_finish_with_stale_pending_state_is_expired(two_fa_user):
    user, _ = two_fa_user
    request = make_request()
    services.begin_two_factor(request, user, "/")
    request.session["pre_2fa"]["ts"] = time.time() - 10_000
    assert services.finish_two_factor(request, "123456") == "expired"


def test_wrong_code_is_invalid_then_locked_on_sixth_attempt(two_fa_user):
    user, _ = two_fa_user
    request = make_request()
    services.begin_two_factor(request, user, "/")
    assert [services.finish_two_factor(request, "000000") for _ in range(5)] == ["invalid"] * 5
    assert services.finish_two_factor(request, "000000") == "locked"
    assert services.finish_two_factor(request, "000000") == "expired"  # state is gone


def test_valid_code_logs_in_and_cannot_be_replayed(two_fa_user):
    user, device = two_fa_user
    code = pyotp.TOTP(device.secret).now()
    request = make_request()
    services.begin_two_factor(request, user, "/")
    assert services.finish_two_factor(request, code) == "ok"
    assert request.session["_auth_user_id"] == str(user.pk) and "pre_2fa" not in request.session

    again = make_request()
    services.begin_two_factor(again, user, "/")
    assert services.finish_two_factor(again, code) == "invalid"  # same time step: replay


def test_recovery_code_works_once(two_fa_user):
    user, device = two_fa_user
    codes = device.new_recovery_codes()
    device.save()
    request = make_request()
    services.begin_two_factor(request, user, "/")
    assert services.finish_two_factor(request, codes[0]) == "ok"
    second = make_request()
    services.begin_two_factor(second, user, "/")
    assert services.finish_two_factor(second, codes[0]) == "invalid"
