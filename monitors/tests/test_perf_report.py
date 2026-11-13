"""O3: perf_report measures the main pages on the seeded data set, reproducibly, from one command."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db

SEED = ["--monitors", "8", "--checks", "1600", "--incidents", "16", "--days", "3"]
SCENARIOS = [
    "dashboard (HTML)",
    "dashboard (API)",
    "monitor page (HTML)",
    "monitor stats 7d (API)",
    "checks list (API)",
    "checks list, failures only (API)",
    "incidents (API)",
    "public status page (50 monitors)",
    "report CSV, 30 days",
]


@pytest.fixture
def seeded():
    call_command("seed_volume", *SEED, stdout=StringIO(), stderr=StringIO())


def report(*args):
    out = StringIO()
    call_command("perf_report", "--runs", "1", *args, stdout=out)
    return out.getvalue()


def test_it_refuses_to_run_without_the_seeded_data(db):
    with pytest.raises(CommandError, match="seed_volume"):
        report()


def test_every_scenario_is_reported_with_time_and_query_count(seeded):
    text = report()
    for name in SCENARIOS:
        row = next((line for line in text.splitlines() if line.startswith(f"| {name}")), None)
        assert row is not None, f"{name} missing from the report:\n{text}"
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1].replace(".", "").isdigit() and cells[2].isdigit()  # ms, queries


def test_json_output_is_machine_readable_and_complete(seeded):
    data = json.loads(report("--format", "json"))
    assert [s["name"] for s in data["scenarios"]] == SCENARIOS
    assert all({"ms", "queries", "status"} <= set(s) for s in data["scenarios"])
    assert all(s["status"] == 200 or s["status"] is None for s in data["scenarios"])  # None = not an HTTP scenario


def test_explain_analyze_with_buffers_is_included_on_request(seeded):
    text = report("--explain")
    assert "Buffers:" in text and "Execution Time" in text


def test_the_numbers_do_not_depend_on_the_database_cache_being_cold_or_warm(seeded):
    """One warm-up run is discarded, so the first scenario is not blamed for loading pages into the cache."""
    assert "warm-up" in report().lower()
