from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from monitors.models import CheckResult, HourlyStat
from monitors.tasks import rollup_range


class Command(BaseCommand):
    help = "Create hourly rollups for existing raw checks. Never overwrites rows that already exist."

    def handle(self, *args, **options):
        first = CheckResult.objects.order_by("checked_at").values_list("checked_at", flat=True).first()
        if first is None:
            self.stdout.write("No checks, nothing to do.")
            return
        start = first.replace(minute=0, second=0, microsecond=0)
        end = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        existing = set(HourlyStat.objects.values_list("monitor_id", "hour"))
        # Day-sized chunks keep each aggregate query small.
        total = 0
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=1), end)
            total += (
                rollup_range(cursor, chunk_end) if not existing else self._missing_only(cursor, chunk_end, existing)
            )
            cursor = chunk_end
        self.stdout.write(self.style.SUCCESS(f"Rolled up {total} monitor-hours."))

    def _missing_only(self, start, end, existing):
        """Insert hours that have no rollup yet; leave existing ones (they may be 'older than raw data')."""
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import TruncHour

        rows = (
            CheckResult.objects.filter(checked_at__gte=start, checked_at__lt=end)
            .annotate(hour=TruncHour("checked_at"))
            .values("monitor_id", "hour")
            .annotate(
                total=Count("id"),
                up=Count("id", filter=Q(is_up=True)),
                rt_sum=Sum("response_time_ms"),
                rt_count=Count("response_time_ms"),
            )
        )
        new = [
            HourlyStat(
                monitor_id=r["monitor_id"],
                hour=r["hour"],
                total=r["total"],
                up=r["up"],
                rt_sum=r["rt_sum"] or 0,
                rt_count=r["rt_count"],
            )  # fmt: skip
            for r in rows
            if (r["monitor_id"], r["hour"]) not in existing
        ]
        HourlyStat.objects.bulk_create(new, batch_size=2000, ignore_conflicts=True)
        return len(new)
