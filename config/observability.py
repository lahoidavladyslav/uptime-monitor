"""Structured (JSON) logging, request correlation and client-IP resolution.

Privacy rules: the access log records the *route pattern* (``confirm/<str:token>/``), never the raw
path, because tokens for email confirmation, password reset and unsubscribe live in URLs. It records
a user id, never an email, and never a query string, header or body.
"""

import contextvars
import ipaddress
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime

from django.conf import settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# attributes every LogRecord has; anything else was passed through `extra=` and is emitted as a field
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


def sanitize_request_id(value: str) -> str | None:
    """An id from a client header is only trusted when it can't smuggle anything into the logs."""
    return value if value and _REQUEST_ID_RE.match(value) else None


class ContextFilter(logging.Filter):
    """Stamps the current request/task id onto every record, whichever logger emitted it."""

    def filter(self, record):
        request_id, task_id = request_id_var.get(), task_id_var.get()
        if request_id:
            record.request_id = request_id
        if task_id:
            record.task_id = task_id
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line (json.dumps escapes newlines, so a message can't forge a log line)."""

    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def client_ip(request) -> str | None:
    """The caller's address. Forwarding headers count only behind our own nginx (TRUST_PROXY_HEADERS)."""
    candidates = []
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        candidates.append(request.META.get("HTTP_X_REAL_IP", ""))
    candidates.append(request.META.get("REMOTE_ADDR", ""))
    for value in candidates:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            continue
    return None


class RequestLogMiddleware:
    """Binds a request id, echoes it as X-Request-ID, and writes one access-log line per request."""

    SKIP_ROUTES = {"healthz/"}
    logger = logging.getLogger("request")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = sanitize_request_id(request.headers.get("X-Request-ID", "")) or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.monotonic()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = rid
            self._log(request, response.status_code, started)
            return response
        finally:
            request_id_var.reset(token)

    def _log(self, request, status, started):
        match = getattr(request, "resolver_match", None)
        route = match.route if match else None
        if route in self.SKIP_ROUTES:
            return
        user = getattr(request, "user", None)
        self.logger.info(
            "request",
            extra={
                "method": request.method,
                "route": route or "unresolved",
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "user_id": user.pk if user is not None and user.is_authenticated else None,
                "ip": client_ip(request),
            },
        )


def build_logging(*, json_format: bool, level: str = "INFO") -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": "config.observability.ContextFilter"}},
        "formatters": {
            "json": {"()": "config.observability.JsonFormatter"},
            "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "filters": ["context"],
                "formatter": "json" if json_format else "plain",
            }
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            # Django logs "Bad Request: /confirm/<token>/" with the raw path for every 4xx; our access
            # log already covers those (with the route instead), so only keep server errors here.
            "django.request": {"level": "ERROR"},
            # gunicorn's own access log would print raw paths too (see --access-logfile in Dockerfile.prod)
            "gunicorn.access": {"level": "WARNING"},
        },
    }
