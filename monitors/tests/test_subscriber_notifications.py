"""O2 (part 2): incident notifications to subscribers — batches of 100, idempotent, unsubscribe-aware."""

from datetime import timedelta
from smtplib import SMTPException

import pytest
from django.core import mail
from django.utils import timezone

from monitors import tasks
from monitors.models import Incident, Monitor, StatusSubscriber, SubscriberNotification
from monitors.tasks import notify_subscribers, send_notification_batch


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Сайт", url="https://example.com", is_public=True)


@pytest.fixture
def incident(mon):
    return Incident.objects.create(monitor=mon, started_at=timezone.now(), first_error="secret")


def make_subs(mon, n, prefix="s", **kw):
    now = timezone.now()
    StatusSubscriber.objects.bulk_create(
        [StatusSubscriber(monitor=mon, email=f"{prefix}{i}@example.com", confirmed_at=now, **kw) for i in range(n)]
    )
    return list(StatusSubscriber.objects.filter(monitor=mon, email__startswith=prefix).order_by("pk"))


def mailed():
    return sorted(m.to[0] for m in mail.outbox)


@pytest.fixture
def batches(monkeypatch):
    """Capture the batch tasks instead of running them (so we see how the work was split)."""
    out = []
    monkeypatch.setattr(tasks.send_notification_batch, "delay", lambda *a: out.append(a))
    return out


# ---------------- batching ----------------
def test_the_fan_out_splits_subscribers_into_batches_of_100(mon, incident, batches):
    make_subs(mon, 250)
    assert notify_subscribers(incident.pk, "opened") == 3
    assert [len(ids) for _, _, ids in batches] == [100, 100, 50]
    assert all(a[0] == incident.pk and a[1] == "opened" for a in batches)
    everyone = [i for _, _, ids in batches for i in ids]
    assert len(everyone) == len(set(everyone)) == 250  # nobody twice, nobody missing


def test_no_task_carries_more_than_a_hundred_even_for_ten_thousand(mon, incident, batches):
    make_subs(mon, 1005)
    notify_subscribers(incident.pk, "opened")
    assert max(len(ids) for _, _, ids in batches) == 100 and len(batches) == 11


def test_only_confirmed_and_not_unsubscribed_addresses_are_included(mon, incident, batches):
    live = make_subs(mon, 3, "live")
    make_subs(mon, 2, "gone", unsubscribed_at=timezone.now())
    StatusSubscriber.objects.create(monitor=mon, email="pending@example.com")  # never confirmed
    notify_subscribers(incident.pk, "opened")
    assert sorted(batches[0][2]) == sorted(s.pk for s in live)


def test_a_person_subscribed_to_the_monitor_and_to_its_page_is_one_recipient(user, mon, incident, batches):
    from monitors.models import StatusPage

    page = StatusPage.objects.create(user=user, title="P", slug="p")
    page.monitors.set([mon])
    now = timezone.now()
    StatusSubscriber.objects.create(monitor=mon, email="same@example.com", confirmed_at=now)
    StatusSubscriber.objects.create(status_page=page, email="same@example.com", confirmed_at=now)
    notify_subscribers(incident.pk, "opened")
    assert sum(len(ids) for _, _, ids in batches) == 1


def test_a_deleted_incident_is_a_noop(db):
    assert notify_subscribers(999_999, "opened") == "gone"


# ---------------- sending ----------------
def test_a_batch_mails_every_subscriber_once_with_unsubscribe_headers(mon, incident):
    subs = make_subs(mon, 5)
    result = send_notification_batch(incident.pk, "opened", [s.pk for s in subs])
    assert result["sent"] == 5 and len(mail.outbox) == 5
    first = mail.outbox[0]
    assert "List-Unsubscribe" in first.extra_headers and "List-Unsubscribe-Post" in first.extra_headers
    assert "secret" not in first.body and "example.com/" not in first.body.split("Статус:")[0]  # no private details
    assert SubscriberNotification.objects.filter(sent_at__isnull=False).count() == 5


