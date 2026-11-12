"""H6: one 'unstable' notice instead of a pile of alerts, then silence until the monitor settles."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail

from monitors.models import Incident, Monitor, OutboxEvent, Subscriber, WebhookEndpoint
from monitors.tasks import record_check

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def mon(user):
    return Monitor.objects.create(user=user, name="Сайт", url="https://example.com")


@pytest.fixture
def run(django_capture_on_commit_callbacks):
    """flip(mon, minute, is_up): one check result at T0+minute, with its on_commit mails executed."""

    def flip(m, minute, is_up):
        with django_capture_on_commit_callbacks(execute=True):
            record_check(
                m.pk, "eu", T0 + timedelta(minutes=minute), 200 if is_up else 503, 10, is_up, None if is_up else "boom"
            )

    return flip


def to(address):
    return [m for m in mail.outbox if m.to == [address]]


def subjects():
    return [m.subject for m in mail.outbox if m.to == ["alice@example.com"]]


def alternate(run, m, count, start=0, step=2):
    """`count` state changes, `step` minutes apart, starting from 'up' at `start` (so the 1st check is down)."""
    for i in range(count):
        run(m, start + i * step, i % 2 == 1)


def test_four_changes_in_fifteen_minutes_are_still_ordinary_alerts(mon, run):
    run(mon, 0, True)
    alternate(run, mon, 4, start=1)  # down, up, down, up  => 4 transitions
    assert len(subjects()) == 4 and not any("нестабіл" in s for s in subjects())
    mon.refresh_from_db()
    assert mon.flapping_since is None


def test_the_fifth_change_sends_one_unstable_notice_instead_of_an_alert(mon, run):
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)  # 5 transitions inside 15 min
    s = subjects()
    assert len(s) == 5 and sum("нестабіл" in x for x in s) == 1 and "нестабіл" in s[-1]
    mon.refresh_from_db()
    assert mon.flapping_since is not None


def test_further_changes_are_muted_but_still_recorded(mon, run, user):
    Subscriber.objects.create(
        monitor=Monitor.objects.filter(pk=mon.pk).first(), email="fan@example.com", confirmed_at=T0
    )
    Monitor.objects.filter(pk=mon.pk).update(is_public=True)
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)
    before_owner, before_fan = len(subjects()), len(to("fan@example.com"))
    for i in range(6, 10):  # more flapping
        run(mon, 1 + i * 2, i % 2 == 1)
    assert len(subjects()) == before_owner and len(to("fan@example.com")) == before_fan  # silence
    assert Incident.objects.filter(monitor=mon).count() >= 4  # ...but the history is complete


def test_webhooks_get_one_flapping_event_and_no_incident_events_while_muted(mon, run):
    WebhookEndpoint.objects.create(monitor=mon, url="https://hooks.example.org/x")
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)
    types = list(OutboxEvent.objects.order_by("id").values_list("event_type", flat=True))
    assert types.count("monitor.flapping") == 1 and types[-1] == "monitor.flapping"
    n = len(types)
    for i in range(6, 10):
        run(mon, 1 + i * 2, i % 2 == 1)
    assert OutboxEvent.objects.count() == n


def test_changes_spread_over_more_than_fifteen_minutes_do_not_count_as_flapping(mon, run):
    run(mon, 0, True)
    alternate(run, mon, 6, start=1, step=4)  # a change every 4 minutes: at most 4 inside any 15-minute window
    assert not any("нестабіл" in s for s in subjects())
    mon.refresh_from_db()
    assert mon.flapping_since is None


def test_a_quiet_fifteen_minutes_ends_flapping_with_one_stabilised_notice(mon, run):
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)
    last = 1 + 4 * 2
    run(mon, last + 10, False)  # still down, no change: only 10 quiet minutes, so still muted
    mon.refresh_from_db()
    assert mon.flapping_since is not None
    run(mon, last + 16, False)  # 15+ quiet minutes: settled
    mon.refresh_from_db()
    assert mon.flapping_since is None
    assert sum("стабіліз" in s for s in subjects()) == 1


def test_the_stabilised_notice_says_what_state_the_monitor_settled_in(mon, run):
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)  # ends down
    run(mon, 9 + 16, False)
    note = next(m for m in mail.outbox if "стабіліз" in m.subject)
    assert "недоступн" in note.body.lower()


def test_after_stabilising_the_next_real_change_alerts_again(mon, run):
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)
    run(mon, 30, True)  # settles (the incident from the last flip closes)
    before = len(subjects())
    run(mon, 40, False)
    assert len(subjects()) == before + 1 and "недоступн" in subjects()[-1]


def test_flapping_is_tracked_per_monitor(user, mon, run):
    other = Monitor.objects.create(user=user, name="Other", url="https://other.example.com")
    run(mon, 0, True)
    alternate(run, mon, 5, start=1)
    run(other, 0, True)
    run(other, 1, False)
    other.refresh_from_db()
    assert other.flapping_since is None
    assert "недоступн" in subjects()[-1]  # the other monitor's first outage is an ordinary alert
