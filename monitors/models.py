import secrets

from django.conf import settings
from django.core.validators import URLValidator
from django.db import models
from django.db.models import Q

http_url_validator = URLValidator(schemes=["http", "https"])


class Monitor(models.Model):
    INTERVAL_CHOICES = [(60, "1 хвилина"), (300, "5 хвилин"), (900, "15 хвилин")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="monitors")
    name = models.CharField("Назва", max_length=100)
    url = models.URLField("URL", max_length=500, validators=[http_url_validator])
    interval_seconds = models.PositiveIntegerField("Інтервал", choices=INTERVAL_CHOICES, default=300)
    is_active = models.BooleanField("Активний", default=True)
    is_public = models.BooleanField("Публічний", default=False)
    public_slug = models.CharField(max_length=32, unique=True, null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "url"], name="uniq_monitor_user_url"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Slug is generated once, on first publication, and then never changes.
        if self.is_public and not self.public_slug:
            self.public_slug = secrets.token_urlsafe(9)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "public_slug"}
        super().save(*args, **kwargs)


class CheckResult(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="checks")
    checked_at = models.DateTimeField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)
    is_up = models.BooleanField()
    error = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-checked_at", "-id"]
        indexes = [
            models.Index(fields=["monitor", "checked_at"], name="check_monitor_time_idx"),
            # the daily cleanup filters on checked_at alone
            models.Index(fields=["checked_at"], name="check_time_idx"),
        ]

    def __str__(self):
        return f"{self.monitor_id} @ {self.checked_at:%Y-%m-%d %H:%M} {'up' if self.is_up else 'down'}"


class Incident(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="incidents")
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    first_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["monitor"],
                condition=Q(resolved_at__isnull=True),
                name="uniq_open_incident_per_monitor",
            ),
        ]

    @property
    def is_open(self):
        return self.resolved_at is None
