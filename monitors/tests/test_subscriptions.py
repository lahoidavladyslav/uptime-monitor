import re
from datetime import timedelta

import pytest
from django.core import mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from monitors import subscriptions
from monitors.models import Incident, Monitor, StatusPage, StatusSubscriber
from monitors.tasks import check_monitor, notify_subscribers

SECRET_ERR = "SECRET-ERROR-TEXT"
SECRET_URL = "https://internal-secret.test"


@pytest.fixture
def page(user):
    m = Monitor.objects.create(user=user, name="Web", url=SECRET_URL, public_title="Website")
    p = StatusPage.objects.create(user=user, title="Acme status", slug="acme")
    p.monitors.set([m])
    return p


@pytest.fixture
def pub_monitor(user):
    return Monitor.objects.create(user=user, name="Solo", url="https://solo.test", is_public=True)


def subscribe_url(page):
    return reverse("group_subscribe", args=[page.slug])


def link(pattern):
    return re.search(pattern, mail.outbox[-1].body).group(0)


def confirmed(target, email="fan@example.com"):
    key = "status_page" if isinstance(target, StatusPage) else "monitor"
    return StatusSubscriber.objects.create(**{key: target}, email=email, confirmed_at=timezone.now())


# ---------------- model ----------------
def test_a_subscriber_targets_exactly_one_thing(page, monitor):
    with pytest.raises(IntegrityError), transaction.atomic():
        StatusSubscriber.objects.create(email="a@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        StatusSubscriber.objects.create(email="a@example.com", status_page=page, monitor=monitor)


def test_same_email_cannot_subscribe_twice_to_one_target(page):
    StatusSubscriber.objects.create(status_page=page, email="a@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        StatusSubscriber.objects.create(status_page=page, email="a@example.com")


# ---------------- subscribing ----------------
def test_subscribe_form_is_public_and_not_cached_with_a_csrf_token(client, page):
    r = client.get(subscribe_url(page))
    assert r.status_code == 200 and "csrfmiddlewaretoken" in r.content.decode()
    # the cached status page itself only links to the form (a cached CSRF token would be shared)
    status = client.get(reverse("group_status", args=["acme"])).content.decode()
    assert subscribe_url(page) in status and "csrfmiddlewaretoken" not in status


def test_subscribe_sends_a_confirmation_mail_and_stores_nothing_confirmed(client, page):
    r = client.post(subscribe_url(page), {"email": "Fan@Example.com "})
    assert r.status_code == 200 and "Перевірте пошту" in r.content.decode()
    sub = StatusSubscriber.objects.get()
    assert sub.email == "fan@example.com" and sub.confirmed_at is None
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["fan@example.com"]
    assert "/subscribe/confirm/" in mail.outbox[0].body


def test_invalid_email_is_rejected_without_a_row(client, page):
    r = client.post(subscribe_url(page), {"email": "not-an-email"})
    assert r.status_code == 200 and not StatusSubscriber.objects.exists() and not mail.outbox


def test_answer_is_identical_for_new_pending_and_confirmed_addresses(client, page):
    confirmed(page, "known@example.com")
    a = client.post(subscribe_url(page), {"email": "known@example.com"})
    b = client.post(subscribe_url(page), {"email": "brand-new@example.com"})
    assert a.status_code == b.status_code == 200
    assert a.content.decode() == b.content.decode()
    assert [m.to for m in mail.outbox] == [["brand-new@example.com"]]  # nothing sent to the confirmed one


def test_confirmation_is_not_resent_within_the_cooldown(client, page):
    client.post(subscribe_url(page), {"email": "fan@example.com"})
    client.post(subscribe_url(page), {"email": "fan@example.com"})
    assert len(mail.outbox) == 1
    StatusSubscriber.objects.update(confirmation_sent_at=timezone.now() - timedelta(minutes=11))
    client.post(subscribe_url(page), {"email": "fan@example.com"})
    assert len(mail.outbox) == 2


def test_subscribing_is_rate_limited_per_ip(client, page, settings):
    settings.SUBSCRIBE_LIMIT_PER_HOUR = 3
    codes = [
        client.post(subscribe_url(page), {"email": f"u{i}@example.com"}, REMOTE_ADDR="198.51.100.4").status_code
        for i in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]
    other_ip = client.post(subscribe_url(page), {"email": "z@example.com"}, REMOTE_ADDR="198.51.100.5")
    assert other_ip.status_code == 200


def test_subscriber_cap_per_target_stops_database_flooding(client, page, settings):
    settings.MAX_SUBSCRIBERS_PER_TARGET = 2
    for i in range(4):
        client.post(subscribe_url(page), {"email": f"u{i}@example.com"})
    assert StatusSubscriber.objects.count() == 2


@pytest.mark.parametrize("make_private", ["page", "monitor"])
def test_private_targets_cannot_be_subscribed_to(client, page, pub_monitor, make_private):
    if make_private == "page":
        StatusPage.objects.filter(pk=page.pk).update(is_public=False)
        url = subscribe_url(page)
    else:
        Monitor.objects.filter(pk=pub_monitor.pk).update(is_public=False)
        url = reverse("monitor_subscribe", args=[pub_monitor.public_slug])
    assert client.get(url).status_code == 404
    assert client.post(url, {"email": "a@example.com"}).status_code == 404
    assert not StatusSubscriber.objects.exists()


def test_single_monitor_page_can_be_subscribed_to(client, pub_monitor):
    client.post(reverse("monitor_subscribe", args=[pub_monitor.public_slug]), {"email": "a@example.com"})
    assert StatusSubscriber.objects.get().monitor == pub_monitor


# ---------------- confirming ----------------
def test_confirm_link_activates_the_subscription(client, page):
    client.post(subscribe_url(page), {"email": "fan@example.com"})
    r = client.get(link(r"http://\S+/subscribe/confirm/\S+/"))
    assert r.status_code == 200 and "підписку підтверджено" in r.content.decode().lower()
    assert StatusSubscriber.objects.get().confirmed_at is not None


def test_confirm_is_idempotent(client, page):
    client.post(subscribe_url(page), {"email": "fan@example.com"})
    url = link(r"/subscribe/confirm/\S+/")
    client.get(url)
    first = StatusSubscriber.objects.get().confirmed_at
    assert client.get(url).status_code == 200
    assert StatusSubscriber.objects.get().confirmed_at == first


@pytest.mark.parametrize("token", ["garbage", "a:b:c"])
def test_bad_confirm_token_is_a_400(client, db, token):
    assert client.get(reverse("subscription_confirm", args=[token])).status_code == 400


def test_expired_confirm_token_is_a_400_and_does_not_confirm(client, page):
    from unittest import mock

    sub = StatusSubscriber.objects.create(status_page=page, email="fan@example.com")
    token = subscriptions.confirm_token_for(sub)
    in_four_days = timezone.now().timestamp() + 4 * 24 * 3600
    with mock.patch("django.core.signing.time.time", return_value=in_four_days):
        assert client.get(reverse("subscription_confirm", args=[token])).status_code == 400
    assert StatusSubscriber.objects.get().confirmed_at is None
    assert client.get(reverse("subscription_confirm", args=[token])).status_code == 200  # still fine when fresh


def test_confirm_token_for_a_deleted_subscriber_is_a_400(client, page):
    sub = StatusSubscriber.objects.create(status_page=page, email="fan@example.com")
    token = subscriptions.confirm_token_for(sub)
    sub.delete()
    assert client.get(reverse("subscription_confirm", args=[token])).status_code == 400


def test_unconfirmed_subscribers_are_purged_after_the_token_expires(page):
    from monitors.tasks import cleanup_unconfirmed_subscribers

    old = StatusSubscriber.objects.create(status_page=page, email="old@example.com")
    StatusSubscriber.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=4))
    keep_new = StatusSubscriber.objects.create(status_page=page, email="new@example.com")
    keep_confirmed = confirmed(page, "ok@example.com")
    StatusSubscriber.objects.filter(pk=keep_confirmed.pk).update(created_at=timezone.now() - timedelta(days=40))
    assert cleanup_unconfirmed_subscribers() == 1
    assert set(StatusSubscriber.objects.values_list("pk", flat=True)) == {keep_new.pk, keep_confirmed.pk}


