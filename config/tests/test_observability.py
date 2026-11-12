import json
import logging

import pytest
from django.test import RequestFactory

from config import observability as obs


def _format(record_kwargs=None, **extra):
    handler = logging.StreamHandler()
    handler.setFormatter(obs.JsonFormatter())
    handler.addFilter(obs.ContextFilter())
    record = logging.getLogger("t").makeRecord(
        "t", logging.INFO, __file__, 1, "hello %s", ("world",), None, extra=extra
    )
    handler.filter(record)
    return json.loads(handler.format(record))


def test_json_formatter_emits_one_object_with_extras():
    out = _format(user_id=7, event="login_failed")
    assert out["message"] == "hello world"
    assert out["level"] == "INFO" and out["logger"] == "t"
    assert out["user_id"] == 7 and out["event"] == "login_failed"
    assert "timestamp" in out


def test_json_formatter_escapes_newlines_so_one_event_is_one_line():
    out_line = obs.JsonFormatter().format(
        logging.LogRecord("t", logging.INFO, __file__, 1, "a\nb\r\nFAKE LINE", (), None)
    )
    assert "\n" not in out_line and json.loads(out_line)["message"] == "a\nb\r\nFAKE LINE"


def test_json_formatter_includes_exception_and_survives_unserialisable_extras():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    record.weird = object()
    out = json.loads(obs.JsonFormatter().format(record))
    assert "ValueError: boom" in out["exception"]
    assert isinstance(out["weird"], str)


def test_request_id_is_in_every_record_while_bound():
    token = obs.request_id_var.set("req-123")
    try:
        assert _format()["request_id"] == "req-123"
    finally:
        obs.request_id_var.reset(token)
    assert "request_id" not in _format()


@pytest.mark.parametrize(
    "header,keeps",
    [("abc-123_X", True), ("a" * 64, True), ("a" * 65, False), ("bad id\nx", False), ("", False)],
)
def test_incoming_request_id_is_only_trusted_when_sane(header, keeps):
    assert (obs.sanitize_request_id(header) == header) is keeps


@pytest.mark.django_db
def test_middleware_sets_response_header_and_logs_route_not_raw_path(client, caplog):
    caplog.set_level(logging.INFO, logger="request")
    resp = client.get("/confirm/SECRET-TOKEN-VALUE/", headers={"x-request-id": "rid-1"})
    assert resp["X-Request-ID"] == "rid-1"
    rec = next(r for r in caplog.records if r.name == "request")
    assert rec.route == "confirm/<str:token>/"
    assert rec.status == resp.status_code == 400
    assert "SECRET-TOKEN-VALUE" not in json.dumps(rec.__dict__, default=str)


@pytest.mark.django_db
def test_middleware_generates_an_id_and_skips_healthz(client, caplog):
    caplog.set_level(logging.INFO, logger="request")
    r = client.get("/healthz/")
    assert len(r["X-Request-ID"]) >= 16
    assert not [x for x in caplog.records if x.name == "request"]


@pytest.mark.django_db
def test_access_log_carries_user_id_but_never_email(auth_client, caplog):
    caplog.set_level(logging.INFO, logger="request")
    auth_client.get("/")
    rec = next(r for r in caplog.records if r.name == "request")
    assert rec.user_id is not None
    assert "alice@example.com" not in json.dumps(rec.__dict__, default=str)


def test_client_ip_ignores_forwarding_headers_unless_proxy_is_trusted(settings):
    rf = RequestFactory()
    req = rf.get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_REAL_IP="203.0.113.9")
    settings.TRUST_PROXY_HEADERS = False
    assert obs.client_ip(req) == "10.0.0.5"
    settings.TRUST_PROXY_HEADERS = True
    assert obs.client_ip(req) == "203.0.113.9"


def test_client_ip_rejects_garbage_from_the_header(settings):
    settings.TRUST_PROXY_HEADERS = True
    req = RequestFactory().get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_REAL_IP="not-an-ip")
    assert obs.client_ip(req) == "10.0.0.5"


def test_celery_task_id_is_bound_while_a_task_runs():
    from celery import shared_task

    seen = {}

    @shared_task(name="tests.log_probe")
    def probe():
        seen["during"] = obs.task_id_var.get()

    result = probe.apply()
    assert seen["during"] == result.id
    assert obs.task_id_var.get() is None  # reset afterwards
