import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from monitors.forms import MonitorForm
from monitors.models import CheckResult, Incident, Monitor


def test_login_required(client, db):
    r = client.get(reverse("dashboard"))
    assert r.status_code == 302 and "/login/" in r["Location"]


@pytest.mark.parametrize("name", ["monitor_detail", "monitor_edit", "monitor_delete"])
def test_other_users_monitor_is_404(client, other_user, monitor, name):
    client.force_login(other_user)
    assert client.get(reverse(name, args=[monitor.pk])).status_code == 404


def test_other_user_cannot_trigger_check_or_delete(client, other_user, monitor):
    client.force_login(other_user)
    assert client.post(reverse("monitor_check", args=[monitor.pk])).status_code == 404
    assert client.post(reverse("monitor_delete", args=[monitor.pk])).status_code == 404
    assert Monitor.objects.filter(pk=monitor.pk).exists()


@pytest.mark.parametrize("url", ["ftp://example.com", "javascript:alert(1)", "not a url"])
def test_url_validation_rejects(user, url):
    form = MonitorForm({"name": "x", "url": url, "interval_seconds": 60, "is_active": True}, user=user)
    assert not form.is_valid() and "url" in form.errors


def test_url_validation_accepts_http_and_https(user):
    for url in ("http://example.com", "https://example.com/path?q=1"):
        form = MonitorForm({"name": "x", "url": url, "interval_seconds": 60}, user=user)
        assert form.is_valid(), form.errors


def test_interval_must_be_a_choice(user):
    form = MonitorForm({"name": "x", "url": "https://a.com", "interval_seconds": 7}, user=user)
    assert not form.is_valid() and "interval_seconds" in form.errors


def test_unique_user_url_form_and_db(user, other_user, monitor):
    form = MonitorForm({"name": "dup", "url": monitor.url, "interval_seconds": 60}, user=user)
    assert not form.is_valid() and "url" in form.errors
    # editing the same monitor with its own URL is fine
    form = MonitorForm({"name": "renamed", "url": monitor.url, "interval_seconds": 60}, user=user, instance=monitor)
    assert form.is_valid()
    # another user may monitor the same URL
    Monitor.objects.create(user=other_user, name="x", url=monitor.url)
    # DB-level constraint backs up the form
    with pytest.raises(IntegrityError), transaction.atomic():
        Monitor.objects.create(user=user, name="again", url=monitor.url)


def test_dashboard_has_constant_query_count(auth_client, user, django_assert_max_num_queries):
    now = timezone.now()

    def make(n):
        for i in range(n):
            m = Monitor.objects.create(user=user, name=f"m{i}-{n}", url=f"https://s{i}-{n}.com")
            CheckResult.objects.create(monitor=m, checked_at=now, is_up=False, error="x")
            Incident.objects.create(monitor=m, started_at=now)

    make(2)
    with django_assert_max_num_queries(6):
        assert auth_client.get(reverse("dashboard")).status_code == 200
    make(10)
    with django_assert_max_num_queries(6):  # same ceiling with 5x the monitors
        r = auth_client.get(reverse("dashboard"))
    assert "Down вже" in r.content.decode()


def test_public_slug_generated_once(user):
    m = Monitor.objects.create(user=user, name="x", url="https://a.com")
    assert m.public_slug is None
    m.is_public = True
    m.save()
    slug = m.public_slug
    assert slug
    m.is_public = False
    m.save()
    m.is_public = True
    m.save()
    assert m.public_slug == slug
