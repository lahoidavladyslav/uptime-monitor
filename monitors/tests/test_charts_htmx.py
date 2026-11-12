import json
from datetime import timedelta

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from monitors.models import CheckResult
from monitors.services import response_time_series


def add(monitor, minutes_ago, ms, up=True):
    CheckResult.objects.create(
        monitor=monitor,
        checked_at=timezone.now() - timedelta(minutes=minutes_ago),
        response_time_ms=ms,
        is_up=up,
        status_code=200 if up else None,
    )


def test_series_24h_raw_points_ordered_with_gap_for_failure(monitor):
    add(monitor, 30, 200)
    add(monitor, 20, None, up=False)
    add(monitor, 10, 100)
    add(monitor, 60 * 30, 999)  # older than 24h -> excluded
    pts = response_time_series(monitor, "24h")
    assert [(p["ms"], p["down"]) for p in pts] == [(200, False), (None, True), (100, False)]


def test_series_7d_is_bucketed_hourly_in_two_queries(monitor, django_assert_num_queries):
    base = 60 * 24 * 2  # two days ago, so the points sit in whole, fixed hours
    for i in range(30):  # 30 checks within ~1h => at most 2 hourly buckets
        add(monitor, base + i, 100 + i)
    add(monitor, base + 5, None, up=False)
    call_command("backfill_rollups")  # 7d points come from the hourly rollups (+ the raw tail)
    with django_assert_num_queries(2):  # rollup rows + raw tail, regardless of how many checks exist
        pts = response_time_series(monitor, "7d")
    assert 1 <= len(pts) <= 2
    assert any(p["down"] for p in pts)
    assert all(p["ms"] is not None for p in pts)  # averaged over successful timings only


def test_series_7d_size_is_bounded(monitor):
    # 400 checks spread over 5 days -> far fewer than 400 points
    for i in range(400):
        add(monitor, i * 18, 100)
    assert len(response_time_series(monitor, "7d")) <= 7 * 24


def test_series_empty(monitor):
    assert response_time_series(monitor, "24h") == []
    assert response_time_series(monitor, "7d") == []


def test_detail_page_embeds_chart_data(auth_client, monitor):
    add(monitor, 5, 123)
    html = auth_client.get(reverse("monitor_detail", args=[monitor.pk])).content.decode()
    assert 'id="rt-chart"' in html and "chart.js" in html
    start = html.index('id="rt-data"')
    payload = html[html.index(">", start) + 1 : html.index("</script>", start)]
    assert json.loads(payload)[0]["ms"] == 123


def test_detail_page_without_data_has_no_chart(auth_client, monitor):
    html = auth_client.get(reverse("monitor_detail", args=[monitor.pk])).content.decode()
    assert 'id="rt-chart"' not in html and "немає даних" in html


def test_dashboard_full_vs_htmx_partial(auth_client, monitor):
    full = auth_client.get(reverse("dashboard")).content.decode()
    assert "<html" in full and 'hx-trigger="every 10s' in full and monitor.name in full
    part = auth_client.get(reverse("dashboard"), HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in part and "<table" in part and monitor.name in part


def test_htmx_poll_with_expired_session_redirects_instead_of_swapping_login(client, db):
    r = client.get(reverse("dashboard"), HTTP_HX_REQUEST="true")
    assert r.status_code == 204
    assert r["HX-Redirect"].startswith("/login/")
    # a normal request still gets a regular 302
    assert client.get(reverse("dashboard")).status_code == 302


def test_htmx_partial_is_user_scoped(client, user, other_user, monitor):
    client.force_login(other_user)
    part = client.get(reverse("dashboard"), HTTP_HX_REQUEST="true").content.decode()
    assert monitor.name not in part
