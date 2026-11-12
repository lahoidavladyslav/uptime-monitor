import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from monitors.forms import MonitorForm, StatusPageForm
from monitors.models import CheckResult, Incident, MaintenanceWindow, Monitor, StatusPage

SECRET_ERR = "SECRET-ERROR-TEXT"


@pytest.fixture
def group(user):
    """Two monitors; one down with a private error text and a private URL."""
    a = Monitor.objects.create(user=user, name="Web", url="https://web.internal-secret.test", public_title="Website")
    b = Monitor.objects.create(user=user, name="API", url="https://api.internal-secret.test")
    now = timezone.now()
    CheckResult.objects.create(monitor=a, checked_at=now, is_up=True, response_time_ms=10)
    CheckResult.objects.create(monitor=b, checked_at=now, is_up=False, error=SECRET_ERR)
    Incident.objects.create(monitor=b, started_at=now - timedelta(minutes=10), first_error=SECRET_ERR)
    page = StatusPage.objects.create(user=user, title="Acme status", slug="acme", description="All Acme systems")
    page.monitors.set([a, b])
    return page, a, b


# ---------------- group page ----------------
def test_group_page_is_public_and_leaks_nothing(client, group):
    page, a, b = group
    r = client.get(reverse("group_status", args=["acme"]))
    html = r.content.decode()
    assert r.status_code == 200
    assert "Acme status" in html and "All Acme systems" in html
    assert "Website" in html and "API" in html  # public_title wins over the internal name
    assert "Web<" not in html.replace("Website", "")
    for leaked in ("internal-secret", SECRET_ERR, "web.", "api."):
        assert leaked not in html, leaked
    assert "Частковий збій" in html  # one of two is down
    assert "триває" in html  # the open incident, without its error text


@pytest.mark.parametrize(
    "states,expected",
    [([True, True], "Усі системи працюють"), ([False, False], "🔴 Збій"), ([True, False], "Частковий збій")],
)
def test_overall_banner(client, user, states, expected):
    page = StatusPage.objects.create(user=user, title="t", slug="t")
    for i, up in enumerate(states):
        m = Monitor.objects.create(user=user, name=f"m{i}", url=f"https://m{i}.com")
        CheckResult.objects.create(monitor=m, checked_at=timezone.now(), is_up=up)
        page.monitors.add(m)
    assert expected in client.get(reverse("group_status", args=["t"])).content.decode()


def test_group_page_has_constant_query_count(client, user, django_assert_max_num_queries):
    page = StatusPage.objects.create(user=user, title="t", slug="t")
    for i in range(12):
        m = Monitor.objects.create(user=user, name=f"m{i}", url=f"https://m{i}.com")
        CheckResult.objects.create(monitor=m, checked_at=timezone.now(), is_up=True)
        page.monitors.add(m)
    with django_assert_max_num_queries(8):  # not 12 * N
        assert client.get(reverse("group_status", args=["t"])).status_code == 200


def test_group_page_404_when_hidden_or_unknown(client, group):
    page, *_ = group
    assert client.get(reverse("group_status", args=["nope"])).status_code == 404
    page.is_public = False
    page.save()
    assert client.get(reverse("group_status", args=["acme"])).status_code == 404


def test_group_cache_and_instant_invalidation(client, group, django_assert_num_queries):
    page, a, b = group
    url = reverse("group_status", args=["acme"])
    client.get(url)
    with django_assert_num_queries(0):
        assert client.get(url).status_code == 200  # served from cache
    # renaming a member shows at once
    a.public_title = "Renamed site"
    a.save()
    assert "Renamed site" in client.get(url).content.decode()
    # removing a member from the group shows at once
    page.monitors.remove(a)
    assert "Renamed site" not in client.get(url).content.decode()
    # hiding the page 404s at once
    client.get(url)
    page.is_public = False
    page.save()
    assert client.get(url).status_code == 404


def test_deleting_a_member_monitor_invalidates_group_page(client, group):
    page, a, b = group
    url = reverse("group_status", args=["acme"])
    assert "API" in client.get(url).content.decode()
    b.delete()
    assert "API" not in client.get(url).content.decode()


def test_maintenance_window_shows_immediately(client, group):
    page, a, b = group
    url = reverse("group_status", args=["acme"])
    client.get(url)
    MaintenanceWindow.objects.create(
        monitor=a, starts_at=timezone.now() - timedelta(minutes=1), ends_at=timezone.now() + timedelta(hours=1)
    )
    assert "Планові роботи" in client.get(url).content.decode()


# ---------------- single page: branding + history ----------------
def test_single_page_branding_and_incident_history(client, user):
    m = Monitor.objects.create(
        user=user, name="internal-name", url="https://x.example", is_public=True,
        public_title="Public Name", public_logo_url="https://cdn.example/logo.png",
    )  # fmt: skip
    now = timezone.now()
    CheckResult.objects.create(monitor=m, checked_at=now, is_up=True)
    Incident.objects.create(
        monitor=m, started_at=now - timedelta(hours=3), resolved_at=now - timedelta(hours=2), first_error=SECRET_ERR
    )
    html = client.get(reverse("public_status", args=[m.public_slug])).content.decode()
    assert "Public Name" in html and "internal-name" not in html
    assert 'src="https://cdn.example/logo.png"' in html
    assert "простій 60 хв" in html
    assert SECRET_ERR not in html and "x.example" not in html


