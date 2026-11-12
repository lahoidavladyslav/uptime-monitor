"""Multi-region checks: which regions exist, what each one currently sees, and the quorum rule.

The quorum is a PURE function of "the latest result of each region" (`decide`). It runs under the
monitor's row lock after every write, so the order in which regions report can't change the outcome and two
regions finishing together can't both open an incident.
"""

from datetime import timedelta

from django.conf import settings

from .models import CheckResult

VOTE_WINDOW = timedelta(minutes=2)  # results further apart than this are not "the same moment"
MIN_SILENCE = timedelta(minutes=5)  # a region with no result for this long (or 2 intervals) has dropped out


def default_region() -> str:
    return settings.CHECK_REGION or settings.CHECK_REGIONS[0]


def silence_threshold(monitor) -> timedelta:
    return max(MIN_SILENCE, timedelta(seconds=2 * monitor.interval_seconds))


def latest_by_region(monitor, since):
    """{region: its newest result since `since`} in one DISTINCT ON query (index: monitor, region, checked_at)."""
    rows = (
        CheckResult.objects.filter(monitor=monitor, checked_at__gte=since, region__in=settings.CHECK_REGIONS)
        .order_by("region", "-checked_at", "-id")
        .distinct("region")
        .only("region", "checked_at", "is_up")
    )
    return {r.region: r for r in rows}


def decide(monitor, trigger) -> bool | None:
    """True = the monitor is up, False = down, None = no quorum yet (leave the incident state alone).

    - A region's vote is its latest result, if it is within VOTE_WINDOW of the result being processed.
    - Participants are the regions that reported within the silence threshold, plus regions that have not
      reported *yet* while the monitor is still young (so a brand-new monitor's first round can't be decided
      by whichever region answers first). Silent regions are dropped: neither "up" nor "down".
    - need = participants // 2 + 1   (2 of 3, 2 of 2, 1 of 1).
    """
    configured = list(settings.CHECK_REGIONS)
    if trigger.region not in configured:  # legacy rows / a worker started with an unknown region
        return trigger.is_up
    reference = trigger.checked_at
    silence = silence_threshold(monitor)
    latest = latest_by_region(monitor, reference - silence)
    latest[trigger.region] = trigger  # also covers a result older than the others' (out-of-order arrival)
    young = monitor.created_at > reference - silence
    participants = [r for r in configured if r in latest or young]
    need = len(participants) // 2 + 1
    votes = [row for row in latest.values() if row.checked_at >= reference - VOTE_WINDOW]
    down = sum(1 for v in votes if not v.is_up)
    up = len(votes) - down
    if down >= need:
        return False
    if up >= need:
        return True
    return None


def region_status(monitor, now=None) -> list[dict]:
    """For the monitor page: each region's current state: up, down, stale (silent) or unknown (not yet)."""
    from django.utils import timezone

    now = now or timezone.now()
    silence = silence_threshold(monitor)
    latest = latest_by_region(monitor, now - timedelta(days=7))
    young = monitor.created_at > now - silence
    out = []
    for region in settings.CHECK_REGIONS:
        row = latest.get(region)
        if row is None:
            status, at = ("unknown" if young else "stale"), None
        elif now - row.checked_at > silence:
            status, at = "stale", row.checked_at
        else:
            status, at = ("up" if row.is_up else "down"), row.checked_at
        out.append({"region": region, "status": status, "last_checked_at": at})
    return out
