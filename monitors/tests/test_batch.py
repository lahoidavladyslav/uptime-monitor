"""H5: batch checks, bulk recording and backpressure."""

import threading
from datetime import timedelta

import httpx
import pytest
from django.core import mail
from django.db import connection
from django.utils import timezone

from monitors import async_checks
from monitors.models import (
    CheckResult,
    Incident,
    MaintenanceWindow,
    Monitor,
    MonitorRegion,
    OutboxEvent,
    WebhookEndpoint,
)
from monitors.tasks import check_batch, check_monitor, claim_batch, due_monitors, record_batch, schedule_checks


@pytest.fixture
def fake_net(monkeypatch):
    """Every URL ending in /down answers 503, everything else 200 (no network)."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(503 if request.url.path.endswith("/down") else 200)

    monkeypatch.setattr(
        async_checks, "make_client", lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return seen


def make_monitors(user, n, prefix="m", **kw):
    Monitor.objects.bulk_create(
        [Monitor(user=user, name=f"{prefix}{i}", url=f"https://{prefix}{i}.example.com/ok", **kw) for i in range(n)]
    )
    return list(Monitor.objects.filter(user=user, name__startswith=prefix).order_by("pk"))


# ---------------- claiming ----------------
def test_a_batch_claim_is_atomic_per_region_and_skips_what_is_not_due(user):
    ms = make_monitors(user, 4)
    ids = [m.pk for m in ms]
    Monitor.objects.filter(pk=ids[3]).update(is_active=False)
    first, _ = claim_batch(ids, "eu")
    assert sorted(first) == sorted(ids[:3])  # inactive one is not claimed
    again, _ = claim_batch(ids, "eu")
    assert again == []  # a duplicate batch of the same region finds nothing due
    other, _ = claim_batch(ids, "us")
    assert sorted(other) == sorted(ids[:3])  # another region has its own clock
    forced, _ = claim_batch(ids[:2], "eu", force=True)
    assert sorted(forced) == sorted(ids[:2])
    assert Monitor.objects.get(pk=ids[0]).last_checked_at is not None


@pytest.mark.django_db(transaction=True)
def test_two_batches_racing_for_the_same_monitors_never_share_one(user):
    ids = [m.pk for m in make_monitors(user, 30)]
    barrier, got = threading.Barrier(2), []

    def worker():
        try:
            barrier.wait(timeout=5)
            got.append(claim_batch(ids, "eu")[0])
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    claimed = [i for g in got for i in g]
    assert len(claimed) == len(set(claimed)) == 30


# ---------------- check_batch end to end ----------------
def test_a_batch_writes_one_result_per_monitor_with_its_region_and_moves_the_clock(user, fake_net):
    ms = make_monitors(user, 5)
    out = check_batch([m.pk for m in ms], region="eu")
    assert out["checked"] == 5
    rows = CheckResult.objects.all()
    assert rows.count() == 5 and set(rows.values_list("region", flat=True)) == {"eu"} and all(r.is_up for r in rows)
    assert all(m.last_checked_at is not None for m in Monitor.objects.filter(user=user))
    assert len(fake_net) == 5


def test_steady_state_costs_a_constant_number_of_queries_not_a_query_per_monitor(
    user, fake_net, django_assert_max_num_queries
):
    ids = [m.pk for m in make_monitors(user, 60)]
    with django_assert_max_num_queries(14):
        check_batch(ids, region="eu")
    assert CheckResult.objects.count() == 60


def test_a_down_monitor_opens_an_incident_and_notifies_like_the_single_path(
    user, fake_net, django_capture_on_commit_callbacks
):
    good, bad = (
        make_monitors(user, 1, "ok")[0],
        Monitor.objects.create(user=user, name="bad", url="https://bad.example.com/down"),
    )
    WebhookEndpoint.objects.create(monitor=bad, url="https://hooks.example.org/x")
    with django_capture_on_commit_callbacks(execute=True):
        check_batch([good.pk, bad.pk], region="eu")
    inc = Incident.objects.get()
    assert inc.monitor_id == bad.pk and inc.first_error == "HTTP 503"
    assert any("недоступний" in m.subject for m in mail.outbox)
    assert list(OutboxEvent.objects.values_list("event_type", flat=True)) == ["incident.opened"]


def test_recovery_in_a_batch_closes_the_incident(user, fake_net, django_capture_on_commit_callbacks):
    m = make_monitors(user, 1)[0]
    Incident.objects.create(monitor=m, started_at=timezone.now() - timedelta(minutes=5), first_error="x")
    with django_capture_on_commit_callbacks(execute=True):
        check_batch([m.pk], region="eu")
    assert Incident.objects.get().resolved_at is not None


def test_maintenance_is_flagged_and_opens_nothing_in_the_batch_path(user, fake_net):
    bad = Monitor.objects.create(user=user, name="bad", url="https://bad.example.com/down")
    now = timezone.now()
    MaintenanceWindow.objects.create(user=user, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
    check_batch([bad.pk], region="eu")
    assert CheckResult.objects.get().in_maintenance is True and not Incident.objects.exists()


def test_the_quorum_still_applies_to_batch_results(user, fake_net, settings):
    settings.CHECK_REGIONS = ["eu", "us", "asia"]
    bad = Monitor.objects.create(user=user, name="bad", url="https://bad.example.com/down")
    Monitor.objects.filter(pk=bad.pk).update(created_at=timezone.now() - timedelta(days=1))
    CheckResult.objects.bulk_create(
        [
            CheckResult(monitor=bad, region=r, checked_at=timezone.now() - timedelta(seconds=180), is_up=True)
            for r in ("eu", "us", "asia")
        ]
    )
    check_batch([bad.pk], region="eu")
    assert not Incident.objects.exists()  # one region down is not a majority
    check_batch([bad.pk], region="us")
    assert Incident.objects.count() == 1


def test_a_monitor_deleted_while_the_batch_was_running_is_dropped_quietly(user, fake_net, monkeypatch):
    from monitors import tasks

    a, b = make_monitors(user, 2)
    real = tasks.record_batch

    def delete_then_record(rows, region):
        Monitor.objects.filter(pk=b.pk).delete()  # the owner deleted it while the requests were in flight
        return real(rows, region)

    monkeypatch.setattr(tasks, "record_batch", delete_then_record)
    out = check_batch([a.pk, b.pk], region="eu")
    assert out["checked"] == 1 and list(CheckResult.objects.values_list("monitor_id", flat=True)) == [a.pk]


def test_batch_and_single_paths_agree_on_incidents(user, fake_net, settings):
    """The same observations must give the same incident whether they arrive as a batch or one by one."""
    single = Monitor.objects.create(user=user, name="s", url="https://s.example.com/down")
    batched = Monitor.objects.create(user=user, name="b", url="https://b.example.com/down")
    for _ in range(3):
        check_monitor(single.pk, force=True)
        check_batch([batched.pk], region="eu", force=True)
    assert Incident.objects.filter(monitor=single).count() == Incident.objects.filter(monitor=batched).count() == 1


def test_record_batch_does_nothing_for_an_empty_batch(db):
    assert record_batch([], "eu") == {"checked": 0, "up": 0, "down": 0, "decided": 0}


# ---------------- backpressure ----------------
@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(
        "monitors.tasks.check_batch.apply_async",
        lambda args=None, kwargs=None, queue=None, **kw: out.append((list(args[0]), kwargs["region"], queue)),
    )
    return out


def test_due_monitors_are_enqueued_in_batches_on_the_region_queue(user, sent, settings):
    settings.CHECK_BATCH_SIZE = 100
    make_monitors(user, 250)
    assert schedule_checks() == 250
    assert [len(ids) for ids, _, _ in sent] == [100, 100, 50]
    assert {q for _, _, q in sent} == {"checks.eu"}
    assert len({i for ids, _, _ in sent for i in ids}) == 250


def test_a_monitor_that_is_already_queued_is_not_enqueued_again(user, sent):
    make_monitors(user, 10)
    schedule_checks()
    sent.clear()
    for _ in range(5):  # five more ticks while nothing has run: the queue is not fed
        schedule_checks()
    assert sent == []
    assert due_monitors().count() == 10  # still due, just already waiting in line


def test_the_flag_is_cleared_when_the_batch_runs_and_the_monitor_is_then_not_due(user, sent, fake_net):
    ms = make_monitors(user, 3)
    schedule_checks()
    assert MonitorRegion.objects.filter(queued_at__isnull=False).count() == 3
    for ids, region, _ in sent:
        check_batch(ids, region=region)
    assert MonitorRegion.objects.filter(queued_at__isnull=False).count() == 0
    sent.clear()
    assert schedule_checks() == 0 and sent == []
    assert CheckResult.objects.count() == len(ms)


def test_a_lost_task_is_re_enqueued_after_the_ttl(user, sent, settings):
    settings.QUEUED_TTL_SECONDS = 180
    make_monitors(user, 2)
    schedule_checks()
    MonitorRegion.objects.update(queued_at=timezone.now() - timedelta(seconds=181))  # the worker died with the task
    sent.clear()
    schedule_checks()
    assert sum(len(ids) for ids, _, _ in sent) == 2


def test_after_a_stall_an_overdue_monitor_is_checked_once_not_once_per_missed_interval(user, sent, fake_net):
    """'due' is not 'checked': the queue was stuck for five intervals; when it drains we get ONE check each."""
    ms = make_monitors(user, 4, interval_seconds=60)
    Monitor.objects.filter(user=user).update(last_checked_at=timezone.now() - timedelta(minutes=5))
    for _ in range(5):
        schedule_checks()
    assert sum(len(ids) for ids, _, _ in sent) == 4  # fed once, not five times
    for ids, region, _ in list(sent):
        check_batch(ids, region=region)
    assert CheckResult.objects.count() == 4
    sent.clear()
    assert schedule_checks() == 0  # the next one is a full interval away, not five catch-up checks
    assert all(m.last_checked_at > timezone.now() - timedelta(seconds=30) for m in Monitor.objects.filter(user=user))
    assert len(ms) == 4


def test_regions_are_throttled_independently(user, sent, settings):
    settings.CHECK_REGIONS = ["eu", "us"]
    ms = make_monitors(user, 3)
    schedule_checks()
    sent.clear()
    MonitorRegion.objects.filter(region="us").update(queued_at=None)  # us has drained, eu has not
    schedule_checks()
    assert {r for _, r, _ in sent} == {"us"} and sum(len(i) for i, _, _ in sent) == 3
    assert len(ms) == 3


def test_inactive_monitors_are_never_enqueued_but_monitors_in_maintenance_are(user, sent):
    on, off, maint = (
        make_monitors(user, 1, "on")[0],
        Monitor.objects.create(user=user, name="off", url="https://off.example.com", is_active=False),
        Monitor.objects.create(user=user, name="mt", url="https://mt.example.com"),
    )
    now = timezone.now()
    MaintenanceWindow.objects.create(
        user=user, monitor=maint, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1)
    )
    schedule_checks()
    enqueued = {i for ids, _, _ in sent for i in ids}
    assert enqueued == {on.pk, maint.pk} and off.pk not in enqueued


def test_the_most_overdue_monitors_go_first(user, sent, settings):
    settings.CHECK_BATCH_SIZE = 2
    ms = make_monitors(user, 4)
    now = timezone.now()
    for i, m in enumerate(ms):  # ms[0] is the most overdue
        Monitor.objects.filter(pk=m.pk).update(last_checked_at=now - timedelta(minutes=10 - i))
    schedule_checks()
    assert sent[0][0] == [ms[0].pk, ms[1].pk]


def test_the_scheduler_ticks_every_ten_seconds(settings):
    """A 60 s tick makes a late-by-more-than-the-slack monitor skip a whole minute (found by the load test)."""
    assert settings.CELERY_BEAT_SCHEDULE["schedule-checks"]["schedule"] == 10.0


def test_a_monitor_checked_late_is_picked_up_at_the_next_tick_not_a_minute_later(user, sent, fake_net):
    """Checked 25 s late in the previous cycle: with a 10 s tick it is due again 35-45 s later, not 95 s."""
    import time_machine

    m = make_monitors(user, 1, interval_seconds=60)[0]
    t0 = timezone.now()
    with time_machine.travel(t0, tick=False):
        Monitor.objects.filter(pk=m.pk).update(last_checked_at=t0 - timedelta(seconds=25 + 60 - 50))
    picked = []
    for step in range(0, 70, 10):  # ticks every 10 s
        with time_machine.travel(t0 + timedelta(seconds=step), tick=False):
            sent.clear()
            schedule_checks()
            if sent:
                picked.append(step)
                MonitorRegion.objects.update(queued_at=None)
                break
    assert picked and picked[0] <= 30
