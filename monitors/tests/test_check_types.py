"""O1: check types (strategy/registry). Pure logic: no database, no network."""

import pytest
from django.core.exceptions import ValidationError

from monitors import check_types
from monitors.check_types import HttpResponse, evaluate_http, get, validate_config

KINDS = ["http_status", "keyword", "json_path", "ssl_expiry"]


def ok(body="", status=200, truncated=False):
    return HttpResponse(status_code=status, body=body, truncated=truncated)


# ---------------- registry ----------------
def test_the_registry_has_exactly_the_four_types_with_a_common_interface():
    assert sorted(check_types.REGISTRY) == sorted(KINDS)
    for kind in KINDS:
        strategy = get(kind)
        assert strategy.key == kind and callable(strategy.clean_config)
    assert [get(k).needs_http for k in KINDS] == [True, True, True, False]
    assert [get(k).needs_body for k in KINDS] == [False, True, True, False]


def test_an_unknown_type_is_rejected_by_name():
    with pytest.raises(ValidationError) as exc:
        validate_config("telnet", {})
    assert "check_type" in exc.value.message_dict


# ---------------- config validation ----------------
VALID = [
    ("http_status", {}),
    ("http_status", {"expected_statuses": [200, 301]}),
    ("http_status", {"expected_statuses": [200, "300-399"]}),
    ("keyword", {"keyword": "Welcome"}),
    ("keyword", {"keyword": "Maintenance", "must_contain": False, "expected_statuses": [200]}),
    ("json_path", {"path": "status.db", "equals": "ok"}),
    ("json_path", {"path": "items.0.name", "equals": None}),
    ("json_path", {"path": "ready", "equals": True}),
    ("ssl_expiry", {"min_days_left": 14}),
    ("ssl_expiry", {}),
]
INVALID = [
    ("http_status", [200], "config"),  # not an object
    ("http_status", {"expected_statuses": "200"}, "expected_statuses"),
    ("http_status", {"expected_statuses": [99]}, "expected_statuses"),
    ("http_status", {"expected_statuses": [600]}, "expected_statuses"),
    ("http_status", {"expected_statuses": ["abc"]}, "expected_statuses"),
    ("http_status", {"expected_statuses": ["400-300"]}, "expected_statuses"),
    ("http_status", {"expected_statuses": [True]}, "expected_statuses"),
    ("http_status", {"expected_statuses": list(range(200, 230))}, "expected_statuses"),
    ("http_status", {"keyword": "x"}, "keyword"),  # a key of another type
    ("keyword", {}, "keyword"),
    ("keyword", {"keyword": ""}, "keyword"),
    ("keyword", {"keyword": "x" * 201}, "keyword"),
    ("keyword", {"keyword": 5}, "keyword"),
    ("keyword", {"keyword": "x", "must_contain": "yes"}, "must_contain"),
    ("json_path", {"path": "a"}, "equals"),  # `equals` is required (null must be explicit)
    ("json_path", {"equals": "ok"}, "path"),
    ("json_path", {"path": "", "equals": 1}, "path"),
    ("json_path", {"path": ".a", "equals": 1}, "path"),
    ("json_path", {"path": "a..b", "equals": 1}, "path"),
    ("json_path", {"path": "a b", "equals": 1}, "path"),
    ("json_path", {"path": "a", "equals": {"x": 1}}, "equals"),  # only scalars
    ("json_path", {"path": "a", "equals": [1]}, "equals"),
    ("ssl_expiry", {"min_days_left": 0}, "min_days_left"),
    ("ssl_expiry", {"min_days_left": 366}, "min_days_left"),
    ("ssl_expiry", {"min_days_left": "14"}, "min_days_left"),
    ("ssl_expiry", {"min_days_left": True}, "min_days_left"),
    ("ssl_expiry", {"expected_statuses": [200]}, "expected_statuses"),  # no HTTP status for an SSL check
]


@pytest.mark.parametrize("kind,config", VALID)
def test_valid_configs_are_accepted(kind, config):
    assert isinstance(validate_config(kind, config), dict)


@pytest.mark.parametrize("kind,config,field", INVALID)
def test_invalid_configs_are_rejected_with_the_offending_field(kind, config, field):
    with pytest.raises(ValidationError) as exc:
        validate_config(kind, config)
    assert field in exc.value.message_dict, exc.value.message_dict


def test_unknown_keys_are_rejected_not_silently_stored():
    with pytest.raises(ValidationError) as exc:
        validate_config("keyword", {"keyword": "x", "typo_field": 1})
    assert "typo_field" in exc.value.message_dict


def test_cleaning_fills_defaults_so_the_stored_config_is_explicit():
    assert validate_config("keyword", {"keyword": "x"}) == {"keyword": "x", "must_contain": True}
    assert validate_config("ssl_expiry", {}) == {"min_days_left": 14}
    assert validate_config("http_status", {}) == {}


def test_a_config_is_never_mutated_by_validation():
    original = {"keyword": "x"}
    validate_config("keyword", original)
    assert original == {"keyword": "x"}


# ---------------- http_status ----------------
@pytest.mark.parametrize("status,up", [(200, True), (301, True), (399, True), (400, False), (503, False)])
def test_http_status_defaults_to_2xx_3xx(status, up):
    out = evaluate_http("http_status", {}, ok(status=status))
    assert out.is_up is up and (out.message is None) == up
    assert out.details["status_code"] == status


