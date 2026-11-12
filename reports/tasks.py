import csv
import io
from datetime import UTC, datetime, time, timedelta

from celery import shared_task
from django.core.files.base import ContentFile
from django.db.models import Count, DurationField, ExpressionWrapper, Q, Sum, Value
from django.db.models.functions import Coalesce, Greatest, Least
from django.utils import timezone

from monitors import rollups
from monitors.models import Incident, Monitor

from .models import ReportExport

HEADER = [
    "monitor_name",
    "url",
    "checks_total",
    "checks_failed",
    "uptime_percent",
    "avg_response_ms",
    "incidents_count",
    "total_downtime_minutes",
]


def build_rows(user, period_start, period_end):
    """All numbers come from two grouped SQL queries, not from raw rows in Python."""
    start = datetime.combine(period_start, time.min, tzinfo=UTC)
    end = datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=UTC)

    monitors = list(Monitor.objects.filter(user=user).order_by("name", "id"))
    # Hourly rollups for everything old (raw checks only live 7 days) plus raw for the last 1-2 hours.
    aggs = rollups.aggregate([m.pk for m in monitors], start, end)

    # Incidents overlapping the period; downtime is clipped to the period.
    clipped_end = Least(Coalesce("resolved_at", Value(timezone.now())), Value(end))
    clipped_start = Greatest("started_at", Value(start))
    incidents = {
        r["monitor_id"]: r
        for r in Incident.objects.filter(monitor__user=user, started_at__lt=end)
        .filter(Q(resolved_at__isnull=True) | Q(resolved_at__gte=start))
        .values("monitor_id")
        .annotate(
            n=Count("id"),
            downtime=Sum(ExpressionWrapper(clipped_end - clipped_start, output_field=DurationField())),
        )
    }

    for m in monitors:
        a = aggs[m.pk]
        i = incidents.get(m.pk)
        yield [
            m.name,
            m.url,
            a.total,
            a.down,
            a.uptime if a.total else "",
            a.avg_ms if a.avg_ms is not None else "",
            i["n"] if i else 0,
            round(i["downtime"].total_seconds() / 60, 1) if i and i["downtime"] else 0,
        ]


@shared_task
def generate_report(export_id):
    # Atomic claim pending -> processing: a duplicate delivery updates 0 rows and exits.
    claimed = ReportExport.objects.filter(pk=export_id, status=ReportExport.Status.PENDING).update(
        status=ReportExport.Status.PROCESSING
    )
    if not claimed:
        return "skipped"

    export = ReportExport.objects.get(pk=export_id)
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(HEADER)
        writer.writerows(build_rows(export.user, export.period_start, export.period_end))
        name = f"report_{export.pk}_{export.period_start}_{export.period_end}.csv"
        export.file.save(name, ContentFile(buf.getvalue().encode("utf-8")), save=False)
        export.status = ReportExport.Status.DONE
        export.error = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        export.status = ReportExport.Status.FAILED
        export.error = f"{type(exc).__name__}: {exc}"
    export.finished_at = timezone.now()
    export.save()
    return export.status