# ---------------- idempotency ----------------
def test_running_the_same_batch_twice_sends_no_duplicates(mon, incident):
    ids = [s.pk for s in make_subs(mon, 20)]
    send_notification_batch(incident.pk, "opened", ids)
    again = send_notification_batch(incident.pk, "opened", ids)
    assert again["sent"] == 0 and len(mail.outbox) == 20
    assert SubscriberNotification.objects.count() == 20  # one row per (incident, subscriber, event)


def test_running_the_whole_notification_twice_sends_no_duplicates(mon, incident):
    make_subs(mon, 30)
    notify_subscribers(incident.pk, "opened")
    notify_subscribers(incident.pk, "opened")  # e.g. the task was delivered twice by the broker
    assert len(mail.outbox) == 30


def test_opened_and_resolved_are_different_events_and_both_are_mailed(mon, incident):
    ids = [s.pk for s in make_subs(mon, 3)]
    send_notification_batch(incident.pk, "opened", ids)
    Incident.objects.filter(pk=incident.pk).update(resolved_at=timezone.now())
    send_notification_batch(incident.pk, "resolved", ids)
    assert len(mail.outbox) == 6
    assert SubscriberNotification.objects.count() == 6


def test_a_second_incident_mails_again(mon, incident):
    ids = [s.pk for s in make_subs(mon, 2)]
    send_notification_batch(incident.pk, "opened", ids)
    Incident.objects.filter(pk=incident.pk).update(resolved_at=timezone.now())  # one open incident per monitor
    other = Incident.objects.create(monitor=mon, started_at=timezone.now() + timedelta(hours=1))
    send_notification_batch(other.pk, "opened", ids)
    assert len(mail.outbox) == 4


def test_a_mail_that_was_sent_is_never_sent_again_even_after_its_lease_expired(mon, incident):
    """The lease protects the first minutes; `sent_at` protects forever."""
    ids = [s.pk for s in make_subs(mon, 4)]
    send_notification_batch(incident.pk, "opened", ids)
    SubscriberNotification.objects.update(claimed_at=timezone.now() - timedelta(days=1))
    assert send_notification_batch(incident.pk, "opened", ids)["sent"] == 0
    assert len(mail.outbox) == 4


def test_the_uniqueness_is_enforced_by_the_database(mon, incident):
    from django.db import IntegrityError, transaction

    s = make_subs(mon, 1)[0]
    SubscriberNotification.objects.create(incident=incident, subscriber=s, event="opened")
    with pytest.raises(IntegrityError), transaction.atomic():
        SubscriberNotification.objects.create(incident=incident, subscriber=s, event="opened")


def test_a_batch_that_is_already_being_sent_by_another_worker_is_not_sent_again(mon, incident):
    subs = make_subs(mon, 3)
    ids = [s.pk for s in subs]
    for s in subs:  # another worker claimed them a moment ago and has not finished
        SubscriberNotification.objects.create(
            incident=incident, subscriber=s, event="opened", claimed_at=timezone.now()
        )
    assert send_notification_batch(incident.pk, "opened", ids)["sent"] == 0 and not mail.outbox
    SubscriberNotification.objects.update(claimed_at=timezone.now() - tasks.NOTIFICATION_LEASE - timedelta(seconds=1))
    assert send_notification_batch(incident.pk, "opened", ids)["sent"] == 3  # that worker died: the lease expired


# ---------------- unsubscribe during the run ----------------
def test_someone_who_unsubscribes_while_the_batch_is_running_is_not_mailed(mon, incident, monkeypatch):
    first, second, third = make_subs(mon, 3)
    real_send = mail.EmailMessage.send

    def send_and_unsubscribe_the_next_person(self, *a, **kw):
        if self.to == [first.email]:
            StatusSubscriber.objects.filter(pk=second.pk).update(unsubscribed_at=timezone.now())  # clicks the link now
        return real_send(self, *a, **kw)

    monkeypatch.setattr(mail.EmailMessage, "send", send_and_unsubscribe_the_next_person)
    send_notification_batch(incident.pk, "opened", [first.pk, second.pk, third.pk])
    assert mailed() == sorted([first.email, third.email])


