import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.db.models import Func, Q, Value
from django.utils import timezone

from accounts.fields import EncryptedTextField

from . import check_types

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

    # --- what is checked (see check_types.py) ---
    check_type = models.CharField("Тип перевірки", max_length=20, choices=check_types.CHOICES, default="http_status")
    check_config = models.JSONField("Налаштування перевірки", default=dict, blank=True)

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

    # set while the monitor changes state too often (see tasks.set_incident_state): alerts are muted meanwhile
    flapping_since = models.DateTimeField(null=True, blank=True)

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

    def clean(self):
        super().clean()
        errors = {}
        try:
            self.check_config = check_types.validate_config(self.check_type, self.check_config)
        except ValidationError as exc:
            for field, messages in exc.message_dict.items():
                if field == "check_type":
                    errors["check_type"] = messages
                else:
                    errors.setdefault("check_config", []).extend(f"{field}: {m}" for m in messages)
        if self.check_type == "ssl_expiry" and not (self.url or "").lower().startswith("https://"):
            errors["url"] = ["Перевірка терміну SSL можлива лише для https-адреси."]
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # An invalid check config must never reach the database, whichever way the monitor is written.
        update_fields = kwargs.get("update_fields")
        if update_fields is None or {"check_type", "check_config", "url"} & set(update_fields):
            self.clean()
        # Slug is generated once, on first publication, and then never changes.
        if self.is_public and not self.public_slug:
            self.public_slug = secrets.token_urlsafe(9)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "public_slug"}
        super().save(*args, **kwargs)


CHECKRESULT_ID_SEQUENCE = "monitors_checkresult_id_seq"


class CheckResult(models.Model):
    """One check from one region. The table is RANGE-partitioned by day on `checked_at` (migration 0010,
    `monitors.partitions`), so Postgres needs the partition key inside every unique key: the primary key is
    `(id, checked_at)`. `id` still comes from one sequence (db_default), Django reads it back with RETURNING."""

    pk = models.CompositePrimaryKey("id", "checked_at")
    id = models.BigIntegerField(
        editable=False,
        db_default=Func(Value(CHECKRESULT_ID_SEQUENCE), function="nextval", output_field=models.BigIntegerField()),
    )
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="checks")
    checked_at = models.DateTimeField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)
    is_up = models.BooleanField()
    error = models.TextField(null=True, blank=True)
    region = models.CharField(max_length=16, default="eu")  # which region's worker made the request
    # stamped at write time: the check ran inside a maintenance window, so it counts towards no uptime/average
    in_maintenance = models.BooleanField(default=False)
    # why the check failed, structured: {"type": "keyword", "status_code": 200, "message": "keyword 'Welcome' not found"}
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-checked_at", "-id"]
        indexes = [
            models.Index(fields=["monitor", "checked_at"], name="check_monitor_time_idx"),
            # "latest result of each region" (quorum, per-region status) is DISTINCT ON (region) over this
            models.Index(fields=["monitor", "region", "checked_at"], name="check_monitor_region_time_idx"),
            # the daily cleanup filters on checked_at alone
            models.Index(fields=["checked_at"], name="check_time_idx"),
        ]

    def __str__(self):
        return f"{self.monitor_id} @ {self.checked_at:%Y-%m-%d %H:%M} {'up' if self.is_up else 'down'}"


class MonitorRegion(models.Model):
    """Per-(monitor, region) schedule clock: the atomic claim that stops a duplicate task of the SAME
    region while the other regions still run their own check."""

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="region_clocks")
    region = models.CharField(max_length=16)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    # set by schedule_checks when this (monitor, region) was put on a queue, cleared when its batch runs:
    # a monitor that is already waiting in line is not enqueued again (backpressure), see tasks.schedule_checks
    queued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["monitor", "region"], name="uniq_monitor_region")]


class Incident(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="incidents")
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    first_error = models.TextField(blank=True, default="")
    during_maintenance = models.BooleanField(default=False)  # was still open while a maintenance window ran

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
    """Planned maintenance. Checks keep running and are stored, but inside a window they open no incident and
    count towards no uptime.

    `monitor=None` means every monitor of `user`. A row with a non-empty `rrule` is a TEMPLATE: it is never a
    window itself, `maintenance.expand_windows` materialises concrete `occurrences` (rows with an empty rrule
    and `parent` set) a month ahead, evaluated as local wall-clock time in `timezone`. Everything that asks
    "is it in maintenance now?" only ever looks at concrete rows.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="maintenance_windows")
    monitor = models.ForeignKey(
        Monitor, null=True, blank=True, on_delete=models.CASCADE, related_name="maintenance_windows"
    )
    starts_at = models.DateTimeField("Початок")
    ends_at = models.DateTimeField("Кінець")
    reason = models.CharField("Причина", max_length=200, blank=True, default="")
    rrule = models.CharField("Повторення (RRULE)", max_length=200, blank=True, default="")
    timezone = models.CharField("Часовий пояс", max_length=64, default="UTC")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="occurrences")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="maintenance_ends_after_start"),
            models.UniqueConstraint(
                fields=["parent", "starts_at"], condition=Q(parent__isnull=False), name="uniq_occurrence_start"
            ),
        ]
        indexes = [models.Index(fields=["monitor", "starts_at", "ends_at"], name="maint_monitor_range_idx")]

    def save(self, *args, **kwargs):
        if self.user_id is None and self.monitor_id is not None:
            self.user_id = self.monitor.user_id
        super().save(*args, **kwargs)

    @property
    def is_template(self):
        return bool(self.rrule)


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


def _new_webhook_secret():
    return secrets.token_urlsafe(32)


class WebhookEndpoint(models.Model):
    """Where a monitor's incident events are POSTed (signed with HMAC-SHA256 using `secret`)."""

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="webhooks")
    url = models.URLField("URL", max_length=500, validators=[http_url_validator])
    # the receiver needs it in clear to verify signatures, so it is encrypted at rest, not hashed
    secret = EncryptedTextField(default=_new_webhook_secret)
    is_active = models.BooleanField("Активний", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class OutboxEvent(models.Model):
    """Transactional outbox: written in the same transaction as the Incident, delivered later, at least once.

    One row per (event x endpoint). `event_id` goes out as X-Event-Id so a receiver can drop duplicates.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        DELIVERED = "delivered", "delivered"
        DEAD = "dead", "dead"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=40)
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-id"]
        indexes = [
            # the dispatcher only ever looks at pending rows, ordered by due time
            models.Index(fields=["next_attempt_at"], condition=Q(status="pending"), name="outbox_pending_due_idx"),
        ]