def test_http_status_honours_the_allowed_list_and_ranges():
    cfg = {"expected_statuses": [200, 301, "400-403"]}
    assert evaluate_http("http_status", cfg, ok(status=301)).is_up
    assert evaluate_http("http_status", cfg, ok(status=402)).is_up
    down = evaluate_http("http_status", cfg, ok(status=404))
    assert not down.is_up and down.message == "HTTP 404, expected one of [200, 301, '400-403']"


# ---------------- keyword ----------------
def test_keyword_up_and_down_with_a_readable_reason():
    cfg = {"keyword": "Welcome", "must_contain": True}
    assert evaluate_http("keyword", cfg, ok("Hello, Welcome!")).is_up
    down = evaluate_http("keyword", cfg, ok("An empty page"))
    assert (down.is_up, down.message) == (False, "keyword 'Welcome' not found")
    assert down.details["type"] == "keyword" and down.details["message"] == down.message


def test_keyword_must_not_contain_inverts_the_rule():
    cfg = {"keyword": "Maintenance", "must_contain": False}
    assert evaluate_http("keyword", cfg, ok("all good")).is_up
    down = evaluate_http("keyword", cfg, ok("Under Maintenance"))
    assert (down.is_up, down.message) == (False, "keyword 'Maintenance' found")


def test_keyword_is_case_sensitive():
    assert not evaluate_http("keyword", {"keyword": "welcome", "must_contain": True}, ok("Welcome")).is_up


def test_a_bad_status_wins_over_the_body_because_the_body_of_an_error_page_proves_nothing():
    out = evaluate_http("keyword", {"keyword": "Welcome", "must_contain": True}, ok("Welcome", status=503))
    assert not out.is_up and out.message.startswith("HTTP 503")


def test_keyword_can_allow_other_statuses_too():
    cfg = {"keyword": "denied", "must_contain": True, "expected_statuses": [401]}
    assert evaluate_http("keyword", cfg, ok("access denied", status=401)).is_up


def test_a_truncated_body_is_called_out_when_the_keyword_is_missing():
    out = evaluate_http("keyword", {"keyword": "needle", "must_contain": True}, ok("x" * 10, truncated=True))
    assert not out.is_up and "truncated" in out.message and "needle" in out.message


# ---------------- json_path ----------------
def jp(path, equals, body, **kw):
    return evaluate_http("json_path", {"path": path, "equals": equals}, ok(body, **kw))


def test_json_path_up_and_down_with_the_actual_value_in_the_reason():
    assert jp("status.db", "ok", '{"status": {"db": "ok"}}').is_up
    down = jp("status.db", "ok", '{"status": {"db": "degraded"}}')
    assert (down.is_up, down.message) == (False, "status.db = 'degraded', expected 'ok'")
    assert down.details["actual"] == "degraded" and down.details["expected"] == "ok"


def test_json_path_walks_arrays_by_index():
    body = '{"items": [{"name": "a"}, {"name": "b"}]}'
    assert jp("items.1.name", "b", body).is_up
    assert "not found" in jp("items.5.name", "b", body).message


@pytest.mark.parametrize(
    "equals,body,up",
    [
        ("ok", '{"v": "ok"}', True),
        (1, '{"v": 1}', True),
        (1, '{"v": 1.0}', True),
        (True, '{"v": true}', True),
        (None, '{"v": null}', True),
        (1, '{"v": true}', False),  # Python says True == 1; JSON does not
        (True, '{"v": 1}', False),
        (1, '{"v": "1"}', False),  # a string is not a number
        ("1", '{"v": 1}', False),
        (0, '{"v": false}', False),
        (None, '{"v": 0}', False),
    ],
)
def test_json_equality_is_type_strict(equals, body, up):
    assert jp("v", equals, body).is_up is up


def test_json_path_missing_key_invalid_json_and_non_container_have_distinct_reasons():
    assert jp("a.b", "x", '{"a": {}}').message == "path 'a.b' not found in the JSON response"
    assert "not valid JSON" in jp("a", "x", "<html>oops</html>").message
    assert "not found" in jp("a", "x", "[1, 2]").message  # a list has no key 'a'
    assert "not found" in jp("a.b", "x", '{"a": "text"}').message  # cannot descend into a string


def test_json_path_on_a_truncated_body_says_so_instead_of_blaming_the_site():
    out = jp("a", "x", '{"a": "xxxx', truncated=True)
    assert not out.is_up and "truncated" in out.message


def test_json_path_checks_the_status_first():
    out = evaluate_http("json_path", {"path": "a", "equals": 1}, ok('{"a": 1}', status=500))
    assert not out.is_up and out.message.startswith("HTTP 500")


# ---------------- ssl_expiry ----------------
@pytest.mark.parametrize(
    "days,min_days,up", [(30, 14, True), (14, 14, True), (13, 14, False), (0, 14, False), (-3, 1, False)]
)
def test_ssl_expiry_compares_days_left_with_the_minimum(days, min_days, up):
    out = get("ssl_expiry").evaluate_ssl({"min_days_left": min_days}, days)
    assert out.is_up is up and out.details["days_left"] == days
    if not up:
        assert out.message == f"certificate expires in {days} days, need at least {min_days}"
