from datetime import UTC, date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from monitors import rollups
from monitors.models import CheckResult


def parse_day(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not a date (use YYYY-MM-DD)") from exc


class Command(BaseCommand):
    help = (
        "Rebuild hourly rollups from raw checks for [--from, --to] (UTC days, inclusive), one day per statement. "
        "Existing rows are overwritten; hours whose raw rows were already cleaned up are left untouched."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (default: oldest raw check)")
        parser.add_argument("--to", dest="end", help="last day, inclusive, YYYY-MM-DD (default: today)")

    def handle(self, *args, start=None, end=None, **options):
        first_day = parse_day(start) if start else None
        last = parse_day(end) if end else timezone.now().astimezone(UTC).date()
        if first_day and first_day > last:
            raise CommandError(f"--from {first_day} is after --to {last}")
        first_raw = CheckResult.objects.order_by("checked_at").values_list("checked_at", flat=True).first()
        if first_raw is None:
            self.stdout.write("No raw checks, nothing to do.")
            return
        first = first_day or first_raw.astimezone(UTC).date()
        total = 0
        day = first
        while day <= last:  # a day per statement keeps each INSERT ... SELECT small and resumable
            day_start = datetime.combine(day, time.min, tzinfo=UTC)
            total += rollups.build(day_start, day_start + timedelta(days=1))
            day += timedelta(days=1)
        self.stdout.write(self.style.SUCCESS(f"Rebuilt {total} monitor-hours ({first} .. {last})."))
