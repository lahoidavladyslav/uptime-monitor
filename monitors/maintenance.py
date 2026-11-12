"""Maintenance windows: scope, recurrence (DST-safe) and the overlap arithmetic used by reports.

Recurrence is evaluated as LOCAL WALL-CLOCK time in the window's own IANA zone, never as a fixed UTC offset:
"every Sunday 02:00-03:00 Europe/Kyiv" is 00:00 UTC in winter and 23:00 UTC (Saturday) in summer. Each
occurrence is a naive local datetime localised with `zoneinfo`:
  - a local time that does not exist (spring-forward gap) uses the offset from before the change (fold=0);
  - a local time that happens twice (autumn) resolves to the first one (fold=0);
  - the end is start + the template's WALL-CLOCK duration; if that makes the window empty (e.g. a window that
    sits entirely inside the gap) it falls back to the template's absolute duration.
"""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, rrulestr
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import MaintenanceWindow

ALLOWED_FREQ = {"DAILY": DAILY, "WEEKLY": WEEKLY, "MONTHLY": MONTHLY}
ALLOWED_KEYS = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL", "WKST", "BYSETPOS"}
MAX_COUNT = 1000
HORIZON_DAYS = 30


# ----------------------------- validation -----------------------------
def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"Невідомий часовий пояс: {name!r}") from exc
    return name


def validate_rrule(text: str) -> str:
    """Normalised rule text, or ValueError. Only DAILY/WEEKLY/MONTHLY: a `FREQ=SECONDLY` rule would be a bomb,
    and the time of day comes from the window itself (no BYHOUR/BYMINUTE/BYSECOND/DTSTART)."""
    raw = (text or "").strip()
    if not raw or "\n" in raw or "DTSTART" in raw.upper():
        raise ValueError("Некоректне правило повторення")
    raw = re.sub(r"^RRULE:", "", raw, flags=re.I)
    parts = {}
    for chunk in raw.split(";"):
        key, sep, value = chunk.partition("=")
        if not sep or not value:
            raise ValueError("Некоректне правило повторення")
        key = key.strip().upper()
        if key not in ALLOWED_KEYS:
            raise ValueError(f"Параметр {key} не підтримується")
        parts[key] = value.strip()
    if parts.get("FREQ", "").upper() not in ALLOWED_FREQ:
        raise ValueError("Підтримуються лише FREQ=DAILY, WEEKLY або MONTHLY")
    if "INTERVAL" in parts and not (parts["INTERVAL"].isdigit() and int(parts["INTERVAL"]) >= 1):
        raise ValueError("INTERVAL має бути додатним числом")
    if "COUNT" in parts and not (parts["COUNT"].isdigit() and 1 <= int(parts["COUNT"]) <= MAX_COUNT):
        raise ValueError(f"COUNT має бути від 1 до {MAX_COUNT}")
    if "UNTIL" in parts:  # the rule is evaluated on naive local times, so UNTIL is too
        parts["UNTIL"] = parts["UNTIL"].rstrip("Zz")
    cleaned = ";".join(f"{k}={v}" for k, v in parts.items())
    try:
        rrulestr(cleaned, dtstart=datetime(2026, 1, 1))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Некоректне правило повторення") from exc
    return cleaned


# ----------------------------- scope -----------------------------
def applies_to(monitor_ref, user_ref) -> Q:
    """Windows that cover a monitor: its own, and the user-wide ones (monitor IS NULL) of the same user."""
    return Q(monitor=monitor_ref) | Q(monitor__isnull=True, user=user_ref)


def under_maintenance_expr(now):
    """Exists() subquery for annotating Monitor rows: some concrete window covers `now`."""
    return Exists(
        MaintenanceWindow.objects.filter(rrule="", starts_at__lte=now, ends_at__gt=now).filter(
            applies_to(OuterRef("pk"), OuterRef("user"))
        )
    )


def is_active(monitor, at) -> bool:
    return (
        MaintenanceWindow.objects.filter(rrule="", starts_at__lte=at, ends_at__gt=at)
        .filter(applies_to(monitor.pk, monitor.user_id))
        .exists()
    )


