import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NGINX = (ROOT / "deploy" / "nginx.conf").read_text()


def rate_limited_pattern() -> re.Pattern:
    """The regex of the nginx location that applies limit_req zone=auth."""
    for match in re.finditer(r"location\s+~\s+(\S+)\s*\{(.*?)\}", NGINX, re.S):
        if "limit_req zone=auth" in match.group(2):
            return re.compile(match.group(1))
    raise AssertionError("no rate-limited location found in deploy/nginx.conf")


@pytest.mark.parametrize(
    "path",
    [
        "/login/", "/signup/", "/password-reset/", "/confirm/abc/",  # server-rendered pages
        "/api/v1/auth/login", "/api/v1/auth/login/2fa", "/api/v1/auth/signup", "/api/v1/auth/resend",
        "/api/v1/auth/confirm", "/api/v1/auth/password-reset", "/api/v1/auth/password-reset/confirm",
    ],
)  # fmt: skip
def test_every_credential_endpoint_is_rate_limited(path):
    """Brute-force protection must cover the JSON API as well as the old pages."""
    assert rate_limited_pattern().search(path), f"{path} is not behind limit_req"


def test_ordinary_api_reads_are_not_rate_limited():
    assert not rate_limited_pattern().search("/api/v1/dashboard")
    assert not rate_limited_pattern().search("/api/v1/auth/me")  # polled by the SPA on every page


def test_env_example_documents_frontend_url():
    """Emails from the API link to the SPA: production must set where it lives."""
    assert "FRONTEND_URL=" in (ROOT / ".env.example").read_text()