def test_someone_who_unsubscribed_before_the_batch_started_is_not_mailed(mon, incident):
    keep, gone = make_subs(mon, 2)
    StatusSubscriber.objects.filter(pk=gone.pk).update(unsubscribed_at=timezone.now())
    send_notification_batch(incident.pk, "opened", [keep.pk, gone.pk])
    assert mailed() == [keep.email]


# ---------------- failures ----------------
def test_a_mail_server_failure_releases_the_unsent_ones_so_the_retry_sends_each_exactly_once(
    mon, incident, monkeypatch
):
    subs = make_subs(mon, 6)
    ids = [s.pk for s in subs]
    real_send, calls = mail.EmailMessage.send, []

    def flaky(self, *a, **kw):
        calls.append(self.to[0])
        if len(calls) == 4:
            raise SMTPException("421 try again later")
        return real_send(self, *a, **kw)

    monkeypatch.setattr(mail.EmailMessage, "send", flaky)
    with pytest.raises(SMTPException):
        send_notification_batch(incident.pk, "opened", ids)
    assert len(mail.outbox) == 3  # three went out before the failure
    assert SubscriberNotification.objects.filter(sent_at__isnull=False).count() == 3
    assert (
        SubscriberNotification.objects.filter(claimed_at__isnull=False, sent_at__isnull=True).count() == 0
    )  # released

    monkeypatch.setattr(mail.EmailMessage, "send", real_send)
    send_notification_batch(incident.pk, "opened", ids)  # Celery's retry
    assert mailed() == sorted(s.email for s in subs)  # everyone exactly once


# ---------------- the janitor ----------------
def test_stalled_notifications_are_picked_up_again_by_the_periodic_resume(mon, incident, batches):
    subs = make_subs(mon, 3)
    old = timezone.now() - tasks.NOTIFICATION_LEASE - timedelta(minutes=1)
    for s in subs:
        SubscriberNotification.objects.create(incident=incident, subscriber=s, event="opened", claimed_at=old)
    SubscriberNotification.objects.create(
        incident=incident, subscriber=make_subs(mon, 1, "done")[0], event="opened", claimed_at=old, sent_at=old
    )
    assert tasks.resume_stalled_notifications() == 1
    assert sorted(batches[0][2]) == sorted(s.pk for s in subs)  # only the unfinished ones


def test_the_resume_ignores_fresh_claims(mon, incident, batches):
    s = make_subs(mon, 1)[0]
    SubscriberNotification.objects.create(incident=incident, subscriber=s, event="opened", claimed_at=timezone.now())
    assert tasks.resume_stalled_notifications() == 0 and batches == []


def test_the_resume_is_scheduled(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["resume-notifications"]
    assert entry["task"] == "monitors.tasks.resume_stalled_notifications"
    assert settings.CELERY_TASK_ROUTES["monitors.tasks.send_notification_batch"] == {"queue": "notifications"}


# ---------------- end to end through the real incident flow ----------------
def test_an_incident_opening_and_closing_mails_the_subscribers_once_each(mon, user, django_capture_on_commit_callbacks):
    import responses

    from monitors.tasks import check_monitor

    make_subs(mon, 4)
    for status in (503, 503, 200):
        with responses.RequestsMock() as rsps, django_capture_on_commit_callbacks(execute=True):
            rsps.add(responses.GET, mon.url, status=status)
            check_monitor(mon.pk, force=True)
    to_subs = [m for m in mail.outbox if m.to[0].startswith("s")]
    assert len(to_subs) == 8  # 4 x opened + 4 x resolved, not 4 x 3 checks