# ----------------------------- recurrence -----------------------------
def occurrences(template: MaintenanceWindow, since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """UTC (start, end) pairs of the template's occurrences that overlap [since, until]."""
    tz = ZoneInfo(template.timezone)
    local_start = template.starts_at.astimezone(tz).replace(tzinfo=None)
    wall = template.ends_at.astimezone(tz).replace(tzinfo=None) - local_start
    absolute = template.ends_at - template.starts_at
    rule = rrulestr(validate_rrule(template.rrule), dtstart=local_start)
    lo = since.astimezone(tz).replace(tzinfo=None) - wall  # an occurrence that started earlier may still be running
    hi = until.astimezone(tz).replace(tzinfo=None)
    result = []
    for naive in rule.between(lo, hi, inc=True):
        start = naive.replace(tzinfo=tz).astimezone(UTC)
        end = (naive + wall).replace(tzinfo=tz).astimezone(UTC)
        if end <= start:  # the whole window fell into the spring-forward gap
            end = start + absolute
        result.append((start, end))
    return result


def expand_windows(now: datetime | None = None, horizon_days: int = HORIZON_DAYS, template=None) -> int:
    """Materialise concrete occurrences from the recurring templates. Idempotent (unique parent+start)."""
    now = now or timezone.now()
    templates = [template] if template is not None else list(MaintenanceWindow.objects.exclude(rrule=""))
    created = 0
    for t in templates:
        wanted = occurrences(t, now, now + timedelta(days=horizon_days))
        have = set(MaintenanceWindow.objects.filter(parent=t).values_list("starts_at", flat=True))
        new = [
            MaintenanceWindow(
                user_id=t.user_id,
                monitor_id=t.monitor_id,
                starts_at=s,
                ends_at=e,
                reason=t.reason,
                timezone=t.timezone,
                parent=t,
            )
            for s, e in wanted
            if s not in have
        ]
        MaintenanceWindow.objects.bulk_create(new, ignore_conflicts=True)
        created += len(new)
    return created


# ----------------------------- arithmetic for reports -----------------------------
def merge(intervals):
    merged = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def overlap_minutes(monitor, start, end) -> float:
    """Minutes of [start, end) covered by maintenance for this monitor (overlapping windows counted once)."""
    rows = (
        MaintenanceWindow.objects.filter(rrule="", starts_at__lt=end, ends_at__gt=start)
        .filter(applies_to(monitor.pk, monitor.user_id))
        .values_list("starts_at", "ends_at")
    )
    clipped = [(max(s, start), min(e, end)) for s, e in rows]
    return sum((e - s).total_seconds() for s, e in merge(clipped)) / 60


class OverlapIndex:
    """Same numbers as `overlap_minutes`, for many incidents of many monitors with ONE query."""

    def __init__(self, user, start, end):
        rows = MaintenanceWindow.objects.filter(user=user, rrule="", starts_at__lt=end, ends_at__gt=start).values_list(
            "monitor_id", "starts_at", "ends_at"
        )
        self.user_wide, self.by_monitor = [], {}
        for monitor_id, s, e in rows:
            (self.user_wide if monitor_id is None else self.by_monitor.setdefault(monitor_id, [])).append((s, e))

    def minutes(self, monitor_id, start, end) -> float:
        windows = self.user_wide + self.by_monitor.get(monitor_id, [])
        clipped = [(max(s, start), min(e, end)) for s, e in windows if s < end and e > start]
        return sum((e - s).total_seconds() for s, e in merge(clipped)) / 60


def save_from_form(form, monitor) -> MaintenanceWindow:
    """Persist a validated MaintenanceForm: scope (this monitor or all of the user's), then expand a recurring
    rule right away so it takes effect without waiting for the periodic task."""
    window = form.save(commit=False)
    window.user_id = monitor.user_id
    window.monitor = None if form.cleaned_data.get("all_monitors") else monitor
    window.save()
    if window.rrule:
        expand_windows(template=window)
    return window


def visible_for(monitor):
    """What a monitor's maintenance list shows: one-off windows still ahead and recurring templates, both its own
    and the user-wide ones. Occurrences are an implementation detail of a template and are not listed."""
    return (
        MaintenanceWindow.objects.filter(applies_to(monitor.pk, monitor.user_id), parent__isnull=True)
        .filter(~Q(rrule="") | Q(ends_at__gt=timezone.now()))
        .order_by("starts_at")
    )