# ---------------- unsubscribing ----------------
def test_unsubscribe_get_does_not_unsubscribe_mail_scanners_prefetch_links(client, page):
    sub = confirmed(page)
    r = client.get(reverse("unsubscribe", args=[sub.unsub_token]))
    assert r.status_code == 200 and StatusSubscriber.objects.filter(pk=sub.pk).exists()
    assert "<form" in r.content.decode()


def test_unsubscribe_post_stamps_the_subscriber_instead_of_deleting_it(client, page):
    sub = confirmed(page)
    r = client.post(reverse("unsubscribe", args=[sub.unsub_token]))
    assert r.status_code == 200 and "відписані" in r.content.decode().lower()
    assert StatusSubscriber.objects.get(pk=sub.pk).unsubscribed_at is not None


def test_one_click_unsubscribe_works_without_a_csrf_token(page):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    sub = confirmed(page)
    r = strict.post(reverse("unsubscribe", args=[sub.unsub_token]), {"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and StatusSubscriber.objects.get().unsubscribed_at is not None


def test_unknown_unsubscribe_token_answers_like_a_known_one(client, page):
    sub = confirmed(page)
    known = client.post(reverse("unsubscribe", args=[sub.unsub_token]))
    unknown = client.post(reverse("unsubscribe", args=["no-such-token"]))
    assert known.status_code == unknown.status_code == 200
    assert known.content == unknown.content


def test_tokens_are_unguessable_and_unique(page):
    a, b = confirmed(page, "a@example.com"), confirmed(page, "b@example.com")
    assert a.unsub_token != b.unsub_token and len(a.unsub_token) >= 30


# ---------------- notifications ----------------
def test_incident_open_and_resolve_mail_confirmed_subscribers(page, django_capture_on_commit_callbacks):
    import responses

    m = page.monitors.get()
    confirmed(page, "fan@example.com")
    StatusSubscriber.objects.create(status_page=page, email="pending@example.com")  # not confirmed: no mail
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, m.url, status=503)
        with django_capture_on_commit_callbacks(execute=True):
            check_monitor(m.pk, force=True)
    to_fan = [x for x in mail.outbox if x.to == ["fan@example.com"]]
    assert len(to_fan) == 1 and "Website" in to_fan[0].subject and "недоступний" in to_fan[0].subject
    assert not [x for x in mail.outbox if x.to == ["pending@example.com"]]
    # the owner still gets their own mail
    assert any(x.to == ["alice@example.com"] for x in mail.outbox)

    mail.outbox.clear()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, m.url, status=200)
        with django_capture_on_commit_callbacks(execute=True):
            check_monitor(m.pk, force=True)
    resolved = [x for x in mail.outbox if x.to == ["fan@example.com"]]
    assert len(resolved) == 1 and "знову працює" in resolved[0].subject


