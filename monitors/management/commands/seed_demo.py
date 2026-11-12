from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from monitors.models import Monitor


class Command(BaseCommand):
    help = "Create demo user (demo / demo12345) with a few monitors, one deliberately broken."

    def handle(self, *args, **options):
        User = get_user_model()
        user, created = User.objects.get_or_create(username="demo", defaults={"email": "demo@example.com"})
        if created:
            user.set_password("demo12345")
            user.save()
        demo = [
            ("Example", "https://example.com", 60, True),
            ("httpbin 200", "https://httpbin.org/status/200", 300, True),
            ("httpbin 500 (broken)", "https://httpbin.org/status/500", 60, False),
            ("Dead host (broken)", "http://nonexistent.invalid", 60, False),
        ]
        for name, url, interval, public in demo:
            Monitor.objects.get_or_create(
                user=user,
                url=url,
                defaults={"name": name, "interval_seconds": interval, "is_public": public},
            )
        self.stdout.write(self.style.SUCCESS("Demo ready: login demo / demo12345"))
