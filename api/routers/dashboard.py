from datetime import datetime
from typing import Literal

from ninja import Router, Schema

from monitors.services import dashboard_rows, summarize

from ..deps import session_auth

router = Router()

Status = Literal["up", "down", "unknown", "maintenance", "paused"]


class MonitorRowOut(Schema):
    id: int
    name: str
    status: Status
    last_checked_at: datetime | None
    uptime_24h: float | None
    down_minutes: int | None
    spark: list[int | None]  # 24 hourly average response times (ms), oldest -> newest


class SummaryOut(Schema):
    up: int
    down: int
    unknown: int
    maintenance: int
    paused: int
    open_incidents: int


class DashboardOut(Schema):
    summary: SummaryOut
    monitors: list[MonitorRowOut]


@router.get("", auth=session_auth, response=DashboardOut, summary="Monitors with live status, uptime and sparkline")
def dashboard(request):
    rows = dashboard_rows(request.auth)
    return {"summary": summarize(rows), "monitors": rows}