def test_subscriber_mail_leaks_nothing_private_and_carries_unsubscribe_headers(page, settings):
    m = page.monitors.get()
    sub = confirmed(page)
    inc = Incident.objects.create(monitor=m, started_at=timezone.now(), first_error=SECRET_ERR)
    notify_subscribers(inc.pk, "opened")
    msg = next(x for x in mail.outbox if x.to == [sub.email])
    everything = msg.subject + msg.body + str(msg.extra_headers)
    for leaked in (SECRET_URL, "internal-secret", SECRET_ERR, "alice", "Web "):
        assert leaked not in everything, leaked
    unsub = f"{settings.SITE_URL}{reverse('unsubscribe', args=[sub.unsub_token])}"
    assert unsub in msg.body
    assert msg.extra_headers["List-Unsubscribe"] == f"<{unsub}>"
    assert msg.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_a_person_subscribed_to_page_and_monitor_gets_one_mail(user, page):
    m = page.monitors.get()
    Monitor.objects.filter(pk=m.pk).update(is_public=True)
    m.refresh_from_db()
    confirmed(page, "fan@example.com")
    confirmed(m, "fan@example.com")
    inc = Incident.objects.create(monitor=m, started_at=timezone.now())
    notify_subscribers(inc.pk, "opened")
    assert len([x for x in mail.outbox if x.to == ["fan@example.com"]]) == 1


def test_no_mail_when_the_page_or_monitor_was_made_private_since(page):
    m = page.monitors.get()
    confirmed(page)
    StatusPage.objects.filter(pk=page.pk).update(is_public=False)
    inc = Incident.objects.create(monitor=m, started_at=timezone.now())
    notify_subscribers(inc.pk, "opened")
    assert not mail.outbox


def test_monitor_subscribers_only_get_their_own_monitor(user, pub_monitor):
    other = Monitor.objects.create(user=user, name="Other", url="https://o.test", is_public=True)
    confirmed(pub_monitor, "fan@example.com")
    inc = Incident.objects.create(monitor=other, started_at=timezone.now())
    notify_subscribers(inc.pk, "opened")
    assert not mail.outbox


def test_notify_for_a_deleted_incident_is_a_noop(db):
    assert notify_subscribers(999_999, "opened") == "gone"


def test_subscription_lifecycle_is_logged_by_id_never_by_address(client, page, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="subscriptions")
    client.post(subscribe_url(page), {"email": "fan@example.com"})
    client.get(link(r"/subscribe/confirm/\S+/"))
    sub = StatusSubscriber.objects.get()
    client.post(reverse("unsubscribe", args=[sub.unsub_token]))
    events = [(r.event, r.subscriber_id) for r in caplog.records if r.name == "subscriptions"]
    assert events == [
        ("subscription_requested", sub.pk),
        ("subscription_confirmed", sub.pk),
        ("unsubscribed", sub.pk),
    ]
    assert "fan@example.com" not in str([r.__dict__ for r in caplog.records if r.name == "subscriptions"])
    assert sub.unsub_token not in str([r.__dict__ for r in caplog.records])
