import csv
from datetime import UTC, date, datetime

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse

from monitors.models import CheckResult, Incident, Monitor
from reports.models import ReportExport
from reports.tasks import generate_report

UTC = UTC


def at(day, hour=12):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


@pytest.fixture
def data(user, other_user):
    a = Monitor.objects.create(user=user, name="A", url="https://a.com")
    b = Monitor.objects.create(user=user, name="B", url="https://b.com")
    foreign = Monitor.objects.create(user=other_user, name="Z", url="https://z.com")
    for i, (up, ms) in enumerate([(True, 100), (True, 200), (True, 300), (False, None)]):
        CheckResult.objects.create(monitor=a, checked_at=at(10, 8 + i), is_up=up, response_time_ms=ms)
    # outside the period -> ignored
    CheckResult.objects.create(monitor=a, checked_at=at(2), is_up=False)
    CheckResult.objects.create(monitor=a, checked_at=at(21, 0), is_up=False)  # day after period_end
    CheckResult.objects.create(monitor=foreign, checked_at=at(10), is_up=False)
    Incident.objects.create(monitor=a, started_at=at(10, 11), resolved_at=at(10, 12), first_error="x")  # 60 min
    # started before the period, 30 min of it inside (Jan 9 23:30 -> Jan 10 00:30)
    Incident.objects.create(
        monitor=a,
        started_at=datetime(2026, 1, 9, 23, 30, tzinfo=UTC),
        resolved_at=datetime(2026, 1, 10, 0, 30, tzinfo=UTC),
        first_error="y",
    )
    Incident.objects.create(monitor=foreign, started_at=at(10), resolved_at=at(11), first_error="z")
    return a, b


def read_rows(export):
    export.refresh_from_db()
    with export.file.open("r") as f:
        return list(csv.DictReader(f))


def test_generate_report_numbers(user, data):
    export = ReportExport.objects.create(user=user, period_start=date(2026, 1, 10), period_end=date(2026, 1, 20))
    assert generate_report(export.pk) == "done"
    rows = {r["monitor_name"]: r for r in read_rows(export)}
    assert set(rows) == {"A", "B"}  # the other user's monitor is absent
    a = rows["A"]
    assert a["url"] == "https://a.com"
    assert (a["checks_total"], a["checks_failed"]) == ("4", "1")
    assert a["uptime_percent"] == "75.0"
    assert a["avg_response_ms"] == "200"
    assert a["incidents_count"] == "2"
    assert float(a["total_downtime_minutes"]) == 90.0  # 60 + clipped 30
    b = rows["B"]  # no data: zeros / blanks, no crash
    assert (b["checks_total"], b["uptime_percent"], b["incidents_count"], b["total_downtime_minutes"]) == (
        "0",
        "",
        "0",
        "0",
    )
    assert export.status == "done" and export.finished_at


def test_generate_report_is_idempotent(user, data):
    export = ReportExport.objects.create(user=user, period_start=date(2026, 1, 10), period_end=date(2026, 1, 20))
    assert generate_report(export.pk) == "done"
    assert generate_report(export.pk) == "skipped"


def test_generate_report_failure_is_recorded(user, data, monkeypatch):
    monkeypatch.setattr("reports.tasks.build_rows", lambda *a: (_ for _ in ()).throw(RuntimeError("kaboom")))
    export = ReportExport.objects.create(user=user, period_start=date(2026, 1, 10), period_end=date(2026, 1, 20))
    assert generate_report(export.pk) == "failed"
    export.refresh_from_db()
    assert export.status == "failed" and "kaboom" in export.error


def test_create_report_via_form_and_validation(auth_client, user):
    url = reverse("report_list")
    bad = auth_client.post(url, {"period_start": "2026-01-01", "period_end": "2026-06-01"})
    assert bad.status_code == 200 and not ReportExport.objects.exists()  # > 90 days
    bad = auth_client.post(url, {"period_start": "2026-02-01", "period_end": "2026-01-01"})
    assert not ReportExport.objects.exists()
    ok = auth_client.post(url, {"period_start": "2026-01-01", "period_end": "2026-01-31"})
    assert ok.status_code == 302
    # eager celery ran it
    assert ReportExport.objects.get().status == "done"


def test_pending_list_auto_refreshes(auth_client, user):
    ReportExport.objects.create(user=user, period_start=date(2026, 1, 1), period_end=date(2026, 1, 2))
    assert 'http-equiv="refresh"' in auth_client.get(reverse("report_list")).content.decode()
    ReportExport.objects.update(status="done")
    assert 'http-equiv="refresh"' not in auth_client.get(reverse("report_list")).content.decode()


def test_download_only_for_owner(client, user, other_user):
    export = ReportExport.objects.create(
        user=user, period_start=date(2026, 1, 1), period_end=date(2026, 1, 2), status="done"
    )
    export.file.save("r.csv", ContentFile(b"a,b\n1,2\n"))
    url = reverse("report_download", args=[export.pk])

    assert client.get(url).status_code == 302  # anonymous -> login
    client.force_login(other_user)
    assert client.get(url).status_code == 404  # someone else's report
    client.force_login(user)
    r = client.get(url)
    assert r.status_code == 200 and b"".join(r.streaming_content) == b"a,b\n1,2\n"
    assert "attachment" in r["Content-Disposition"]


def test_report_queue_limit(auth_client, user, monkeypatch):
    monkeypatch.setattr("reports.views.generate_report.delay", lambda pk: None)  # stay pending
    url = reverse("report_list")
    for _ in range(3):
        assert auth_client.post(url, {"period_start": "2026-01-01", "period_end": "2026-01-10"}).status_code == 302
    r = auth_client.post(url, {"period_start": "2026-01-01", "period_end": "2026-01-10"})
    assert r.status_code == 200 and ReportExport.objects.count() == 3
    assert "Забагато" in r.content.decode()
