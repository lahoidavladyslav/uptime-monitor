"""Daily range partitions of `monitors_checkresult` (PARTITION BY RANGE (checked_at), UTC days).

Why partitions and not batched DELETEs: dropping a day is a catalog operation. A DELETE of N rows writes N
tombstones, N WAL records (plus index entries), leaves the dead space for VACUUM and bloats the table and its
indexes; `DROP TABLE` of the partition writes almost no WAL, returns the disk space at once and leaves nothing
to vacuum. Queries with a `checked_at` range only visit the partitions of that range (partition pruning).
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from . import rollups

logger = logging.getLogger(__name__)

PARENT = "monitors_checkresult"
DEFAULT = f"{PARENT}_default"
AHEAD_DAYS = 3  # partitions are created this many days before they are needed
LOCK_KEY = 7_302_026  # advisory lock: one partition manager at a time
_NAME_RE = re.compile(rf"^{PARENT}_p(\d{{8}})$")


def partition_name(day: date) -> str:
    return f"{PARENT}_p{day:%Y%m%d}"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    lo = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return lo, lo + timedelta(days=1)


def _lit(dt: datetime) -> str:
    """A timestamptz literal for DDL (utility statements cannot take bind parameters)."""
    return f"'{dt.astimezone(UTC):%Y-%m-%d %H:%M:%S}+00'"


def _fetch(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _run(sql, params=()):
    with connection.cursor() as cur:
        cur.execute(sql, params)


def exists(name: str) -> bool:
    return _fetch("SELECT to_regclass(%s) IS NOT NULL", [name])[0][0]


def daily_partitions() -> dict[date, str]:
    """{day: partition table name} for every attached daily partition."""
    rows = _fetch(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = %s::regclass",
        [PARENT],
    )
    found = {}
    for (name,) in rows:
        m = _NAME_RE.match(name)
        if m:
            found[datetime.strptime(m.group(1), "%Y%m%d").date()] = name
    return found


def default_rows(limit: int = 1_000_000) -> int:
    """Rows in the safety-net partition. It must be 0: anything there means a partition was missing."""
    return _fetch(f"SELECT count(*) FROM (SELECT 1 FROM {DEFAULT} LIMIT %s) s", [limit])[0][0]


def _lock():
    _run("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])


def ensure_partition(day: date) -> bool:
    """Create the partition for `day`. True if it was created now.

    If the default partition already holds rows of that day, `CREATE TABLE ... PARTITION OF` would fail
    ("updated partition constraint for default partition would be violated by some row"), so those rows are
    ADOPTED instead: build a standalone table, move the rows out of default into it, then ATTACH it (a CHECK
    constraint equal to the range lets ATTACH skip re-validating the rows).
    """
    name = partition_name(day)
    lo, hi = day_bounds(day)
    with transaction.atomic():
        _lock()
        if exists(name):
            return False
        has_rows = _fetch(
            f"SELECT EXISTS (SELECT 1 FROM {DEFAULT} WHERE checked_at >= %s AND checked_at < %s)", [lo, hi]
        )[0][0]
        if not has_rows:
            _run(f"CREATE TABLE {name} PARTITION OF {PARENT} FOR VALUES FROM ({_lit(lo)}) TO ({_lit(hi)})")
            return True
        _run(f"CREATE TABLE {name} (LIKE {PARENT} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)")
        _run(
            f"ALTER TABLE {name} ADD CONSTRAINT {name}_range CHECK (checked_at >= {_lit(lo)} AND checked_at < {_lit(hi)})"
        )
        _run(
            f"WITH moved AS (DELETE FROM {DEFAULT} WHERE checked_at >= %s AND checked_at < %s RETURNING *) "
            f"INSERT INTO {name} SELECT * FROM moved",
            [lo, hi],
        )
        _run(f"ALTER TABLE {PARENT} ATTACH PARTITION {name} FOR VALUES FROM ({_lit(lo)}) TO ({_lit(hi)})")
        _run(f"ALTER TABLE {name} DROP CONSTRAINT {name}_range")
        return True


def adopt_default_rows(cutoff: datetime, today: date) -> int:
    """Give default-partition rows of the days we care about (retention window .. today + AHEAD) a real
    partition. Rows outside that window (a clock-skewed or bogus timestamp such as year 2099) stay in default,
    visible in /metrics, instead of minting a partition per bogus day."""
    lo = cutoff.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    hi = day_bounds(today + timedelta(days=AHEAD_DAYS))[1]
    days = _fetch(
        f"SELECT DISTINCT (checked_at AT TIME ZONE 'UTC')::date FROM {DEFAULT} "
        "WHERE checked_at >= %s AND checked_at < %s ORDER BY 1",
        [lo, hi],
    )
    for (day,) in days:
        ensure_partition(day)
    return len(days)


def purge_default(cutoff: datetime) -> int:
    """Default-partition rows older than retention (they never had a partition to be dropped with): roll them
    up, then delete in batches."""
    oldest = _fetch(f"SELECT min(checked_at) FROM {DEFAULT} WHERE checked_at < %s", [cutoff])[0][0]
    if oldest is None:
        return 0
    rollups.build(rollups.floor_hour(oldest), cutoff)
    total = 0
    while True:
        with connection.cursor() as cur:
            cur.execute(
                f"DELETE FROM {DEFAULT} WHERE ctid IN (SELECT ctid FROM {DEFAULT} WHERE checked_at < %s LIMIT %s)",
                [cutoff, settings.CLEANUP_BATCH_SIZE],
            )
            if cur.rowcount == 0:
                return total
            total += cur.rowcount


def drop_expired(cutoff: datetime) -> list[date]:
    """DROP every daily partition that lies wholly before `cutoff` - after rolling it up one more time, so
    that nothing is ever dropped that is not already summarised (the periodic job only recomputes 2 hours)."""
    dropped = []
    for day, name in sorted(daily_partitions().items()):
        lo, hi = day_bounds(day)
        if hi > cutoff:
            continue
        with transaction.atomic():
            _lock()
            rollups.build(lo, hi)
            _run(f"DROP TABLE {name}")
        dropped.append(day)
    return dropped


def manage(now: datetime | None = None) -> dict:
    """The hourly housekeeping: partitions ahead, adoption, purge, drop, rollup retention."""
    from .models import CheckRollupHourly

    now = now or timezone.now()
    today = now.astimezone(UTC).date()
    cutoff = rollups.floor_hour(now - timedelta(days=settings.CHECK_RETENTION_DAYS))
    created = [d for d in (today + timedelta(days=i) for i in range(AHEAD_DAYS + 1)) if ensure_partition(d)]
    adopted_days = adopt_default_rows(cutoff, today)
    purged = purge_default(cutoff)
    dropped = drop_expired(cutoff)
    CheckRollupHourly.objects.filter(hour__lt=now - timedelta(days=settings.ROLLUP_RETENTION_DAYS)).delete()
    stray = default_rows()
    if stray:
        logger.warning(
            "default partition of checkresult is not empty (%s rows): a partition was missing or a timestamp is bogus",
            stray,
            extra={"event": "checkresult_default_partition", "rows": stray},
        )
    return {"created": created, "adopted_days": adopted_days, "purged": purged, "dropped": dropped, "stray": stray}
