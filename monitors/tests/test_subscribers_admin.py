"""O2 (part 3): the monitor admin shows how many confirmed subscribers and unsubscribes a monitor has."""

import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from monitors.models import Monitor, StatusSubscriber


@pytest.fixture
def admin_client(db):
    boss = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw12345678")
    c = Client()
    c.force_login(boss)
    return c


def add(mon, n, **kw):
    StatusSubscriber.objects.bulk_create(
        [
            StatusSubscriber(
                monitor=mon,
                email=f"{kw.get('tag', 'x')}{mon.pk}-{i}@example.com",
                **{k: v for k, v in kw.items() if k != "tag"},
            )
            for i in range(n)
        ]
    )


def cells(html, name):
    """The text of every cell in column `name` of the changelist, in row order."""
    return re.findall(rf'<td class="field-{name}">(.*?)</td>', html)


def test_the_changelist_shows_confirmed_and_unsubscribed_counts_per_monitor(admin_client, user):
    now = timezone.now()
    a = Monitor.objects.create(user=user, name="A", url="https://a.example.com")
    b = Monitor.objects.create(user=user, name="B", url="https://b.example.com")
    add(a, 3, confirmed_at=now, tag="ok")  # 3 active
    add(a, 2, confirmed_at=now, unsubscribed_at=now, tag="bye")  # 2 left
    add(a, 4, tag="pending")  # never confirmed: counts as neither
    add(b, 1, confirmed_at=now, tag="ok")
    html = admin_client.get(reverse("admin:monitors_monitor_changelist")).content.decode()
    rows = dict(
        zip(
            re.findall(r'<th class="field-name">.*?>(A|B)</a>', html),
            zip(cells(html, "confirmed_subscribers"), cells(html, "unsubscribed_count"), strict=True),
            strict=True,
        )
    )
    assert rows == {"A": ("3", "2"), "B": ("1", "0")}


def test_the_change_page_shows_the_same_numbers(admin_client, user):
    now = timezone.now()
    m = Monitor.objects.create(user=user, name="A", url="https://a.example.com")
    add(m, 2, confirmed_at=now, tag="ok")
    add(m, 1, confirmed_at=now, unsubscribed_at=now, tag="bye")
    html = admin_client.get(reverse("admin:monitors_monitor_change", args=[m.pk])).content.decode()
    assert re.search(r"Підтверджених підписників.*?<div class=\"readonly\">2</div>", html, re.S)
    assert re.search(r"Відписок.*?<div class=\"readonly\">1</div>", html, re.S)


def test_the_changelist_does_not_query_per_monitor(admin_client, user, django_assert_max_num_queries):
    for i in range(30):
        m = Monitor.objects.create(user=user, name=f"m{i}", url=f"https://m{i}.example.com")
        add(m, 3, confirmed_at=timezone.now(), tag="ok")
    with django_assert_max_num_queries(12):  # session + user + count + one annotated query, however many rows
        assert admin_client.get(reverse("admin:monitors_monitor_changelist")).status_code == 200


def test_subscribers_are_browsable_and_searchable_by_address(admin_client, user):
    m = Monitor.objects.create(user=user, name="A", url="https://a.example.com")
    StatusSubscriber.objects.create(monitor=m, email="findme@example.com", confirmed_at=timezone.now())
    StatusSubscriber.objects.create(monitor=m, email="other@example.com")
    html = admin_client.get(reverse("admin:monitors_statussubscriber_changelist"), {"q": "findme"}).content.decode()
    assert "findme@example.com" in html and "other@example.com" not in html
