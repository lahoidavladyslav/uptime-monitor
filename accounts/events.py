"""Sign-in journal: one place that writes the DB row and the structured log line."""

import logging

from config.observability import client_ip

from .models import LoginEvent

security_log = logging.getLogger("security")


def record_login_event(request, user, kind: str) -> None:
    """`user` may be None (unknown username): then only the log line is written, without the name."""
    ip = client_ip(request) if request is not None else None
    if user is not None:
        agent = (request.META.get("HTTP_USER_AGENT", "") if request is not None else "")[:255]
        LoginEvent.objects.create(user=user, kind=kind, ip=ip, user_agent=agent)
    security_log.info(
        kind if kind != "failed" else "login_failed",
        extra={
            "event": "login_failed" if kind == "failed" else kind,
            "user_id": user.pk if user else None,
            "known_user": user is not None,
            "ip": ip,
        },
    )
