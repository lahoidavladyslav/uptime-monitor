import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from monitors.models import Monitor


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


@pytest.fixture(autouse=True)
def _fake_public_dns(monkeypatch):
    """Tests never touch real DNS: every hostname resolves to a public IP."""
    import socket

    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("monitors.security.socket.getaddrinfo", fake)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("alice", "alice@example.com", "pw12345678")


@pytest.fixture
def other_user(db):
    return get_user_model().objects.create_user("bob", "bob@example.com", "pw12345678")


@pytest.fixture
def monitor(user):
    return Monitor.objects.create(user=user, name="Site", url="https://example.com")


@pytest.fixture
def auth_client(client, user):
    client.force_login(user)
    return client
