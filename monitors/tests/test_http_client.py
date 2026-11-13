import socket
import threading

import pytest

from monitors.http_client import guarded_session
from monitors.tasks import perform_http_check


@pytest.fixture
def local_server():
    """A real HTTP server on 127.0.0.1 (a 'private' address)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    hits = []

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            hits.append(1)
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], hits
    srv.close()


def test_connection_to_private_peer_is_refused_even_if_dns_said_public(local_server, monkeypatch):
    """DNS rebinding: the pre-flight check passed, the real socket points at 127.0.0.1."""
    port, hits = local_server
    monkeypatch.setattr("monitors.tasks.blocked_reason", lambda url: None)  # pre-check fooled
    out = perform_http_check(f"http://127.0.0.1:{port}/")
    assert (out.status_code, out.is_up) == (None, False)
    assert "Заблоковано" in out.error


def test_guard_can_be_disabled_for_local_dev(local_server, settings):
    port, _ = local_server
    settings.ALLOW_PRIVATE_TARGETS = True
    with guarded_session() as s:
        assert s.get(f"http://127.0.0.1:{port}/", timeout=3).status_code == 200


def test_guard_blocks_without_the_setting(local_server, settings):
    port, hits = local_server
    settings.ALLOW_PRIVATE_TARGETS = False
    with guarded_session() as s, pytest.raises(Exception, match="non-public address"):
        s.get(f"http://127.0.0.1:{port}/", timeout=3)
