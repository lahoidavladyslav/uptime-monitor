"""O1: the Monitor model owns the check type and refuses invalid configs (clean() AND save())."""

import importlib

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from monitors.models import CheckResult, Monitor


def make(user, **kw):
    return Monitor(user=user, name="M", url="https://m.example.com", **kw)


def test_new_monitors_default_to_a_plain_status_check(user):
    m = Monitor.objects.create(user=user, name="M", url="https://m.example.com")
    assert (m.check_type, m.check_config) == ("http_status", {})


@pytest.mark.parametrize(
    "kind,config",
    [
        ("keyword", {"keyword": "Welcome"}),
        ("json_path", {"path": "status.db", "equals": "ok"}),
        ("ssl_expiry", {"min_days_left": 7}),
    ],
)
def test_clean_accepts_valid_configs_and_normalises_them(user, kind, config):
    m = make(user, check_type=kind, check_config=config)
    m.full_clean(exclude=["user", "public_slug"])
    assert m.check_config == {**config, **({"must_contain": True} if kind == "keyword" else {})}


@pytest.mark.parametrize(
    "kind,config,field",
    [
        ("keyword", {}, "check_config"),
        ("keyword", {"keyword": "x", "typo": 1}, "check_config"),
        ("json_path", {"path": "a"}, "check_config"),
        ("ssl_expiry", {"min_days_left": 0}, "check_config"),
        ("telnet", {}, "check_type"),
    ],
)
def test_clean_rejects_invalid_configs_on_the_right_field(user, kind, config, field):
    with pytest.raises(ValidationError) as exc:
        make(user, check_type=kind, check_config=config).full_clean(exclude=["user", "public_slug"])
    assert field in exc.value.message_dict


def test_the_error_names_the_setting_that_is_wrong(user):
    with pytest.raises(ValidationError) as exc:
        make(user, check_type="keyword", check_config={}).full_clean(exclude=["user", "public_slug"])
    assert "keyword" in " ".join(exc.value.message_dict["check_config"])


def test_an_ssl_expiry_monitor_needs_an_https_url(user):
    with pytest.raises(ValidationError) as exc:
        Monitor(user=user, name="M", url="http://m.example.com", check_type="ssl_expiry").full_clean(
            exclude=["user", "public_slug"]
        )
    assert "url" in exc.value.message_dict


def test_save_refuses_an_invalid_config_so_it_never_reaches_the_database(user):
    with pytest.raises(ValidationError):
        Monitor.objects.create(user=user, name="M", url="https://m.example.com", check_type="keyword", check_config={})
    assert not Monitor.objects.exists()


def test_save_also_guards_updates_and_leaves_the_stored_row_untouched(user):
    m = Monitor.objects.create(
        user=user, name="M", url="https://m.example.com", check_type="keyword", check_config={"keyword": "a"}
    )
    m.check_config = {"keyword": ""}
    with pytest.raises(ValidationError):
        m.save()
    m.refresh_from_db()
    assert m.check_config["keyword"] == "a"


def test_a_partial_update_that_does_not_touch_the_check_does_not_revalidate_it(user):
    m = Monitor.objects.create(user=user, name="M", url="https://m.example.com")
    Monitor.objects.filter(pk=m.pk).update(
        check_type="keyword", check_config={}
    )  # a legacy/bad row written around the guard
    m.refresh_from_db()
    m.last_checked_at = timezone.now()
    m.save(update_fields=["last_checked_at"])  # the scheduler's hot path must not fail on it


def test_check_result_details_default_to_an_empty_object_and_keep_structure(user):
    m = Monitor.objects.create(user=user, name="M", url="https://m.example.com")
    a = CheckResult.objects.create(monitor=m, checked_at=timezone.now(), is_up=True)
    b = CheckResult.objects.create(
        monitor=m,
        checked_at=timezone.now(),
        is_up=False,
        details={"type": "keyword", "message": "keyword 'Welcome' not found"},
    )
    a.refresh_from_db(), b.refresh_from_db()
    assert a.details == {} and b.details["message"] == "keyword 'Welcome' not found"


# ---------------- the data migration for existing monitors ----------------
@pytest.mark.parametrize(
    "status,text,expected_type,expected_config",
    [
        ("", "", "http_status", {}),
        ("200,204", "", "http_status", {"expected_statuses": [200, 204]}),
        ("200, 300-399", "", "http_status", {"expected_statuses": [200, "300-399"]}),
        ("", "Welcome", "keyword", {"keyword": "Welcome", "must_contain": True}),
        ("200", "Welcome", "keyword", {"keyword": "Welcome", "must_contain": True, "expected_statuses": [200]}),
    ],
)
def test_legacy_expected_status_and_text_become_a_check_type_and_config(
    user, status, text, expected_type, expected_config
):
    migration = importlib.import_module("monitors.migrations.0012_check_types")
    assert migration.convert(status, text) == (expected_type, expected_config)
