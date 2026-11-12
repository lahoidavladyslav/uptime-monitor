"""Operational endpoints: /healthz/ for orchestrators, /metrics for Prometheus."""

import hmac
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Max, Min, OuterRef, Q, Subquery
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from reports.models import ReportExport

from . import loadstats, partitions
from .models import CheckResult, Incident, Monitor
from .tasks import due_monitors

STALE_AFTER = timedelta(minutes=5)


@never_cache
@require_GET
def healthz(request):
    """200 when this web process can serve traffic (DB + cache). 503 otherwise.

    `checks` reports whether the background pipeline (beat -> worker) is alive, but does NOT
    affect the status code: restarting web wouldn't fix a dead worker. Alert on it instead
    (or on the `uptime_scheduler_lag_seconds` metric).
    """
    body = {"db": "ok", "cache": "ok", "checks": "ok"}
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        body["db"] = "error"
    try:
        cache.set("healthz", "1", 5)
        if cache.get("healthz") != "1":
            raise RuntimeError
    except Exception:
        body["cache"] = "error"

    if body["db"] == "ok":
        newest = Monitor.objects.filter(is_active=True).aggregate(m=Max("last_checked_at"))["m"]
        has_active = Monitor.objects.filter(is_active=True).exists()
        if has_active and (newest is None or timezone.now() - newest > STALE_AFTER):
            body["checks"] = "stale"
    healthy = body["db"] == "ok" and body["cache"] == "ok"
    body["status"] = "ok" if healthy else "error"
    return JsonResponse(body, status=200 if healthy else 503)


def _line(name, value, labels=None):
    lab = ""
    if labels:
        lab = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
    return f"{name}{lab} {value}"


@never_cache
@require_GET
def metrics(request):
    """Prometheus text format. Disabled unless METRICS_TOKEN is set; then Bearer auth is required."""
    token = settings.METRICS_TOKEN
    if not token:
        raise Http404
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        return HttpResponse("unauthorized\n", status=401, content_type="text/plain")

    now = timezone.now()
    out = []

    def metric(name, help_text, samples):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.extend(samples)

    latest = CheckResult.objects.filter(monitor=OuterRef("pk")).order_by("-checked_at")
    active = Monitor.objects.filter(is_active=True).annotate(last_is_up=Subquery(latest.values("is_up")[:1]))
    by_state = active.aggregate(
        up=Count("id", filter=Q(last_is_up=True)),
        down=Count("id", filter=Q(last_is_up=False)),
        unknown=Count("id", filter=Q(last_is_up__isnull=True)),
    )
    metric(
        "uptime_monitors",
        "Active monitors by last known state.",
        [_line("uptime_monitors", v, {"state": k}) for k, v in by_state.items()],
    )
    metric(
        "uptime_monitors_inactive",
        "Paused monitors.",
        [_line("uptime_monitors_inactive", Monitor.objects.filter(is_active=False).count())],
    )
    metric(
        "uptime_open_incidents", "Currently open incidents.",
        [_line("uptime_open_incidents", Incident.objects.filter(resolved_at__isnull=True).count())],
    )  # fmt: skip
    metric(
        "uptime_checks_last_5m", "Checks recorded in the last 5 minutes.",
        [_line("uptime_checks_last_5m", CheckResult.objects.filter(checked_at__gte=now - timedelta(minutes=5)).count())],
    )  # fmt: skip

    metric(
        "uptime_checkresult_default_partition_rows",
        "Rows in the default partition of the check table. Must be 0; anything there means a day's partition was missing.",
        [_line("uptime_checkresult_default_partition_rows", partitions.default_rows())],
    )
    metric(
        "uptime_checkresult_partitions", "Attached daily partitions of the check table.",
        [_line("uptime_checkresult_partitions", len(partitions.daily_partitions()))],
    )  # fmt: skip

    metric(
        "uptime_queue_length", "Messages waiting in each Celery queue.",
        [_line("uptime_queue_length", n, {"queue": q}) for q, n in loadstats.queue_lengths().items()],
    )  # fmt: skip
    metric(
        "uptime_checks_last_1m", "Checks recorded in the last minute (throughput).",
        [_line("uptime_checks_last_1m", loadstats.checks_last_minute(now))],
    )  # fmt: skip
    pct = loadstats.response_percentiles(now)
    if pct is not None:
        metric(
            "uptime_response_time_ms", "Response time of checks in the last 5 minutes.",
            [_line("uptime_response_time_ms", pct[0], {"quantile": "0.5"}),
             _line("uptime_response_time_ms", pct[1], {"quantile": "0.95"})],
        )  # fmt: skip
    metric(
        "uptime_overdue_checks", "Monitors whose check is more than 30 s past its slot (the backlog).",
        [_line("uptime_overdue_checks", loadstats.overdue_checks(now))],
    )  # fmt: skip

    # How overdue is the most overdue monitor? Grows when beat or the workers are stuck.
    overdue = due_monitors(now).exclude(last_checked_at__isnull=True).aggregate(oldest=Min("due_at"))["oldest"]
    lag = max((now - overdue).total_seconds(), 0) if overdue else 0
    metric(
        "uptime_scheduler_lag_seconds",
        "Age of the most overdue check; alert when it keeps growing.",
        [_line("uptime_scheduler_lag_seconds", round(lag))],
    )

    exports = dict(ReportExport.objects.values_list("status").annotate(n=Count("id")))
    metric(
        "uptime_report_exports", "Report exports by status.",
        [_line("uptime_report_exports", exports.get(s, 0), {"status": s}) for s in ReportExport.Status.values],
    )  # fmt: skip
    expiring = Monitor.objects.filter(is_active=True, ssl_expires_at__lte=now + timedelta(days=14)).count()
    metric(
        "uptime_ssl_expiring_14d",
        "Active monitors whose certificate expires within 14 days.",
        [_line("uptime_ssl_expiring_14d", expiring)],
    )
    return HttpResponse("\n".join(out) + "\n", content_type="text/plain; version=0.0.4")
