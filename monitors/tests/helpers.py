"""Small builders shared by the tests."""

from monitors.tasks import CheckOutcome


def outcome(is_up=True, status_code=None, ms=None, error=None, details=None):
    """A CheckOutcome the way a real check would produce it (status/time/error follow the verdict)."""
    code = status_code if status_code is not None else (200 if is_up else 503)
    return CheckOutcome(
        code,
        ms if ms is not None else (10 if is_up else None),
        is_up,
        None if is_up else (error or "boom"),
        details or ({} if is_up else {"type": "http_status", "status_code": code, "message": error or "boom"}),
    )
