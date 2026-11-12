import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from api.api import api

SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"
STAGE1_PATHS = {
    "/api/v1/auth/csrf",
    "/api/v1/auth/me",
    "/api/v1/auth/signup",
    "/api/v1/auth/confirm",
    "/api/v1/auth/resend",
    "/api/v1/auth/login",
    "/api/v1/auth/login/2fa",
    "/api/v1/auth/logout",
    "/api/v1/auth/password-reset",
    "/api/v1/auth/password-reset/confirm",
    "/api/v1/dashboard",
}


def test_committed_openapi_matches_code():
    call_command("export_openapi", "--check")  # raises CommandError when the snapshot is stale


def test_check_fails_when_snapshot_is_stale(tmp_path):
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n")
    with pytest.raises(CommandError, match="stale"):
        call_command("export_openapi", "--check", path=str(stale))


def test_schema_has_all_stage1_paths():
    assert STAGE1_PATHS <= set(json.loads(SNAPSHOT.read_text())["paths"])
    assert STAGE1_PATHS <= set(api.get_openapi_schema()["paths"])


def test_snapshot_is_deterministic_json():
    text = SNAPSHOT.read_text()
    assert (
        text.endswith("\n")
        and text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
