"""Throughput / latency / backlog numbers, shared by /metrics and `manage.py load_report`."""

from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import CheckResult
from .tasks import due_monitors

OVERDUE_AFTER = timedelta(seconds=30)  # a check this far past its slot counts as overdue


def queue_lengths() -> dict[str, int]:
    """Messages waiting per Celery queue (Redis LLEN). Empty if the broker can't be reached: a monitoring
    endpoint must degrade, not fail."""
    import redis

    names = [f"checks.{r}" for r in settings.CHECK_REGIONS] + ["checks", "notifications", "reports"]
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        return {name: int(client.llen(name)) for name in names}
    except Exception:
        return {}


def checks_last_minute(now=None) -> int:
    now = now or timezone.now()
    return CheckResult.objects.filter(checked_at__gte=now - timedelta(minutes=1)).count()


def response_percentiles(now=None, window=timedelta(minutes=5)) -> tuple[int, int] | None:
    """(p50, p95) response time in ms over the window (timed, non-maintenance checks), or None."""
    now = now or timezone.now()
    with connection.cursor() as cur:
        cur.execute(
            """SELECT percentile_cont(ARRAY[0.5, 0.95]) WITHIN GROUP (ORDER BY response_time_ms)
               FROM monitors_checkresult
               WHERE checked_at >= %s AND response_time_ms IS NOT NULL AND NOT in_maintenance""",
            [now - window],
        )
        value = cur.fetchone()[0]
    return None if value is None else (round(value[0]), round(value[1]))


def overdue_checks(now=None) -> int:
    """Active monitors whose next check was due more than OVERDUE_AFTER ago (the backlog)."""
    now = now or timezone.now()
    return due_monitors(now - OVERDUE_AFTER - timedelta(seconds=10)).exclude(last_checked_at__isnull=True).count()
