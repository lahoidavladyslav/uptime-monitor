"""Auth logic shared by the Django views and the JSON API (one implementation, two front doors)."""

import time
from collections.abc import Callable
from typing import Literal

import pyotp
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core import signing
from django.core.mail import send_mail

from .events import record_login_event
from .models import TOTPDevice

User = get_user_model()

CONFIRM_SALT = "email-confirm"
CONFIRM_MAX_AGE = 3 * 24 * 3600
PRE_2FA_MAX_AGE = 300
MAX_2FA_ATTEMPTS = 5
BACKEND = "django.contrib.auth.backends.ModelBackend"

TwoFactorResult = Literal["ok", "invalid", "expired", "locked"]


# ---------- email confirmation ----------
def make_confirmation_token(user) -> str:
    return signing.dumps(user.pk, salt=CONFIRM_SALT)


def confirm_token(token: str):
    """Activate the account the token belongs to. None if the token is bad, expired or orphaned."""
    try:
        pk = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
        user = User.objects.get(pk=pk)
    except (signing.BadSignature, User.DoesNotExist):
        return None
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    return user


def send_confirmation_email(user, link: str) -> None:
    send_mail(
        "Підтвердіть email",
        f"Щоб активувати акаунт, відкрийте посилання (діє 3 дні):\n{link}",
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


def resend_confirmation(email: str, link_for: Callable[[object], str]) -> None:
    """Silent when nothing matches, so callers can answer identically either way."""
    user = User.objects.filter(email__iexact=email, is_active=False).first()
    if user:
        send_confirmation_email(user, link_for(user))


def register(form, confirm_link_for: Callable[[object], str], reset_link: str):
    """Sign-up that never reveals whether an email already has an account.

    New address        -> inactive account + confirmation mail.
    Unconfirmed owner  -> the same confirmation mail again, no second account.
    Active account     -> a "you already have an account" mail to its owner, nothing created.
    The caller answers identically in all three cases.
    """
    email = form.cleaned_data["email"]
    existing = User.objects.filter(email__iexact=email).order_by("-is_active", "id").first()
    if existing is None:
        user = form.save(commit=False)
        user.is_active = False  # cannot log in until the email is confirmed
        user.save()
        send_confirmation_email(user, confirm_link_for(user))
    elif not existing.is_active:
        send_confirmation_email(existing, confirm_link_for(existing))
    else:
        send_mail(
            "У вас уже є акаунт",
            "Хтось (імовірно ви) спробував зареєструватися з цією адресою, але вже є акаунт із цією адресою.\n"
            f"Якщо ви забули пароль, відновіть його: {reset_link}\n\n"
            "Якщо це були не ви — просто проігноруйте цей лист.",
            settings.DEFAULT_FROM_EMAIL,
            [existing.email],
        )


# ---------- two-factor ----------
def has_two_factor(user) -> bool:
    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def begin_two_factor(request, user, next_url: str) -> None:
    """The password was correct but the user is NOT logged in yet."""
    request.session["pre_2fa"] = {"user": user.pk, "ts": time.time(), "next": next_url, "tries": 0}


def verify_totp(device, code: str) -> bool:
    totp = pyotp.TOTP(device.secret)
    code = code.replace(" ", "")
    now = int(time.time())
    for offset in (-1, 0, 1):  # tolerate 30s clock skew
        step = now // 30 + offset
        if step > device.last_used_step and totp.at(step * 30) == code:
            device.last_used_step = step  # one code, one login
            device.save(update_fields=["last_used_step"])
            return True
    return False


def finish_two_factor(request, code: str) -> TwoFactorResult:
    """Check a TOTP/recovery code against the pending challenge. Logs the user in on "ok"."""
    pre = request.session.get("pre_2fa")
    if not pre or time.time() - pre["ts"] > PRE_2FA_MAX_AGE:
        request.session.pop("pre_2fa", None)
        return "expired"
    pre["tries"] += 1
    request.session["pre_2fa"] = pre
    user = User.objects.get(pk=pre["user"])
    if pre["tries"] > MAX_2FA_ATTEMPTS:
        request.session.pop("pre_2fa", None)
        record_login_event(request, user, "two_factor_locked")
        return "locked"
    device = user.totp_device
    if verify_totp(device, code) or device.use_recovery_code(code):
        request.session.pop("pre_2fa", None)
        login(request, user, backend=BACKEND)
        return "ok"
    record_login_event(request, user, "two_factor_failed")
    return "invalid"
