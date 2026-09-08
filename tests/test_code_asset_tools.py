"""BigQuery Studio code assets.

The cases worth pinning are the ones the live API taught us, because none of
them are guessable from the type signatures: the region is not the dataset
region, a wrong region is empty rather than an error, notebook bodies are
mostly output, and one asset in six hundred carries no type label at all.
"""

from __future__ import annotations

import json

import pytest
from google.api_core import exceptions as api_exceptions

from conftest import FakeRepository, clear_caches
from data_platform_mcp.errors import DataPlatformMCPError
from data_platform_mcp.tools import code_asset_tools
from data_platform_mcp.tools.code_asset_tools import (
    find_code_assets_using_table as find,
    get_code_asset,
    list_code_assets,
)

SQL = "CREATE OR REPLACE TABLE `p.d.daily` AS SELECT 1 FROM `p.d.raw`"

_real_client = code_asset_tools._client


def _notebook(source, outputs_bytes=0):
    """A notebook whose outputs dwarf its source, as real ones do."""
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [source],
                    "execution_count": 3,
                    "outputs": [{"data": {"image/png": "A" * outputs_bytes}}],
                }
            ]
        }
    )


# --- location ---------------------------------------------------------------


def test_code_asset_location_does_not_inherit_the_dataset_location(monkeypatch):
    """The whole reason the setting exists: datasets sit in multi-region US,
    which Dataform rejects outright, while the assets live in us-central1."""
    monkeypatch.setenv("BQ_CODE_ASSET_LOCATION", "us-central1")
    clear_caches()
    from data_platform_mcp.config import require_environment

    env = require_environment()
    assert env.location == "US"
    assert code_asset_tools._location(env) == "us-central1"


def test_location_falls_back_to_dataset_location_when_unset():
    clear_caches()
    from data_platform_mcp.config import require_environment

    assert code_asset_tools._location(require_environment()) == "us"


def test_empty_result_says_it_may_be_the_wrong_region(fake_code_assets):
    """A wrong region returns zero assets, not an error. Without this note the
    answer 'you have no notebooks' is indistinguishable from the truth."""
    fake_code_assets(repos=[])
    result = list_code_assets()
    assert result["total_in_project"] == 0
    assert "regional" in result["note"]
    assert result["location"] == "us"


def test_every_result_states_the_location_it_read(fake_code_assets):
    fake_code_assets(repos=[FakeRepository("a")])
    assert list_code_assets()["location"] == "us"
    assert "us" in fake_code_assets.get().parents[0]


# --- listing ----------------------------------------------------------------


def test_listing_counts_every_type_even_when_filtered(fake_code_assets):
    """The type breakdown describes the project, not the filter -- otherwise
    asking for notebooks hides that saved queries outnumber them 6:1."""
    fake_code_assets(
        repos=[
            FakeRepository("q1", "sql"),
            FakeRepository("q2", "sql"),
            FakeRepository("n1", "notebook"),
        ]
    )
    result = list_code_assets(asset_type="notebook")
    assert result["by_type"] == {"sql": 2, "notebook": 1}
    assert result["matched"] == 1
    assert [a["name"] for a in result["assets"]] == ["n1"]


def test_listing_never_reads_bodies(fake_code_assets):
    """Bodies are what cost quota; a listing that opened 616 of them would
    exhaust it before the user asked a second question."""
    client = fake_code_assets(
        repos=[FakeRepository("q1", "sql", repo_id="r1")],
        files={"r1": {"content.sql": SQL}},
    )
    list_code_assets()
    assert client.reads == []


def test_truncation_is_reported_not_silent(fake_code_assets):
    fake_code_assets(repos=[FakeRepository(f"q{i}", "sql") for i in range(10)])
    result = list_code_assets(limit=3)
    assert len(result["assets"]) == 3
    assert "3 of 10" in result["truncated"]


def test_unparseable_internal_metadata_does_not_fail_the_call(fake_code_assets):
    """last_modified lives in a field the API documents no schema for and
    names 'internal', so it must never be load-bearing."""
    fake_code_assets(repos=[FakeRepository("q", internal_metadata="not json")])
    assert list_code_assets()["assets"][0]["last_modified"] is None