def test_old_incidents_are_not_in_the_30_day_history(client, user):
    m = Monitor.objects.create(user=user, name="m", url="https://x.example", is_public=True)
    Incident.objects.create(
        monitor=m, started_at=timezone.now() - timedelta(days=45), resolved_at=timezone.now() - timedelta(days=44)
    )
    assert "Інцидентів не було" in client.get(reverse("public_status", args=[m.public_slug])).content.decode()


def test_logo_url_must_be_http_or_https(user):
    for bad in ("javascript:alert(1)", "data:text/html,<script>", "ftp://x.com/a.png"):
        f = MonitorForm(
            {"name": "x", "url": "https://a.com", "interval_seconds": 60, "public_logo_url": bad}, user=user
        )
        assert not f.is_valid() and "public_logo_url" in f.errors, bad


# ---------------- feeds ----------------
def test_monitor_feed_has_open_and_resolved_entries_without_secrets(client, user):
    m = Monitor.objects.create(user=user, name="Site", url="https://secret-host.example", is_public=True)
    now = timezone.now()
    done = Incident.objects.create(
        monitor=m, started_at=now - timedelta(hours=3), resolved_at=now - timedelta(hours=2), first_error=SECRET_ERR
    )
    live = Incident.objects.create(monitor=m, started_at=now - timedelta(minutes=5), first_error=SECRET_ERR)
    r = client.get(reverse("monitor_feed", args=[m.public_slug]))
    xml = r.content.decode()
    assert r.status_code == 200 and "atom" in r["Content-Type"]
    assert f"incident-{done.pk}-open" in xml and f"incident-{done.pk}-resolved" in xml  # outage + recovery
    assert f"incident-{live.pk}-open" in xml and f"incident-{live.pk}-resolved" not in xml
    assert SECRET_ERR not in xml and "secret-host" not in xml
    assert "max-age=60" in r["Cache-Control"]


def test_feed_404_for_private_and_group_feed(client, group, user):
    page, a, b = group
    assert client.get(reverse("group_feed", args=["acme"])).status_code == 200
    assert "API" in client.get(reverse("group_feed", args=["acme"])).content.decode()
    page.is_public = False
    page.save()
    assert client.get(reverse("group_feed", args=["acme"])).status_code == 404
    private = Monitor.objects.create(user=user, name="p", url="https://p.example", public_slug="abc")
    assert client.get(reverse("monitor_feed", args=[private.public_slug])).status_code == 404


# ---------------- management UI ----------------
def test_status_page_form_only_offers_own_monitors(user, other_user, monitor):
    foreign = Monitor.objects.create(user=other_user, name="theirs", url="https://theirs.example")
    f = StatusPageForm({"title": "t", "slug": "mine", "monitors": [foreign.pk]}, user=user)
    assert not f.is_valid() and "monitors" in f.errors
    f = StatusPageForm({"title": "t", "slug": "mine", "monitors": [monitor.pk]}, user=user)
    assert f.is_valid(), f.errors


def test_status_page_slug_is_globally_unique(user, other_user, group):
    f = StatusPageForm({"title": "dup", "slug": "acme"}, user=other_user)
    assert not f.is_valid() and "slug" in f.errors


def test_status_page_crud_and_ownership(client, user, other_user, monitor):
    client.force_login(user)
    r = client.post(
        reverse("statuspage_new"), {"title": "Mine", "slug": "mine", "is_public": "on", "monitors": [monitor.pk]}
    )
    assert r.status_code == 302
    page = StatusPage.objects.get(slug="mine")
    assert page.user == user and list(page.monitors.all()) == [monitor]
    assert "Mine" in client.get(reverse("statuspage_list")).content.decode()

    client.force_login(other_user)
    assert client.get(reverse("statuspage_edit", args=[page.pk])).status_code == 404
    assert client.post(reverse("statuspage_delete", args=[page.pk])).status_code == 404
    assert "Mine" not in client.get(reverse("statuspage_list")).content.decode()

    client.force_login(user)
    client.post(reverse("statuspage_delete", args=[page.pk]))
    assert not StatusPage.objects.exists()
    assert client.get(reverse("group_status", args=["mine"])).status_code == 404


def test_status_page_management_requires_login(client, db):
    for name in ("statuspage_list", "statuspage_new"):
        assert client.get(reverse(name)).status_code == 302


def test_status_pages_contain_feed_discovery_link(client, group):
    html = client.get(reverse("group_status", args=["acme"])).content.decode()
    assert re.search(r'<link rel="alternate" type="application/atom\+xml"[^>]*href="/page/acme/feed/"', html)
