"""H4: checks from several regions and a quorum for incidents."""

import itertools
import threading
from datetime import timedelta
from pathlib import Path

import pytest
import responses
import yaml
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from monitors import regions
from monitors.models import CheckResult, Incident, Monitor, OutboxEvent, WebhookEndpoint
from monitors.tasks import check_monitor, record_check, schedule_checks

ROOT = Path(__file__).resolve().parents[2]
EU, US, ASIA = "eu", "us", "asia"


@pytest.fixture(autouse=True)
def three_regions(settings):
    settings.CHECK_REGIONS = [EU, US, ASIA]


@pytest.fixture
def mon(user):
    m = Monitor.objects.create(user=user, name="Сайт", url="https://example.com")
    Monitor.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(days=1))  # not a brand-new monitor
    # the previous round: every region is alive (reported within the silence threshold) but its result is
    # older than the 2-minute vote window, exactly like a monitor on a 5-minute interval in real life
    CheckResult.objects.bulk_create(
        [
            CheckResult(monitor=m, region=r, checked_at=timezone.now() - timedelta(seconds=180), is_up=True)
            for r in (EU, US, ASIA)
        ]
    )
    return Monitor.objects.get(pk=m.pk)


@pytest.fixture
def bare(user):
    """A monitor with no history at all (for tests that count CheckResult rows)."""
    return Monitor.objects.create(user=user, name="bare", url="https://bare.example.com")


def rec(m, region, up, ago=0, error="boom"):
    """Record one region's result the way check_monitor's write phase does."""
    return record_check(
        m.pk, region, timezone.now() - timedelta(seconds=ago), None if up else 503, 50 if up else None, up,
        None if up else error,
    )  # fmt: skip


def incidents(m):
    return Incident.objects.filter(monitor=m)


# ---------------- quorum ----------------
def test_two_of_three_regions_down_opens_exactly_one_incident(mon):
    rec(mon, EU, False)
    assert not incidents(mon).exists()  # one region is not enough
    rec(mon, US, False)
    assert incidents(mon).count() == 1 and incidents(mon).get().resolved_at is None
    rec(mon, ASIA, False)
    assert incidents(mon).count() == 1  # the third agreeing region adds nothing


def test_one_flaky_region_never_opens_an_incident(mon):
    for _ in range(5):
        rec(mon, EU, False)  # its network is broken
        rec(mon, US, True)
        rec(mon, ASIA, True)
    assert not incidents(mon).exists() and not OutboxEvent.objects.exists()


@pytest.mark.parametrize("order", list(itertools.permutations([(EU, False), (US, False), (ASIA, True)])))
def test_the_outcome_does_not_depend_on_the_order_results_arrive_in(mon, order):
    for region, up in order:
        rec(mon, region, up)
    assert incidents(mon).count() == 1 and incidents(mon).get().resolved_at is None


@pytest.mark.parametrize("order", list(itertools.permutations([(EU, False), (US, True), (ASIA, True)])))
def test_a_lone_down_never_opens_in_any_order(mon, order):
    for region, up in order:
        rec(mon, region, up)
    assert not incidents(mon).exists()


def test_recovery_also_needs_a_quorum(mon):
    rec(mon, EU, False)
    rec(mon, US, False)
    assert incidents(mon).get().resolved_at is None
    rec(mon, EU, True)  # a single region says "up": not enough to close
    assert incidents(mon).get().resolved_at is None
    rec(mon, US, True)  # two of three: closed
    assert incidents(mon).get().resolved_at is not None


def test_a_later_flap_after_recovery_opens_a_new_incident(mon):
    rec(mon, EU, False, ago=30)
    rec(mon, US, False, ago=30)
    rec(mon, EU, True, ago=20)
    rec(mon, US, True, ago=20)
    rec(mon, EU, False)
    rec(mon, US, False)
    assert incidents(mon).count() == 2 and incidents(mon).filter(resolved_at__isnull=True).count() == 1


# ---------------- silent regions ----------------
def silent(m, region):
    """A region that reported once, long ago, and then went quiet (worker/network gone)."""
    CheckResult.objects.filter(monitor=m, region=region).delete()
    CheckResult.objects.create(monitor=m, region=region, checked_at=timezone.now() - timedelta(minutes=20), is_up=True)


def test_a_silent_region_is_neither_up_nor_down(mon):
    silent(mon, ASIA)
    rec(mon, EU, False)
    assert not incidents(mon).exists()  # 1 of 2 live regions: still no majority, and asia's old "up" is ignored
    rec(mon, US, False)
    assert incidents(mon).count() == 1  # both live regions agree


def test_a_silent_region_does_not_vote_up_to_block_a_real_outage(mon):
    silent(mon, ASIA)
    rec(mon, EU, False)
    rec(mon, US, False)
    assert incidents(mon).count() == 1  # asia's stale "up" cannot outvote two fresh "down"


def test_with_only_one_live_region_that_region_decides(mon):
    silent(mon, US)
    silent(mon, ASIA)
    rec(mon, EU, False)
    assert incidents(mon).count() == 1


def test_split_vote_between_two_live_regions_is_undecided(mon):
    silent(mon, ASIA)
    rec(mon, EU, False)
    rec(mon, US, True)
    assert not incidents(mon).exists()


def test_the_first_round_of_a_new_monitor_cannot_be_decided_by_one_region(user):
    fresh = Monitor.objects.create(user=user, name="new", url="https://new.example.com")
    rec(fresh, EU, False)  # us and asia have not had their first check yet
    assert not incidents(fresh).exists()
    rec(fresh, US, False)
    assert incidents(fresh).count() == 1


