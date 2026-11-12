import re
import time

import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

from accounts.models import TOTPDevice

User = get_user_model()
PW = "Str0ng-pass-123"


def signup(client, username="carol", email="c@example.com"):
    return client.post(reverse("signup"), {"username": username, "email": email, "password1": PW, "password2": PW})


def confirm_link():
    return re.search(r"http://\S+/confirm/\S+/", mail.outbox[-1].body).group(0)


def test_signup_requires_email_confirmation_before_login(client, db):
    r = signup(client)
    assert r.status_code == 200 and "Перевірте пошту" in r.content.decode()
    user = User.objects.get(username="carol")
    assert user.is_active is False
    # cannot log in yet
    r = client.post(reverse("login"), {"username": "carol", "password": PW})
    assert r.status_code == 200 and "ще не активований" in r.content.decode()
    assert "_auth_user_id" not in client.session
    # confirm via the emailed link
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["c@example.com"]
    r = client.get(confirm_link())
    assert r.status_code == 302
    user.refresh_from_db()
    assert user.is_active
    assert client.post(reverse("login"), {"username": "carol", "password": PW}).status_code == 302


def test_confirmation_link_tampered_or_expired(client, db, settings):
    signup(client)
    link = confirm_link()
    assert client.get(link[:-3] + "x/").status_code == 400
    assert User.objects.get(username="carol").is_active is False
    import accounts.views as v

    v.CONFIRM_MAX_AGE = -1  # everything is expired
    try:
        assert client.get(link).status_code == 400
    finally:
        v.CONFIRM_MAX_AGE = 3 * 24 * 3600


def test_duplicate_email_rejected(client, user):
    r = signup(client, "other", "ALICE@example.com")
    assert r.status_code == 200 and not User.objects.filter(username="other").exists()


def test_resend_does_not_reveal_whether_email_exists(client, db):
    signup(client)
    mail.outbox.clear()
    r1 = client.post(reverse("resend_confirmation"), {"email": "c@example.com"}, follow=True)
    r2 = client.post(reverse("resend_confirmation"), {"email": "nobody@example.com"}, follow=True)
    assert len(mail.outbox) == 1  # only the real inactive account got a mail
    m1 = [str(m) for m in r1.context["messages"]]
    m2 = [str(m) for m in r2.context["messages"]]
    assert m1 == m2


def test_password_reset_flow(client, user):
    r = client.post(reverse("password_reset"), {"email": "alice@example.com"})
    assert r.status_code == 302
    link = re.search(r"http://\S+/password-reset/\S+/\S+/", mail.outbox[-1].body).group(0)
    r = client.get(link, follow=True)
    r = client.post(r.request["PATH_INFO"], {"new_password1": "Another-pass-987", "new_password2": "Another-pass-987"})
    assert r.status_code == 302
    assert client.post(reverse("login"), {"username": "alice", "password": "Another-pass-987"}).status_code == 302


def test_password_reset_unknown_email_sends_nothing_but_looks_same(client, db):
    r = client.post(reverse("password_reset"), {"email": "ghost@example.com"})
    assert r.status_code == 302 and len(mail.outbox) == 0


# ---------------- 2FA ----------------
def enable_2fa(client, user):
    client.force_login(user)
    client.get(reverse("two_factor_setup"))
    device = TOTPDevice.objects.get(user=user)
    r = client.post(reverse("two_factor_setup"), {"code": pyotp.TOTP(device.secret).now()})
    assert r.status_code == 200 and "коди відновлення" in r.content.decode()
    codes = re.findall(r"(?:^|>)([0-9a-f]{10})$", r.content.decode(), re.M)
    client.logout()
    device.refresh_from_db()
    # the next code must be a different time step, or replay protection (correctly) rejects it
    device.last_used_step -= 2
    device.save()
    return device, codes


