from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from monitors import loadstats


class Command(BaseCommand):
    help = "Print throughput, latency and backlog of the check pipeline over the last N minutes."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=5)

    def handle(self, *args, minutes, **options):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute(
                """SELECT date_trunc('minute', checked_at) AS m, count(*),
                          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY response_time_ms)),
                          round(percentile_cont(0.95) WITHIN GROUP (ORDER BY response_time_ms)),
                          count(*) FILTER (WHERE NOT is_up)
                   FROM monitors_checkresult
                   WHERE checked_at >= %s AND checked_at < date_trunc('minute', %s::timestamptz)
                   GROUP BY 1 ORDER BY 1""",
                [now - timedelta(minutes=minutes + 1), now],
            )
            rows = cur.fetchall()
        self.stdout.write("minute (UTC)           checks   p50 ms   p95 ms   failed")
        for m, n, p50, p95, bad in rows:
            self.stdout.write(f"{m:%Y-%m-%d %H:%M}   {n:8d} {p50 or 0:8.0f} {p95 or 0:8.0f} {bad:8d}")
        if rows:
            per_min = sum(r[1] for r in rows) / len(rows)
            self.stdout.write(f"average: {per_min:.0f} checks/min over {len(rows)} full minutes")
        self.stdout.write(f"overdue (>30 s past slot): {loadstats.overdue_checks(now)}")
        self.stdout.write(f"queue lengths: {loadstats.queue_lengths()}")
