from ninja.security import SessionAuth
from ninja.utils import check_csrf

from .errors import ApiError

CSRF_MESSAGE = "CSRF-перевірка не пройдена. Оновіть сторінку й спробуйте ще раз."


def enforce_csrf(request) -> None:
    """For *unauthenticated* unsafe requests (login, signup, reset): Ninja only checks CSRF
    inside authenticated routes, so those call this explicitly."""
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    if check_csrf(request) is not None:
        raise ApiError(403, CSRF_MESSAGE)


# Authenticated routes: Ninja's SessionAuth already enforces CSRF on unsafe methods.
session_auth = SessionAuth(csrf=True)