def test_2fa_setup_rejects_wrong_code_and_stores_only_hashes(client, user):
    client.force_login(user)
    client.get(reverse("two_factor_setup"))
    r = client.post(reverse("two_factor_setup"), {"code": "000000"})
    assert "Невірний код" in r.content.decode()
    assert not TOTPDevice.objects.get(user=user).confirmed
    device, codes = enable_2fa(client, user)
    assert len(codes) == 8 and device.confirmed
    assert not any(c in str(device.recovery_hashes) for c in codes)  # plaintext never stored


def test_login_with_2fa_requires_code(client, user):
    device, codes = enable_2fa(client, user)
    r = client.post(reverse("login"), {"username": "alice", "password": "pw12345678"})
    assert r.status_code == 302 and r["Location"] == reverse("two_factor_verify")
    assert "_auth_user_id" not in client.session  # password alone is NOT a login
    assert client.get(reverse("dashboard")).status_code == 302

    r = client.post(reverse("two_factor_verify"), {"code": "123456"})
    assert "Невірний код" in r.content.decode() and "_auth_user_id" not in client.session

    r = client.post(reverse("two_factor_verify"), {"code": pyotp.TOTP(device.secret).now()})
    assert r.status_code == 302
    assert client.get(reverse("dashboard")).status_code == 200


def test_totp_code_cannot_be_replayed(client, user):
    device, _ = enable_2fa(client, user)
    code = pyotp.TOTP(device.secret).now()
    for attempt in (1, 2):
        client.post(reverse("login"), {"username": "alice", "password": "pw12345678"})
        client.post(reverse("two_factor_verify"), {"code": code})
        logged_in = "_auth_user_id" in client.session
        assert logged_in is (attempt == 1)
        client.logout()


def test_recovery_code_works_once(client, user):
    device, codes = enable_2fa(client, user)
    client.post(reverse("login"), {"username": "alice", "password": "pw12345678"})
    assert client.post(reverse("two_factor_verify"), {"code": codes[0]}).status_code == 302
    assert "_auth_user_id" in client.session
    client.logout()
    client.post(reverse("login"), {"username": "alice", "password": "pw12345678"})
    client.post(reverse("two_factor_verify"), {"code": codes[0]})
    assert "_auth_user_id" not in client.session  # already used
    device.refresh_from_db()
    assert len(device.recovery_hashes) == 7


def test_2fa_attempts_are_limited(client, user):
    enable_2fa(client, user)
    client.post(reverse("login"), {"username": "alice", "password": "pw12345678"})
    for _ in range(5):
        client.post(reverse("two_factor_verify"), {"code": "111111"})
    r = client.post(reverse("two_factor_verify"), {"code": "111111"})
    assert r.status_code == 302 and r["Location"] == reverse("login")
    assert client.get(reverse("two_factor_verify")).status_code == 302  # pre-login state is gone


def test_2fa_pending_state_expires(client, user):
    enable_2fa(client, user)
    client.post(reverse("login"), {"username": "alice", "password": "pw12345678"})
    session = client.session
    session["pre_2fa"]["ts"] = time.time() - 10_000
    session.save()
    assert client.get(reverse("two_factor_verify")).status_code == 302


def test_2fa_verify_without_password_step_is_redirected(client, db):
    assert client.get(reverse("two_factor_verify")).status_code == 302


def test_2fa_login_does_not_redirect_offsite(client, user):
    device, _ = enable_2fa(client, user)
    client.post(reverse("login") + "?next=https://evil.example/", {"username": "alice", "password": "pw12345678"})
    r = client.post(reverse("two_factor_verify"), {"code": pyotp.TOTP(device.secret).now()})
    assert r.status_code == 302 and "evil.example" not in r["Location"]


def test_disable_2fa_needs_password(client, user):
    enable_2fa(client, user)
    client.force_login(user)
    client.post(reverse("two_factor_disable"), {"password": "wrong"})
    assert TOTPDevice.objects.filter(user=user, confirmed=True).exists()
    client.post(reverse("two_factor_disable"), {"password": "pw12345678"})
    assert not TOTPDevice.objects.filter(user=user).exists()


@pytest.mark.parametrize("name", ["security", "two_factor_setup"])
def test_security_pages_require_login(client, db, name):
    assert client.get(reverse(name)).status_code == 302
