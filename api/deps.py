from ninja.errors import HttpError
from ninja.security import APIKeyHeader, SessionAuth
from ninja.utils import check_csrf

CSRF_MESSAGE = "CSRF-перевірка не пройдена. Оновіть сторінку й спробуйте ще раз."


class CsrfProtect(APIKeyHeader):
    """For *unauthenticated* unsafe routes (login, signup, reset).

    Ninja runs auth BEFORE it parses the body, so a request without a valid CSRF token is
    rejected with 403 whatever its payload looks like. (Calling a check inside the handler
    would be too late: invalid bodies would answer 422 first.)
    """

    param_name = "X-CSRFToken"

    def authenticate(self, request, key):
        if check_csrf(request) is not None:
            raise HttpError(403, CSRF_MESSAGE)
        return True


csrf_protect = CsrfProtect()

# Authenticated routes: Ninja's SessionAuth already enforces CSRF on unsafe methods.
session_auth = SessionAuth(csrf=True)
