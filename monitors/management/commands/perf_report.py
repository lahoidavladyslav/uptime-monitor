"""Measure the main pages on the data set made by `seed_volume` and print a markdown (or JSON) report.

For every scenario: median wall time over --runs (after one discarded warm-up), the number of SQL queries, the
time spent in SQL, and with --explain the `EXPLAIN (ANALYZE, BUFFERS)` of the slowest SELECT it issued. When
pg_stat_statements is available the most expensive statements are listed too.

    python manage.py seed_volume          # once (minutes)
    python manage.py perf_report --explain
"""

import json
import re
import statistics
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.utils import DatabaseError
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from monitors.management.commands.seed_volume import PREFIX, USERNAME
from monitors.models import Incident, Monitor
from reports.tasks import build_rows


class Command(BaseCommand):
    help = "Measure dashboard, monitor page, stats, reports... on the seeded data set (reproducible numbers)."

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=5, help="timed runs per scenario (median is reported)")
        parser.add_argument("--explain", action="store_true", help="EXPLAIN (ANALYZE, BUFFERS) of the slowest SELECT")
        parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
        parser.add_argument("--only", help="comma-separated substrings of scenario names")

    def handle(self, *args, runs, explain, format, only, **options):
        user = get_user_model().objects.filter(username=USERNAME).first()
        if user is None or not Monitor.objects.filter(user=user, name__startswith=PREFIX).exists():
            raise CommandError("no seeded data: run `manage.py seed_volume` first")
        mid = (
            Incident.objects.filter(monitor__user=user)
            .order_by("monitor_id")
            .values_list("monitor_id", flat=True)
            .first()
            or Monitor.objects.filter(user=user).values_list("pk", flat=True).first()
        )
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)
        today = timezone.now().date()
        scenarios = [
            ("dashboard (HTML)", lambda: client.get("/").status_code),
            ("dashboard (API)", lambda: client.get("/api/v1/dashboard").status_code),
            ("monitor page (HTML)", lambda: client.get(f"/monitors/{mid}/").status_code),
            ("monitor stats 7d (API)", lambda: client.get(f"/api/v1/monitors/{mid}/stats?period=7d").status_code),
            ("checks list (API)", lambda: client.get(f"/api/v1/monitors/{mid}/checks?page=1").status_code),
            (
                "checks list, failures only (API)",
                lambda: client.get(f"/api/v1/monitors/{mid}/checks?only_failed=true").status_code,
            ),
            ("incidents (API)", lambda: client.get(f"/api/v1/monitors/{mid}/incidents").status_code),
            ("public status page (50 monitors)", lambda: self._public_page(client)),
            ("report CSV, 30 days", lambda: self._report(user, today - timedelta(days=29), today)),
        ]
        if only:
            wanted = [w.strip().lower() for w in only.split(",")]
            scenarios = [s for s in scenarios if any(w in s[0].lower() for w in wanted)]
        has_pgss = self._pg_stat_statements()
        results = [self._measure(name, fn, runs, explain, has_pgss) for name, fn in scenarios]

        if format == "json":
            self.stdout.write(json.dumps({"runs": runs, "scenarios": results}, default=str, indent=2))
            return
        self._markdown(results, explain)

    # ---- scenarios that are not a plain GET ----
    @staticmethod
    def _public_page(client):
        cache.clear()  # the public page is cached for 60 s: measure the real render, not the cache
        return client.get("/page/perf-status/").status_code

    @staticmethod
    def _report(user, start, end):
        for _ in build_rows(user, start, end):
            pass
        return None

    # ---- measuring ----
    @staticmethod
    def _pg_stat_statements():
        """Is the extension installed AND loaded (shared_preload_libraries)? Probed inside a savepoint, so a missing
        extension can never poison the caller's transaction."""
        try:
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
                if cur.fetchone() is None:
                    return False
                cur.execute("SELECT 1 FROM pg_stat_statements LIMIT 1")
            return True
        except DatabaseError:
            return False

    def _measure(self, name, fn, runs, explain, has_pgss):
        fn()  # warm-up: loads pages into the cache, fills the ORM/prepared-statement state
        if has_pgss:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_stat_statements_reset()")
        timings, captured, status = [], None, None
        for _ in range(runs):
            with CaptureQueriesContext(connection) as ctx:
                start = time.perf_counter()
                status = fn()
                timings.append((time.perf_counter() - start) * 1000)
            captured = ctx
        queries = list(captured.captured_queries)
        slowest = max(queries, key=lambda q: float(q["time"]), default=None)
        result = {
            "name": name,
            "ms": round(statistics.median(timings), 1),
            "queries": len(queries),
            "sql_ms": round(sum(float(q["time"]) for q in queries) * 1000, 1),
            "status": status,
            "slowest_sql_ms": round(float(slowest["time"]) * 1000, 1) if slowest else None,
            "slowest_sql": slowest["sql"] if slowest else None,
        }
        if explain and slowest and slowest["sql"].lstrip().upper().startswith("SELECT"):
            result["plan"] = self._explain(slowest["sql"])
        if has_pgss:
            result["top_statements"] = self._top_statements()
        return result

    @staticmethod
    def _explain(sql):
        with connection.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
            return "\n".join(row[0] for row in cur.fetchall())

    @staticmethod
    def _top_statements(limit=3):
        with connection.cursor() as cur:
            cur.execute(
                """SELECT calls, round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 2), left(query, 160)
                   FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                     AND query NOT ILIKE '%%pg_stat_statements%%'
                   ORDER BY total_exec_time DESC LIMIT %s""",
                [limit],
            )
            return [
                {"calls": c, "total_ms": float(t), "mean_ms": float(m), "query": re.sub(r"\s+", " ", q)}
                for c, t, m, q in cur.fetchall()
            ]

    # ---- output ----
    def _markdown(self, results, explain):
        self.stdout.write("(one warm-up run per scenario is discarded; times are the median of the timed runs)")
        self.stdout.write("")
        self.stdout.write("| scenario | median ms | SQL queries | time in SQL ms | slowest query ms |")
        self.stdout.write("|---|---|---|---|---|")
        for r in results:
            self.stdout.write(f"| {r['name']} | {r['ms']} | {r['queries']} | {r['sql_ms']} | {r['slowest_sql_ms']} |")
        for r in results:
            if r.get("top_statements"):
                self.stdout.write(f"\n#### {r['name']}: most expensive statements (pg_stat_statements)")
                for s in r["top_statements"]:
                    self.stdout.write(
                        f"- {s['calls']}x, total {s['total_ms']} ms, mean {s['mean_ms']} ms: `{s['query']}`"
                    )
            if explain and r.get("plan"):
                self.stdout.write(f"\n#### {r['name']}: EXPLAIN (ANALYZE, BUFFERS) of the slowest SELECT")
                self.stdout.write("```\n" + r["plan"] + "\n```")