# --- reading one asset ------------------------------------------------------


def test_notebook_outputs_are_stripped(fake_code_assets):
    """Measured at 77% of bytes across 52 real notebooks."""
    fake_code_assets(
        repos=[FakeRepository("nb", "notebook", repo_id="r1")],
        files={"r1": {"content.ipynb": _notebook("print(1)", outputs_bytes=5000)}},
    )
    result = get_code_asset("nb")
    assert result["outputs_stripped"] is True
    assert result["source_chars"] < result["raw_chars"] / 10
    assert result["cells"][0]["source"] == "print(1)"
    assert "A" * 100 not in json.dumps(result)


def test_saved_query_write_target_is_extracted(fake_code_assets):
    fake_code_assets(
        repos=[FakeRepository("q", "sql", repo_id="r1")],
        files={"r1": {"content.sql": SQL}},
    )
    assert get_code_asset("q")["writes_to_from_sql"] == ["p.d.daily"]


def test_templated_targets_in_notebooks_are_not_reported_as_tables(fake_code_assets):
    """A notebook builds its SQL in Python, so the destination regex sees
    `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}` and would report it as a real
    table. Observed on the first live notebook this was run against."""
    body = _notebook('sql = f"CREATE OR REPLACE TABLE `{PROJECT}.{DS}.{TBL}` AS SELECT 1"')
    fake_code_assets(
        repos=[FakeRepository("nb", "notebook", repo_id="r1")],
        files={"r1": {"content.ipynb": body}},
    )
    assert "writes_to_from_sql" not in get_code_asset("nb")


def test_asset_with_no_type_label_is_read_by_asking_for_its_filename(fake_code_assets):
    """One asset in 616 had no type label. Guessing an extension yields a
    NotFound that explains nothing, so unknown types list the directory."""
    fake_code_assets(
        repos=[FakeRepository("mystery", asset_type=None, repo_id="r1")],
        files={"r1": {"content.weird": "hello"}},
    )
    result = get_code_asset("mystery")
    assert result["type"] == "unknown"
    assert result["content"] == "hello"


def test_ambiguous_display_name_refuses_rather_than_picking_one(fake_code_assets):
    """Display names are not unique in BigQuery Studio. Answering about the
    wrong asset would be invisible in the reply."""
    fake_code_assets(
        repos=[
            FakeRepository("report", "sql", repo_id="r1"),
            FakeRepository("report", "sql", repo_id="r2"),
        ]
    )
    with pytest.raises(DataPlatformMCPError) as exc:
        get_code_asset("report")
    assert "not unique" in str(exc.value)
    assert "r1" in str(exc.value) and "r2" in str(exc.value)


def test_unknown_asset_suggests_near_matches(fake_code_assets):
    fake_code_assets(repos=[FakeRepository("daily_revenue_report", "sql")])
    with pytest.raises(DataPlatformMCPError) as exc:
        get_code_asset("revenue")
    assert "daily_revenue_report" in str(exc.value)


def test_oversized_body_is_flagged_not_silently_cut(fake_code_assets):
    fake_code_assets(
        repos=[FakeRepository("big", "sql", repo_id="r1")],
        files={"r1": {"content.sql": "SELECT 1 -- " + "x" * 60_000}},
    )
    result = get_code_asset("big")
    assert "stopped_for_size" in result
    assert len(result["content"]) == code_asset_tools._MAX_PAYLOAD_CHARS


# --- errors -----------------------------------------------------------------


def test_permission_denied_names_both_causes(fake_code_assets):
    """A missing role and a bad region are the same 403 here."""
    fake_code_assets(raise_on_list=api_exceptions.PermissionDenied("nope"))
    with pytest.raises(DataPlatformMCPError) as exc:
        list_code_assets()
    message = str(exc.value)
    assert "roles/dataform.viewer" in message
    assert "code_asset_location" in message


