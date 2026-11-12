import secrets

from django.conf import settings
from django.core.validators import URLValidator
from django.db import models
from django.db.models import Q

http_url_validator = URLValidator(schemes=["http", "https"])


def _new_token():
    return secrets.token_urlsafe(32)


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

    # --- advanced check configuration ---
    METHOD_CHOICES = [("GET", "GET"), ("HEAD", "HEAD"), ("POST", "POST")]
    http_method = models.CharField("Метод", max_length=4, choices=METHOD_CHOICES, default="GET")
    request_headers = models.JSONField("Заголовки", default=dict, blank=True)
    request_body = models.TextField("Тіло запиту", blank=True, default="", max_length=10_000)
    expected_status = models.CharField(
        "Очікувані коди", max_length=100, blank=True, default="", help_text="Напр. 200,204,300-399. Порожньо = 2xx/3xx."
    )
    expected_text = models.CharField(
        "Очікуваний текст", max_length=200, blank=True, default="", help_text="Має бути у тілі відповіді."
    )

    # --- public page branding ---
    public_title = models.CharField("Назва на публічній сторінці", max_length=100, blank=True, default="")
    public_logo_url = models.URLField(
        "Логотип (URL)", max_length=500, blank=True, default="", validators=[http_url_validator]
    )

    # --- SSL certificate expiry (filled by a daily task) ---
    ssl_expires_at = models.DateTimeField(null=True, blank=True)
    ssl_error = models.CharField(max_length=200, blank=True, default="")
    ssl_alerted_level = models.PositiveSmallIntegerField(null=True, blank=True)  # smallest threshold already mailed

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "url"], name="uniq_monitor_user_url"),
        ]

    def __str__(self):
        return self.name

    @property
    def public_name(self):
        return self.public_title or self.name

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


class MaintenanceWindow(models.Model):
    """Planned maintenance: no checks run, so the period doesn't count as downtime."""

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="maintenance_windows")
    starts_at = models.DateTimeField("Початок")
    ends_at = models.DateTimeField("Кінець")
    reason = models.CharField("Причина", max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="maintenance_ends_after_start"),
        ]
        indexes = [models.Index(fields=["monitor", "starts_at", "ends_at"], name="maint_monitor_range_idx")]


class StatusPage(models.Model):
    """A public page that groups several of the owner's monitors under one title."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="status_pages")
    title = models.CharField("Заголовок", max_length=100)
    slug = models.SlugField("Адреса (slug)", max_length=50, unique=True)
    description = models.CharField("Опис", max_length=300, blank=True, default="")
    logo_url = models.URLField("Логотип (URL)", max_length=500, blank=True, default="", validators=[http_url_validator])
    is_public = models.BooleanField("Опублікована", default=True)
    monitors = models.ManyToManyField(Monitor, related_name="status_pages", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title", "id"]

    def __str__(self):
        return self.title


class CheckRollupHourly(models.Model):
    """Per-monitor, per-UTC-hour rollup of CheckResult (built every 5 minutes by `build_rollups`).

    Raw checks live 7 days; these rows are ~1/60th the size, live a year, and are the source for every
    window longer than a day (7d stats, public page, reports, 30/90d uptime).

    Averages are stored as SUM and COUNT, never as a mean (a mean of hourly means is wrong when hours hold
    different numbers of checks). `response_count` is not `checks_total`: failed checks carry no timing.
    """

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="rollups")
    hour = models.DateTimeField()  # UTC, truncated to the hour
    checks_total = models.PositiveIntegerField()
    checks_up = models.PositiveIntegerField()
    sum_response_ms = models.BigIntegerField(default=0)
    response_count = models.PositiveIntegerField(default=0)  # checks that have a response time
    min_response_ms = models.PositiveIntegerField(null=True, blank=True)
    max_response_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["monitor", "hour"], name="uniq_hourly_monitor_hour")]
        indexes = [models.Index(fields=["hour"], name="hourly_hour_idx")]


class Subscriber(models.Model):
    """An email address that wants incident mails for one public status page or one public monitor.

    Double opt-in: a row exists from the moment someone types the address, but it only receives mail
    once `confirmed_at` is set by clicking the emailed link. Unconfirmed rows are purged after the link
    expires. `unsub_token` is a permanent capability for the one-click unsubscribe link in every mail.
    """

    status_page = models.ForeignKey(
        StatusPage, null=True, blank=True, on_delete=models.CASCADE, related_name="subscribers"
    )
    monitor = models.ForeignKey(Monitor, null=True, blank=True, on_delete=models.CASCADE, related_name="subscribers")
    email = models.EmailField(max_length=254)  # stored lower-case
    unsub_token = models.CharField(max_length=64, unique=True, default=_new_token)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status_page__isnull=False, monitor__isnull=True)
                | Q(status_page__isnull=True, monitor__isnull=False),
                name="subscriber_exactly_one_target",
            ),
            models.UniqueConstraint(
                fields=["status_page", "email"],
                condition=Q(status_page__isnull=False),
                name="uniq_subscriber_page_email",
            ),
            models.UniqueConstraint(
                fields=["monitor", "email"], condition=Q(monitor__isnull=False), name="uniq_subscriber_monitor_email"
            ),
        ]

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None
