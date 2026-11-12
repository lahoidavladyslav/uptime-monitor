from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import LoginEvent


@shared_task
def cleanup_login_events():
    cutoff = timezone.now() - timedelta(days=settings.LOGIN_EVENT_RETENTION_DAYS)
    deleted, _ = LoginEvent.objects.filter(created_at__lt=cutoff).delete()
    return deleted
