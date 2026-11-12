from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from monitors.models import CheckResult
from monitors.services import daily_bar


def make_public(monitor):
    monitor.is_public = True
    monitor.save()
    return reverse("public_status", args=[monitor.public_slug])


def test_public_page_without_login_hides_url_and_errors(client, monitor):
    CheckResult.objects.create(monitor=monitor, checked_at=timezone.now(), is_up=False, error="SECRET-ERROR-TEXT")
    url = make_public(monitor)
    r = client.get(url)
    html = r.content.decode()
    assert r.status_code == 200
    assert monitor.name in html
    assert monitor.url not in html and "example.com" not in html
    assert "SECRET-ERROR-TEXT" not in html


def test_private_monitor_is_404(client, monitor):
    monitor.public_slug = "abc123"
    monitor.save()  # is_public stays False
    assert client.get(reverse("public_status", args=["abc123"])).status_code == 404
    assert client.get(reverse("public_status", args=["nope"])).status_code == 404


def test_going_private_invalidates_cache_immediately(client, monitor):
    url = make_public(monitor)
    assert client.get(url).status_code == 200  # now cached
    assert client.get(url).status_code == 200
    monitor.is_public = False
    monitor.save()
    assert client.get(url).status_code == 404  # not "after 60 seconds"


def test_deleting_monitor_invalidates_cache(client, monitor):
    url = make_public(monitor)
    client.get(url)
    monitor.delete()
    assert client.get(url).status_code == 404


def test_page_is_cached(client, monitor, django_assert_num_queries):
    url = make_public(monitor)
    client.get(url)
    with django_assert_num_queries(0):
        assert client.get(url).status_code == 200


def test_daily_bar_colors_and_single_query(monitor, django_assert_num_queries):
    now = timezone.now()

    def add(days_ago, up, down):
        t = now - timedelta(days=days_ago)
        for _ in range(up):
            CheckResult.objects.create(monitor=monitor, checked_at=t, is_up=True)
        for _ in range(down):
            CheckResult.objects.create(monitor=monitor, checked_at=t, is_up=False)

    add(1, 100, 0)  # 100%  -> green
    add(2, 95, 5)  # 95%   -> yellow
    add(3, 50, 50)  # 50%   -> red
    add(4, 99, 1)  # 99%   -> green (not < 99)
    add(5, 89, 11)  # 89%   -> red
    # day 6 and today (maybe): no data -> gray
    with django_assert_num_queries(1):
        bar = daily_bar(monitor, 7)
    by_age = {(now.date() - b["day"]).days: b["color"] for b in bar}
    assert len(bar) == 7
    assert (by_age[1], by_age[2], by_age[3], by_age[4], by_age[5], by_age[6]) == (
        "green",
        "yellow",
        "red",
        "green",
        "red",
        "gray",
    )
