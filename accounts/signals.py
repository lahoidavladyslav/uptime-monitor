from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from .events import record_login_event


@receiver(user_logged_in)
def _logged_in(sender, request, user, **kwargs):
    record_login_event(request, user, "login")


@receiver(user_login_failed)
def _login_failed(sender, credentials, request=None, **kwargs):
    # Never read the password from `credentials`; the username is only used to find the account.
    username = credentials.get("username") or ""
    user = get_user_model().objects.filter(**{get_user_model().USERNAME_FIELD: username}).first() if username else None
    record_login_event(request, user, "failed")