def test_quota_error_says_it_will_recover(fake_code_assets, no_backoff_sleep):
    """Exhaustion is a refilling bucket, so 'retry shortly' is the actual fix
    and must not read as a permanent failure."""
    fake_code_assets(raise_on_list=api_exceptions.ResourceExhausted("slow down"))
    with pytest.raises(DataPlatformMCPError) as exc:
        list_code_assets()
    assert "refills" in str(exc.value)


def test_quota_failures_are_retried_before_giving_up(fake_code_assets, no_backoff_sleep):
    """Retry is the whole mitigation for a depleted bucket, so a single
    ResourceExhausted must not surface to the caller."""
    calls = {"n": 0}
    repo = FakeRepository("q", "sql", repo_id="r1")

    class Flaky:
        def list_repositories(self, request=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise api_exceptions.ResourceExhausted("slow down")
            return iter([repo])

    from data_platform_mcp.tools import code_asset_tools as mod

    fake_code_assets(repos=[])
    mod._client = lambda env: Flaky()  # noqa: E731
    try:
        assert list_code_assets()["total_in_project"] == 1
        assert calls["n"] == 3
        assert no_backoff_sleep.slept, "expected backoff between attempts"
    finally:
        mod._client = _real_client


def test_backoff_grows_between_attempts(no_backoff_sleep):
    """Constant retries against a refilling bucket just re-collide."""
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise api_exceptions.ResourceExhausted("slow down")
        return "ok"

    assert code_asset_tools._retrying(flaky) == "ok"
    assert no_backoff_sleep.slept == sorted(no_backoff_sleep.slept)


# --- searching for a table --------------------------------------------------


def _corpus(fake_code_assets, bodies):
    repos, files = [], {}
    for i, (name, body) in enumerate(bodies.items()):
        rid = f"r{i}"
        repos.append(FakeRepository(name, "sql", repo_id=rid))
        files[rid] = {"content.sql": body}
    return fake_code_assets(repos=repos, files=files)


def test_search_finds_readers_and_ranks_by_match_count(fake_code_assets):
    _corpus(
        fake_code_assets,
        {
            "one_hit": "SELECT * FROM `p.d.orders`",
            "two_hits": "SELECT * FROM `p.d.orders` JOIN `p.d.orders` USING (id)",
            "unrelated": "SELECT * FROM `p.d.customers`",
        },
    )
    result = find("orders")
    assert result["found_in"] == 2
    assert [a["name"] for a in result["assets"]] == ["two_hits", "one_hit"]


def test_search_matches_regardless_of_qualification(fake_code_assets):
    """'p.d.orders', 'd.orders' and 'orders' should all find each other --
    the caller rarely knows which form the SQL used."""
    _corpus(fake_code_assets, {"a": "SELECT * FROM `proj.dataset.orders`"})
    for query in ("orders", "dataset.orders", "proj.dataset.orders", "`proj.dataset.orders`"):
        assert find(query)["found_in"] == 1, query


def test_search_reports_how_much_it_covered(fake_code_assets):
    """A search is evidence about what it scanned, never proof of absence."""
    _corpus(fake_code_assets, {f"q{i}": "SELECT 1" for i in range(10)})
    result = find("orders", max_assets=4)
    assert result["assets_scanned"] == 4
    assert result["assets_available"] == 10
    assert "4 of 10" in result["truncated"]


def test_unreadable_assets_are_reported_not_counted_as_misses(fake_code_assets):
    """Silence would read as 'this table is unused here', which is the one
    conclusion a failed read must not support."""
    client = _corpus(fake_code_assets, {"a": "SELECT * FROM `p.d.orders`"})
    client.raise_on_read = api_exceptions.ResourceExhausted("slow down")
    result = find("orders")
    assert result["found_in"] == 0
    assert result["unread"]["count"] == 1
    assert "NOT searched" in result["unread"]["note"]


def test_search_requires_a_table_name(fake_code_assets):
    _corpus(fake_code_assets, {"a": "SELECT 1"})
    with pytest.raises(DataPlatformMCPError):
        find("   ")
