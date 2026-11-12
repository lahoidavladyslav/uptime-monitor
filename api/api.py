from django.http import JsonResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError
from ninja.errors import ValidationError as NinjaValidationError

from .errors import ApiError

UNAUTHORIZED = "Потрібен вхід."
INVALID = "Перевірте введені дані."


def _error(status: int, message: str, errors: dict | None = None) -> JsonResponse:
    return JsonResponse({"message": message, "errors": errors or {}}, status=status)


def register_handlers(api: NinjaAPI) -> None:
    @api.exception_handler(ApiError)
    def api_error(request, exc: ApiError):
        return _error(exc.status, exc.message, exc.errors)

    @api.exception_handler(NinjaValidationError)
    def validation_error(request, exc: NinjaValidationError):
        errors: dict[str, list[str]] = {}
        for item in exc.errors:
            loc = [p for p in item.get("loc", []) if p not in ("body", "query", "path", "form")]
            errors.setdefault(str(loc[-1]) if loc else "__all__", []).append(item.get("msg", INVALID))
        return _error(422, INVALID, errors)

    @api.exception_handler(AuthenticationError)
    def unauthorized(request, exc: AuthenticationError):
        return _error(401, UNAUTHORIZED)

    @api.exception_handler(HttpError)
    def http_error(request, exc: HttpError):
        # e.g. Ninja's own CSRF failure inside authenticated routes
        message = (
            "CSRF-перевірка не пройдена. Оновіть сторінку й спробуйте ще раз." if exc.status_code == 403 else str(exc)
        )
        return _error(exc.status_code, message)


api = NinjaAPI(title="Uptime Monitor API", version="1.0", urls_namespace="api")
register_handlers(api)

from .routers.auth import router as auth_router  # noqa: E402

api.add_router("/auth", auth_router, tags=["auth"])
