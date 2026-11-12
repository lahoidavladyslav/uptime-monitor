"""One error shape for the whole API: {"message": str, "errors": {field: [msgs]}}."""

from django.core.exceptions import NON_FIELD_ERRORS


class ApiError(Exception):
    def __init__(self, status: int, message: str, errors: dict[str, list[str]] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or {}


def field_errors_from_django(errors) -> dict[str, list[str]]:
    """Django ErrorDict -> {field: [messages]}; non-field errors live under "__all__"."""
    return {
        ("__all__" if name == NON_FIELD_ERRORS else name): [str(m) for m in messages]
        for name, messages in errors.items()
    }
