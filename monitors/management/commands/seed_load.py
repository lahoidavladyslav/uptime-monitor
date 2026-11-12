from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from monitors.models import Monitor

PREFIX = "load-"


class Command(BaseCommand):
    help = (
        "Create N monitors that point at the load-test fake server (see docker-compose.load.yml), spread over "
        "--hosts host names (fake-00 ... ). Idempotent: re-running tops up to N. --clean removes them."
    )

    def add_arguments(self, parser):
        parser.add_argument("--monitors", type=int, default=10_000)
        parser.add_argument("--hosts", type=int, default=50, help="distinct fake host names (per-host limit test)")
        parser.add_argument("--port", type=int, default=8080)
        parser.add_argument("--interval", type=int, default=60, choices=[60, 300, 900])
        parser.add_argument("--user", default="loadtest")
        parser.add_argument("--clean", action="store_true", help="delete every load monitor instead")

    def handle(self, *args, monitors, hosts, port, interval, user, clean, **options):
        if monitors < 1 or hosts < 1:
            raise CommandError("--monitors and --hosts must be positive")
        owner, _ = get_user_model().objects.get_or_create(username=user, defaults={"email": f"{user}@example.com"})
        if clean:
            deleted, _ = Monitor.objects.filter(user=owner, name__startswith=PREFIX).delete()
            self.stdout.write(f"Deleted {deleted} objects.")
            return
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
        have = set(Monitor.objects.filter(user=owner, name__startswith=PREFIX).values_list("name", flat=True))
        new = [
            Monitor(
                user=owner,
                name=f"{PREFIX}{i:05d}",
                url=f"http://fake-{i % hosts:02d}:{port}/m/{i}",
                interval_seconds=interval,
            )
            for i in range(monitors)
            if f"{PREFIX}{i:05d}" not in have
        ]
        Monitor.objects.bulk_create(new, batch_size=1000, ignore_conflicts=True)
        total = Monitor.objects.filter(user=owner, name__startswith=PREFIX).count()
        self.stdout.write(
            self.style.SUCCESS(f"Created {len(new)}; {total} load monitors on {hosts} hosts for user {user!r}.")
        )
