"""O2 (part 1): StatusSubscriber — unique per address ignoring case, soft unsubscribe, 48 h confirmation, 5/h limit."""

from datetime import timedelta

import pytest
import time_machine
from django.core import mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from monitors import subscriptions
from monitors.models import Monitor, StatusPage, StatusSubscriber


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Сайт", url="https://example.com", is_public=True)


@pytest.fixture
def page(user, mon):
    p = StatusPage.objects.create(user=user, title="Acme", slug="acme")
    p.monitors.set([mon])
    return p


def sub(target, email="fan@example.com", confirmed=True, **kw):
    key = "monitor" if isinstance(target, Monitor) else "status_page"
    return StatusSubscriber.objects.create(
        **{key: target}, email=email, confirmed_at=timezone.now() if confirmed else None, **kw
    )


# ---------------- the model ----------------
def test_the_model_has_the_fields_of_the_brief(mon):
    s = sub(mon)
    assert (s.monitor_id, s.email, s.unsubscribed_at) == (mon.pk, "fan@example.com", None)
    assert s.confirmed_at and s.created_at


@pytest.mark.parametrize("target_fixture", ["mon", "page"])
def test_an_address_is_unique_per_target_ignoring_case_even_when_written_around_the_view(request, target_fixture):
    target = request.getfixturevalue(target_fixture)
    sub(target, "Fan@Example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        sub(target, "fan@example.com")  # same address, different case


def test_the_same_address_may_follow_two_different_monitors(user, mon):
    other = Monitor.objects.create(user=user, name="B", url="https://b.example.com", is_public=True)
    sub(mon)
    sub(other)  # no error


# ---------------- soft unsubscribe ----------------
def test_unsubscribing_keeps_the_row_and_stamps_the_time(client, mon):
    s = sub(mon)
    r = client.post(reverse("unsubscribe", args=[s.unsub_token]))
    assert r.status_code == 200
    s.refresh_from_db()
    assert s.unsubscribed_at is not None  # not deleted: we can show counts and prove the opt-out


def test_unsubscribing_twice_changes_nothing(client, mon):
    s = sub(mon)
    client.post(reverse("unsubscribe", args=[s.unsub_token]))
    s.refresh_from_db()
    first = s.unsubscribed_at
    client.post(reverse("unsubscribe", args=[s.unsub_token]))
    s.refresh_from_db()
    assert s.unsubscribed_at == first


def test_an_unsubscribed_address_gets_no_incident_mail(mon):
    from monitors.models import Incident
    from monitors.tasks import notify_subscribers

    s = sub(mon)
    StatusSubscriber.objects.filter(pk=s.pk).update(unsubscribed_at=timezone.now())
    inc = Incident.objects.create(monitor=mon, started_at=timezone.now())
    notify_subscribers(inc.pk, "opened")
    assert not mail.outbox


def test_subscribing_again_after_unsubscribing_needs_a_fresh_confirmation(client, mon):
    s = sub(mon, unsubscribed_at=timezone.now() - timedelta(days=1))
    client.post(reverse("monitor_subscribe", args=[mon.public_slug]), {"email": "fan@example.com"})
    s.refresh_from_db()
    assert s.unsubscribed_at is None and s.confirmed_at is None  # back to pending: the old consent is gone
    assert len(mail.outbox) == 1 and "/subscribe/confirm/" in mail.outbox[0].body
    assert StatusSubscriber.objects.count() == 1  # the same row, not a duplicate


def test_an_unsubscribed_address_looks_exactly_like_any_other_to_the_visitor(client, mon):
    sub(mon, "gone@example.com", unsubscribed_at=timezone.now())
    a = client.post(reverse("monitor_subscribe", args=[mon.public_slug]), {"email": "gone@example.com"})
    b = client.post(reverse("monitor_subscribe", args=[mon.public_slug]), {"email": "new@example.com"})
    assert a.status_code == b.status_code == 200
    assert a.content.decode().replace("gone@example.com", "X") == b.content.decode().replace("new@example.com", "X")


# ---------------- confirmation lives 48 hours ----------------
def test_the_confirmation_link_works_for_48_hours_and_not_a_minute_longer(client, mon):
    s = sub(mon, confirmed=False)
    t0 = timezone.now()
    with time_machine.travel(t0, tick=False):
        token = subscriptions.confirm_token_for(s)
    with time_machine.travel(t0 + timedelta(hours=47, minutes=59), tick=False):
        assert client.get(reverse("subscription_confirm", args=[token])).status_code == 200
    s.refresh_from_db()
    assert s.confirmed_at is not None
    StatusSubscriber.objects.filter(pk=s.pk).update(confirmed_at=None)
    with time_machine.travel(t0 + timedelta(hours=48, minutes=1), tick=False):
        assert client.get(reverse("subscription_confirm", args=[token])).status_code == 400
    s.refresh_from_db()
    assert s.confirmed_at is None


def test_a_forged_or_altered_token_is_rejected(client, mon):
    s = sub(mon, confirmed=False)
    token = subscriptions.confirm_token_for(s)
    other = sub(mon, "other@example.com", confirmed=False)
    forged = (
        token.split(":")[0] + ":" + subscriptions.confirm_token_for(other).split(":", 1)[1]
    )  # id of A, signature of B
    for bad in (forged, token[:-2] + "xx", "x" + token):
        assert client.get(reverse("subscription_confirm", args=[bad])).status_code == 400
    s.refresh_from_db()
    assert s.confirmed_at is None


# ---------------- abuse protection ----------------
def test_no_more_than_five_subscriptions_per_ip_per_hour(client, mon):
    codes = [
        client.post(
            reverse("monitor_subscribe", args=[mon.public_slug]),
            {"email": f"u{i}@example.com"},
            REMOTE_ADDR="198.51.100.9",
        ).status_code
        for i in range(7)
    ]
    assert codes == [200] * 5 + [429] * 2
    assert StatusSubscriber.objects.count() == 5


def test_a_repeat_request_for_the_same_address_mails_at_most_once_per_ten_minutes(client, mon):
    url = reverse("monitor_subscribe", args=[mon.public_slug])
    client.post(url, {"email": "fan@example.com"})
    client.post(url, {"email": "FAN@example.com"})  # different case, same person
    assert len(mail.outbox) == 1
    StatusSubscriber.objects.update(confirmation_sent_at=timezone.now() - timedelta(minutes=11))
    client.post(url, {"email": "fan@example.com"})
    assert len(mail.outbox) == 2


def test_cleanup_counts_the_age_from_the_last_confirmation_mail_not_from_the_row_creation(mon):
    """A re-subscribed old row is pending again: it must not be purged the moment the cleanup runs."""
    from monitors.tasks import cleanup_unconfirmed_subscribers

    old_row_fresh_mail = sub(mon, "again@example.com", confirmed=False, confirmation_sent_at=timezone.now())
    StatusSubscriber.objects.filter(pk=old_row_fresh_mail.pk).update(created_at=timezone.now() - timedelta(days=30))
    stale = sub(mon, "stale@example.com", confirmed=False, confirmation_sent_at=timezone.now() - timedelta(days=3))
    unsubscribed = sub(mon, "bye@example.com", confirmed=False, unsubscribed_at=timezone.now() - timedelta(days=60))
    StatusSubscriber.objects.filter(pk=unsubscribed.pk).update(created_at=timezone.now() - timedelta(days=90))
    assert cleanup_unconfirmed_subscribers() == 1
    assert set(StatusSubscriber.objects.values_list("pk", flat=True)) == {old_row_fresh_mail.pk, unsubscribed.pk}
    assert not StatusSubscriber.objects.filter(pk=stale.pk).exists()
