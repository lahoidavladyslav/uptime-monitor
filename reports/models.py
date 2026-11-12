from django.conf import settings
from django.db import models


class ReportExport(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "В черзі"
        PROCESSING = "processing", "Генерується"
        DONE = "done", "Готово"
        FAILED = "failed", "Помилка"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exports")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    file = models.FileField(upload_to="reports/%Y/%m/", null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def in_progress(self):
        return self.status in (self.Status.PENDING, self.Status.PROCESSING)
