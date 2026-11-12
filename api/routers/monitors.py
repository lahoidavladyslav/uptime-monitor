from datetime import datetime
from typing import Literal

from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404
from django.utils import timezone
from ninja import Query, Router, Schema
from ninja import Status as HttpStatus

from monitors import regions, services
from monitors.forms import MaintenanceForm
from monitors.models import MaintenanceWindow
from monitors.tasks import check_ssl, enqueue_checks

from ..deps import session_auth
from ..errors import ApiError, field_errors_from_django
from ..schemas import StatusOut
from .dashboard import Status

router = Router()

Period = Literal["24h", "7d"]
MAX_PAGE_SIZE = 100
INCIDENT_LIMIT = 50


def owned(request, monitor_id: int):
    """The monitor, only if it belongs to the caller: a foreign id is a 404, not a 403 (no probing)."""
    row = services.dashboard_queryset(request.auth).filter(pk=monitor_id).first()
    if row is None:
        raise Http404
    return row


class UptimeOut(Schema):
    h24: float | None
    d7: float | None
    d30: float | None
    d90: float | None


class RegionOut(Schema):
    region: str
    status: Literal["up", "down", "stale", "unknown"]  # stale = the region has gone silent
    last_checked_at: datetime | None


class MonitorDetailOut(Schema):
    id: int
    name: str
    url: str
    status: Status
    is_active: bool
    last_checked_at: datetime | None
    ssl_days_left: int | None
    ssl_error: str
    down_minutes: int | None
    uptime: UptimeOut
    regions: list[RegionOut]


class StatsOut(Schema):
    total: int
    up: int
    down: int
    uptime: float | None
    rt_avg: int | None
    rt_min: int | None
    rt_max: int | None


class SeriesPointOut(Schema):
    t: str  # label, UTC
    ms: int | None  # None = failed check / no timing -> a gap in the line
    down: bool


class CheckOut(Schema):
    id: int
    checked_at: datetime
    status_code: int | None
    response_time_ms: int | None
    is_up: bool
    error: str | None


class ChecksPageOut(Schema):
    items: list[CheckOut]
    total: int
    page: int
    pages: int


class IncidentOut(Schema):
    id: int
    started_at: datetime
    resolved_at: datetime | None
    first_error: str
    minutes: int


class MaintenanceOut(Schema):
    id: int
    starts_at: datetime
    ends_at: datetime
    reason: str


class ChecksQuery(Schema):
    page: int = 1
    page_size: int = 20
    only_failed: bool = False


@router.get("/{monitor_id}", auth=session_auth, response=MonitorDetailOut, summary="One monitor with live status")
def detail(request, monitor_id: int):
    m = owned(request, monitor_id)
    now = timezone.now()
    windows = services.uptime_windows(m)
    return {
        "id": m.pk,
        "name": m.name,
        "url": m.url,
        "status": services.monitor_status(m),
        "is_active": m.is_active,
        "last_checked_at": m.last_checked_at,
        "ssl_days_left": services.ssl_days_left(m),
        "ssl_error": m.ssl_error,
        "down_minutes": int((now - m.down_since).total_seconds() // 60) if m.down_since else None,
        "uptime": {
            "h24": services.uptime_percent(m.checks_24h, m.up_24h),
            "d7": windows[7],
            "d30": windows[30],
            "d90": windows[90],
        },
        "regions": regions.region_status(m),
    }


@router.get("/{monitor_id}/stats", auth=session_auth, response=StatsOut, summary="Aggregates over 24h or 7d")
def stats(request, monitor_id: int, period: Period = "24h"):
    return services.monitor_stats(owned(request, monitor_id), period)


@router.get(
    "/{monitor_id}/series", auth=session_auth, response=list[SeriesPointOut], summary="Response-time chart points"
)
def series(request, monitor_id: int, period: Period = "24h"):
    return services.response_time_series(owned(request, monitor_id), period)


@router.get("/{monitor_id}/checks", auth=session_auth, response=ChecksPageOut, summary="Recent checks, paginated")
def checks(request, monitor_id: int, q: Query[ChecksQuery]):
    qs = owned(request, monitor_id).checks.all()
    if q.only_failed:
        qs = qs.filter(is_up=False)
    size = min(max(q.page_size, 1), MAX_PAGE_SIZE)
    page = Paginator(qs, size).get_page(q.page)  # clamps: past the end -> last page, garbage -> first
    return {
        "items": list(page.object_list),
        "total": page.paginator.count,
        "page": page.number,
        "pages": page.paginator.num_pages,
    }


@router.get("/{monitor_id}/incidents", auth=session_auth, response=list[IncidentOut], summary="Latest incidents")
def incidents(request, monitor_id: int):
    now = timezone.now()
    return [
        {
            "id": i.pk,
            "started_at": i.started_at,
            "resolved_at": i.resolved_at,
            "first_error": i.first_error,
            "minutes": int(((i.resolved_at or now) - i.started_at).total_seconds() // 60),
        }
        for i in owned(request, monitor_id).incidents.all()[:INCIDENT_LIMIT]
    ]


@router.get(
    "/{monitor_id}/maintenance", auth=session_auth, response=list[MaintenanceOut], summary="Active and upcoming windows"
)
def maintenance(request, monitor_id: int):
    monitor = owned(request, monitor_id)
    return MaintenanceWindow.objects.filter(monitor_id=monitor.pk, ends_at__gt=timezone.now()).order_by("starts_at")[
        :20
    ]


class MaintenanceIn(Schema):
    starts_at: str  # ISO 8601; parsed and validated by MaintenanceForm (one set of rules)
    ends_at: str
    reason: str = ""


@router.post(
    "/{monitor_id}/check",
    auth=session_auth,
    response={202: StatusOut},
    summary='Queue an immediate check ("check now")',
)
def check_now(request, monitor_id: int):
    monitor = owned(request, monitor_id)
    if not services.cooldown_ok(f"check-now:{monitor.pk}", settings.CHECK_NOW_COOLDOWN_SECONDS):
        raise ApiError(429, "Зачекайте кілька секунд перед наступною перевіркою.")
    if monitor.in_maintenance:
        raise ApiError(409, "Монітор зараз на обслуговуванні — перевірки призупинено.")
    enqueue_checks(monitor.pk, force=True)
    if monitor.url.lower().startswith("https://"):
        check_ssl.delay(monitor.pk)
    return HttpStatus(202, {"status": "queued"})


@router.post(
    "/{monitor_id}/maintenance",
    auth=session_auth,
    response={201: MaintenanceOut},
    summary="Schedule a maintenance window",
)
def create_maintenance(request, monitor_id: int, data: MaintenanceIn):
    monitor = owned(request, monitor_id)
    form = MaintenanceForm(data.dict())
    if not form.is_valid():
        raise ApiError(422, "Перевірте введені дані.", field_errors_from_django(form.errors))
    window = form.save(commit=False)
    window.monitor = monitor
    window.save()
    return HttpStatus(201, window)


@router.delete(
    "/{monitor_id}/maintenance/{window_id}",
    auth=session_auth,
    response=StatusOut,
    summary="Delete a maintenance window",
)
def delete_maintenance(request, monitor_id: int, window_id: int):
    monitor = owned(request, monitor_id)
    deleted, _ = MaintenanceWindow.objects.filter(pk=window_id, monitor_id=monitor.pk).delete()
    if not deleted:
        raise Http404
    return {"status": "deleted"}
