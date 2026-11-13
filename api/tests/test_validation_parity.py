import pytest

from api.errors import field_errors_from_django
from api.validation import validate_monitor
from monitors.forms import MonitorForm
from monitors.models import Monitor

VALID = {"name": "Site", "url": "https://new.example.org", "interval_seconds": 300, "http_method": "GET"}


@pytest.mark.parametrize(
    "override,field",
    [
        ({"url": "ftp://x.com"}, "url"),
        ({"url": "http://127.0.0.1/"}, "url"),
        ({"url": "https://example.com"}, "url"),  # duplicate of the fixture monitor
        ({"check_type": "keyword", "check_config": {}}, "check_config"),
        ({"request_headers": "Host: x"}, "request_headers"),
        ({"http_method": "HEAD", "check_type": "keyword", "check_config": {"keyword": "x"}}, "check_type"),
        ({"interval_seconds": 7}, "interval_seconds"),
    ],
)
def test_bad_input_gives_exactly_the_forms_errors(user, monitor, override, field):
    data = {**VALID, **override}
    cleaned, errors = validate_monitor(user, data)
    assert cleaned == {} and field in errors and errors[field]
    assert errors == field_errors_from_django(MonitorForm(data, user=user).errors)  # one source of truth


def test_valid_input_returns_cleaned_data_with_parsed_headers(user):
    cleaned, errors = validate_monitor(user, {**VALID, "request_headers": "X-A: 1"})
    assert errors == {} and cleaned["request_headers"] == {"X-A": "1"}
    assert cleaned["url"] == "https://new.example.org"


def test_editing_a_monitor_with_its_own_url_is_valid_and_does_not_mutate_it(user, monitor):
    cleaned, errors = validate_monitor(user, {**VALID, "url": monitor.url, "name": "Renamed"}, instance=monitor)
    assert errors == {} and cleaned["name"] == "Renamed"
    assert monitor.name == "Site"  # validation alone must not change the object
    assert Monitor.objects.get(pk=monitor.pk).name == "Site"
