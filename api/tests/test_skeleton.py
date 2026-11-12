import json

import pytest
from django.test import Client
from ninja import NinjaAPI, Router, Schema
from ninja.testing import TestClient

from api.api import register_handlers
from api.errors import ApiError


def test_openapi_is_served(client, db):
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    assert "/api/v1/auth/me" in r.json()["paths"]


def test_me_anonymous_is_401_json(client, db):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert "message" in r.json()


def test_me_returns_user(client, user):
    client.force_login(user)
    assert client.get("/api/v1/auth/me").json() == {
        "id": user.id,
        "username": "alice",
        "email": "alice@example.com",
        "has_2fa": False,
    }


def test_csrf_endpoint_sets_cookie(client, db):
    r = client.get("/api/v1/auth/csrf")
    assert r.status_code == 200
    assert "csrftoken" in r.cookies


class Body(Schema):
    name: str


@pytest.fixture
def throwaway():
    """A private API wired with the SAME handlers, so we can exercise error shapes."""
    api = NinjaAPI(urls_namespace="throwaway")
    register_handlers(api)
    router = Router()

    @router.get("/boom")
    def boom(request):
        raise ApiError(422, "Невірні дані", {"name": ["Обов'язкове"]})

    @router.post("/echo")
    def echo(request, data: Body):
        return {"name": data.name}

    api.add_router("/", router)
    return TestClient(api)


def test_api_error_uses_common_format(throwaway):
    r = throwaway.get("/boom")
    assert r.status_code == 422
    assert r.json() == {"message": "Невірні дані", "errors": {"name": ["Обов'язкове"]}}


def test_ninja_body_validation_uses_common_format(throwaway):
    r = throwaway.post("/echo", json={})
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"message", "errors"}
    assert "name" in body["errors"] and body["message"]


def test_unsafe_method_requires_csrf(user):
    c = Client(enforce_csrf_checks=True)
    c.force_login(user)
    assert c.post("/api/v1/auth/logout").status_code == 403
    token = c.get("/api/v1/auth/csrf").cookies["csrftoken"].value
    r = c.post("/api/v1/auth/logout", HTTP_X_CSRFTOKEN=token)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert c.get("/api/v1/auth/me").status_code == 401  # session really ended


def test_csrf_failure_body_is_json_message(user):
    c = Client(enforce_csrf_checks=True)
    c.force_login(user)
    r = c.post("/api/v1/auth/logout")
    assert r.status_code == 403 and "message" in json.loads(r.content)