def test_votes_older_than_two_minutes_are_not_combined(mon):
    rec(mon, EU, False, ago=200)  # 3m20s ago: outside the vote window of what arrives now
    rec(mon, US, False)
    assert not incidents(mon).exists()


def test_regions_report_per_region_status(mon):
    silent(mon, ASIA)
    rec(mon, EU, True)
    rec(mon, US, False)
    rows = {r["region"]: r for r in regions.region_status(mon)}
    assert rows[EU]["status"] == "up" and rows[US]["status"] == "down" and rows[ASIA]["status"] == "stale"
    assert rows[ASIA]["last_checked_at"] is not None


def test_a_region_that_never_reported_is_unknown_not_stale(user):
    fresh = Monitor.objects.create(user=user, name="n", url="https://n.example.com")
    rec(fresh, EU, True)
    rows = {r["region"]: r["status"] for r in regions.region_status(fresh)}
    assert rows == {EU: "up", US: "unknown", ASIA: "unknown"}


# ---------------- concurrency ----------------
@pytest.mark.django_db(transaction=True)
def test_two_regions_finishing_at_the_same_moment_open_one_incident(user, monkeypatch):
    """Forces the nasty interleaving: both workers have written their result and are about to decide.

    Without the monitor row lock each would decide while the other's row is still uncommitted and invisible,
    both would answer "no quorum yet", and the outage would go unreported (a lost update). With the lock the
    second worker can't even write until the first has committed, so it decides on both results.
    """
    from monitors import tasks

    m = Monitor.objects.create(user=user, name="c", url="https://c.example.com")
    Monitor.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(days=1))
    CheckResult.objects.bulk_create(
        [
            CheckResult(monitor=m, region=r, checked_at=timezone.now() - timedelta(seconds=180), is_up=True)
            for r in (EU, US, ASIA)
        ]
    )
    WebhookEndpoint.objects.create(monitor=m, url="https://hooks.example.org/x")
    meet, start, errors = threading.Barrier(2), threading.Barrier(2), []
    real_decide = regions.decide

    def decide_after_meeting(monitor, trigger):
        try:
            meet.wait(timeout=2)  # reached by both only if nothing serialises the two write phases
        except threading.BrokenBarrierError:
            pass
        return real_decide(monitor, trigger)

    monkeypatch.setattr(tasks.regions, "decide", decide_after_meeting)

    def worker(region):
        try:
            start.wait(timeout=5)
            rec(m, region, False)
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(r,)) for r in (EU, US)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert not errors
    assert incidents(m).count() == 1  # the outage is reported, once
    assert OutboxEvent.objects.count() == 1  # one webhook, not one per region


# ---------------- claims, fan-out, results ----------------
@responses.activate
def test_each_region_runs_its_own_check_but_duplicates_within_a_region_are_skipped(bare):
    responses.add(responses.GET, bare.url, status=200)
    assert check_monitor(bare.pk, region=EU) == "up"
    assert check_monitor(bare.pk, region=EU) == "skipped"  # same region, same interval: duplicate
    assert check_monitor(bare.pk, region=US) == "up"  # another region is not blocked by it
    assert sorted(CheckResult.objects.values_list("region", flat=True)) == [EU, US]


@responses.activate
def test_force_bypasses_the_interval_in_every_region(bare):
    responses.add(responses.GET, bare.url, status=200)
    check_monitor(bare.pk, region=EU)
    assert check_monitor(bare.pk, force=True, region=EU) == "up"


def test_the_worker_region_is_the_default_region(bare, settings):
    settings.CHECK_REGION = ASIA
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, bare.url, status=200)
        check_monitor(bare.pk)
    assert CheckResult.objects.get().region == ASIA


def test_scheduling_fans_out_to_every_region_queue(mon, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "monitors.tasks.check_monitor.apply_async",
        lambda args=None, kwargs=None, queue=None, **kw: sent.append((args, kwargs, queue)),
    )
    assert schedule_checks() == 1
    assert sorted(q for _, _, q in sent) == ["checks.asia", "checks.eu", "checks.us"]
    assert {a[0] for a, _, _ in sent} == {mon.pk} and {k["region"] for _, k, _ in sent} == {EU, US, ASIA}


def test_check_now_goes_to_every_region(auth_client, mon, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "monitors.tasks.check_monitor.apply_async",
        lambda args=None, kwargs=None, queue=None, **kw: sent.append((kwargs, queue)),
    )
    monkeypatch.setattr("monitors.views.check_ssl.delay", lambda pk: None)
    auth_client.post(reverse("monitor_check", args=[mon.pk]))
    assert sorted(q for _, q in sent) == ["checks.asia", "checks.eu", "checks.us"] and all(k["force"] for k, _ in sent)


# ---------------- UI / API ----------------
def test_detail_page_and_api_show_every_region(auth_client, mon):
    silent(mon, ASIA)
    rec(mon, EU, True)
    rec(mon, US, False)
    html = auth_client.get(reverse("monitor_detail", args=[mon.pk])).content.decode()
    for label in ("eu", "us", "asia"):
        assert label in html
    assert "немає даних" in html.lower()  # the silent region is called out, not hidden
    body = auth_client.get(f"/api/v1/monitors/{mon.pk}").json()
    assert {r["region"]: r["status"] for r in body["regions"]} == {EU: "up", US: "down", ASIA: "stale"}


# ---------------- deployment ----------------
def test_compose_has_one_worker_per_region_on_its_own_queue():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for region in (EU, US, ASIA):
        svc = services[f"worker-{region}"]
        assert svc["environment"]["CHECK_REGION"] == region
        assert f"-Q checks.{region}" in svc["command"]
