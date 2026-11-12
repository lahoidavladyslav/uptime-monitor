import smtplib

import pytest
import responses
from django.core import mail

from monitors.models import Incident
from monitors.tasks import check_monitor, send_incident_notification

URL = "https://example.com"


def run(monitor, django_capture_on_commit_callbacks, status):
    responses.upsert(responses.GET, URL, status=status)
    with django_capture_on_commit_callbacks(execute=True):
        check_monitor(monitor.pk, force=True)


@responses.activate
def test_up_to_down_opens_incident_and_notifies(monitor, django_capture_on_commit_callbacks):
    run(monitor, django_capture_on_commit_callbacks, 200)
    assert not Incident.objects.exists() and not mail.outbox
    run(monitor, django_capture_on_commit_callbacks, 503)
    incident = Incident.objects.get()
    assert incident.resolved_at is None and incident.first_error == "HTTP 503"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["alice@example.com"] and "недоступний" in mail.outbox[0].subject


@responses.activate
def test_down_to_down_does_not_spam(monitor, django_capture_on_commit_callbacks):
    for status in (500, 500, 502):
        run(monitor, django_capture_on_commit_callbacks, status)
    assert Incident.objects.count() == 1
    assert len(mail.outbox) == 1
    assert Incident.objects.get().first_error == "HTTP 500"  # the first error is kept


@responses.activate
def test_down_to_up_resolves(monitor, django_capture_on_commit_callbacks):
    run(monitor, django_capture_on_commit_callbacks, 500)
    run(monitor, django_capture_on_commit_callbacks, 200)
    incident = Incident.objects.get()
    assert incident.resolved_at is not None
    assert [m.subject.startswith(("🔴", "🟢")) for m in mail.outbox] == [True, True]
    assert "знову працює" in mail.outbox[1].subject
    run(monitor, django_capture_on_commit_callbacks, 200)  # up -> up: nothing
    assert len(mail.outbox) == 2

    run(monitor, django_capture_on_commit_callbacks, 500)  # a new outage = a new incident
    assert Incident.objects.count() == 2


@responses.activate
def test_notification_is_queued_only_on_commit(monitor, django_capture_on_commit_callbacks):
    responses.add(responses.GET, URL, status=500)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        check_monitor(monitor.pk, force=True)
    assert len(callbacks) == 1 and not mail.outbox  # nothing sent before commit


def test_notification_retries_on_smtp_error(monitor, monkeypatch):
    from django.utils import timezone

    incident = Incident.objects.create(monitor=monitor, started_at=timezone.now(), first_error="x")
    assert send_incident_notification.max_retries == 3
    assert send_incident_notification.retry_backoff is True
    assert smtplib.SMTPException in send_incident_notification.autoretry_for

    def boom(*a, **k):
        raise smtplib.SMTPException("down")

    monkeypatch.setattr("monitors.tasks.send_mail", boom)
    with pytest.raises(smtplib.SMTPException):  # called directly, celery re-raises the original
        send_incident_notification(incident.pk, "opened")


def test_notification_for_deleted_incident_is_noop(db):
    assert send_incident_notification(999999, "opened") == "gone"
