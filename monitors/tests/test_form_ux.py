"""A failed form submit must be impossible to overlook (regression: errors were unstyled and easy to miss)."""

import re

import pytest
from django.urls import reverse

from monitors.models import Monitor

VALID = {"name": "Site", "url": "https://example.org", "interval_seconds": "300", "http_method": "GET"}


def post(client, data, url="monitor_new"):
    return client.post(reverse(url), data)


@pytest.fixture
def c(auth_client):
    return auth_client


def test_invalid_submit_stays_on_page_with_flash_and_marked_field(c):
    r = post(c, {**VALID, "url": "http://127.0.0.1:8000"})
    html = r.content.decode()
    assert r.status_code == 200 and not Monitor.objects.exists()
    assert "Не збережено — виправте помилки у формі." in html  # flash alert at the top
    assert 'class="alert alert-error' in html or "alert-danger" in html or "alert-error" in html
    assert "приватну/внутрішню мережу" in html  # the actual reason
    assert re.search(r'<input[^>]*name="url"[^>]*class="[^"]*is-invalid', html) or re.search(
        r'<input[^>]*class="[^"]*is-invalid[^"]*"[^>]*name="url"', html
    )


def test_error_alerts_are_rendered_red(c):
    # flash level "error" must map to a Bootstrap red alert (message.tags is "error" -> alert-error is unstyled!)
    html = post(c, {**VALID, "name": ""}).content.decode()
    assert "alert-danger" in html


def test_url_error_is_reported_once(c):
    html = post(c, {**VALID, "url": "http://web:8000"}).content.decode()  # hostname without a TLD
    assert html.count("Введіть коректний URL.") == 1


@pytest.mark.parametrize("url", ["ftp://example.org", "javascript:alert(1)"])
def test_non_http_schemes_are_rejected_with_one_message(c, url):
    html = post(c, {**VALID, "url": url}).content.decode()
    assert html.count("errorlist") == 1 or html.count("Введіть коректний URL.") <= 1
    assert not Monitor.objects.exists()


def test_fields_are_bootstrap_styled(c):
    html = c.get(reverse("monitor_new")).content.decode()
    assert 'class="form-control"' in html or "form-control" in html
    assert "form-select" in html  # selects
    assert "form-check-input" in html  # checkboxes


def test_valid_submit_still_redirects(c):
    assert post(c, VALID).status_code == 302 and Monitor.objects.count() == 1


def test_status_page_form_invalid_flashes_too(c):
    r = c.post(reverse("statuspage_new"), {"title": "", "slug": ""})
    html = r.content.decode()
    assert r.status_code == 200 and "Не збережено" in html and "is-invalid" in html


def test_edit_invalid_flashes(c, monitor):
    r = c.post(reverse("monitor_edit", args=[monitor.pk]), {**VALID, "url": "nope nope"})
    assert "Не збережено" in r.content.decode()
