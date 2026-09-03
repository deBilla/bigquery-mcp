"""Scheduled queries: what writes a table, and why it stopped.

The destination extraction is the part worth pinning. Most scheduled queries on
a real platform declare no destination dataset because they write with DDL, so
without reading the SQL the tool cannot answer the question it exists for.
"""

from __future__ import annotations

import pytest
from google.api_core import exceptions as api_exceptions

from conftest import FakeRun, FakeTransferConfig, clear_caches
from data_platform_mcp.errors import DataPlatformMCPError
from data_platform_mcp.tools import transfer_tools
from data_platform_mcp.tools.transfer_tools import _targets_from_sql

DDL = "CREATE OR REPLACE TABLE `proj.reports.daily` AS SELECT 1"


# --- reading the destination out of SQL -------------------------------------


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("CREATE OR REPLACE TABLE `p.d.t` AS SELECT 1", ["p.d.t"]),
        ("CREATE TABLE IF NOT EXISTS `p.d.t` AS SELECT 1", ["p.d.t"]),
        ("INSERT INTO `p.d.t` (a) VALUES (1)", ["p.d.t"]),
        ("DELETE FROM `p.d.t` WHERE day = CURRENT_DATE()", ["p.d.t"]),
        ("MERGE `p.d.t` USING s ON x", ["p.d.t"]),
        ("MERGE INTO `p.d.t` USING s ON x", ["p.d.t"]),
        ("TRUNCATE TABLE `p.d.t`", ["p.d.t"]),
        ("UPDATE `p.d.t` SET a = 1", ["p.d.t"]),
        ("insert into p.d.t select 1", ["p.d.t"]),          # unquoted, lowercase
    ],
)
def test_write_targets_are_found_for_each_statement_shape(sql, expected):
    assert _targets_from_sql(sql) == expected


def test_comments_are_stripped_before_matching():
    """Real configs open with a comment describing the step, which would
    otherwise hide the statement behind it."""
    sql = "-- Step 1: wipe the window\nDELETE FROM `p.d.t` WHERE day > '2020-01-01'"
    assert _targets_from_sql(sql) == ["p.d.t"]
    assert _targets_from_sql("/* INSERT INTO `p.d.decoy` */ DELETE FROM `p.d.real`") == [
        "p.d.real"
    ]


def test_only_qualified_names_count_as_destinations():
    """A looser match picks up the keyword after the verb -- UPDATE x SET y
    yielded 'SET'. Requiring backticks or a dot removes that class entirely."""
    assert _targets_from_sql("UPDATE `p.d.t` SET col = 1") == ["p.d.t"]
    assert "SET" not in _targets_from_sql("UPDATE `p.d.t` SET col = 1")
    assert _targets_from_sql("INSERT INTO tmp SELECT 1") == []


def test_several_targets_are_all_reported_in_order():
    sql = "DELETE FROM `p.d.a` WHERE 1=1; INSERT INTO `p.d.b` SELECT * FROM `p.d.c`"
    assert _targets_from_sql(sql) == ["p.d.a", "p.d.b"]


def test_a_stored_procedure_call_yields_nothing_rather_than_a_guess():
    assert _targets_from_sql("CALL `p.d.sp_refresh`();") == []


# --- listing ----------------------------------------------------------------


def test_a_declared_destination_is_used_as_is(fake_transfers):
    fake_transfers.configs = [
        FakeTransferConfig("sq_a", destination_dataset_id="reports", table="daily")
    ]
    row = transfer_tools.list_scheduled_queries()["scheduled_queries"][0]
    assert row["destination"] == "reports.daily"
    assert "writes_to_from_sql" not in row


def test_a_ddl_query_reports_where_it_writes(fake_transfers):
    """107 of 118 real configs are shaped this way; without the SQL there is
    no destination to report at all."""
    fake_transfers.configs = [FakeTransferConfig("sq_b", query=DDL)]
    row = transfer_tools.list_scheduled_queries()["scheduled_queries"][0]
    assert row["destination"] == ""
    assert row["writes_to_from_sql"] == ["proj.reports.daily"]


def test_filtering_by_dataset_finds_ddl_writers_too(fake_transfers):
    """Filtering on the declared field alone would hide most of them."""
    fake_transfers.configs = [
        FakeTransferConfig("declared", destination_dataset_id="reports", table="t"),
        FakeTransferConfig("via_ddl", query=DDL),
        FakeTransferConfig("elsewhere", query="INSERT INTO `proj.other.t` SELECT 1"),
    ]
    names = [
        q["name"]
        for q in transfer_tools.list_scheduled_queries(dataset="reports")[
            "scheduled_queries"
        ]
    ]
    assert names == ["declared", "via_ddl"]


def test_disabled_and_failing_queries_are_called_out(fake_transfers):
    """The reason a table went stale, surfaced without having to scan rows."""
    fake_transfers.configs = [
        FakeTransferConfig("healthy", query=DDL),
        FakeTransferConfig("switched_off", query=DDL, disabled=True),
        FakeTransferConfig("broken", query=DDL, state=5),
    ]
    result = transfer_tools.list_scheduled_queries()
    assert result["disabled_queries"] == ["switched_off"]
    assert result["failing_queries"] == ["broken"]
    assert "stale" in result["note"]


def test_disabled_queries_can_be_excluded(fake_transfers):
    fake_transfers.configs = [
        FakeTransferConfig("on", query=DDL),
        FakeTransferConfig("off", query=DDL, disabled=True),
    ]
    result = transfer_tools.list_scheduled_queries(include_disabled=False)
    assert [q["name"] for q in result["scheduled_queries"]] == ["on"]


def test_other_transfer_types_are_ignored(fake_transfers):
    """The API also carries S3 and Ads transfers, which are noise here."""
    fake_transfers.configs = [
        FakeTransferConfig("a_query", query=DDL),
        FakeTransferConfig("an_s3_import", data_source_id="amazon_s3"),
    ]
    assert transfer_tools.list_scheduled_queries()["count"] == 1


def test_the_location_is_lowercased_for_the_api(monkeypatch, fake_transfers):
    """BigQuery reports multi-regions capitalised ("US"); the transfer API
    addresses them lowercase, so they would never match."""
    monkeypatch.setenv("BQ_LOCATION", "US")
    clear_caches()
    transfer_tools.list_scheduled_queries()
    assert fake_transfers.parents[0].endswith("/locations/us")


# --- one query in detail ----------------------------------------------------


def test_a_single_query_returns_its_sql_and_runs(fake_transfers):
    fake_transfers.configs = [FakeTransferConfig("sq_daily", query=DDL)]
    fake_transfers.runs = [FakeRun(state=4), FakeRun(state=5, error="boom")]
    result = transfer_tools.get_scheduled_query("sq_daily")
    assert result["sql"] == DDL
    assert [r["state"] for r in result["runs"]] == ["SUCCEEDED", "FAILED"]
    assert "1 of the last 2 runs failed" in result["note"]


def test_a_query_can_be_found_by_partial_name(fake_transfers):
    fake_transfers.configs = [FakeTransferConfig("sq_daily_revenue", query=DDL)]
    assert transfer_tools.get_scheduled_query("revenue")["name"] == "sq_daily_revenue"


def test_an_ambiguous_name_is_refused_rather_than_guessed(fake_transfers):
    fake_transfers.configs = [
        FakeTransferConfig("sq_daily_a", query=DDL),
        FakeTransferConfig("sq_daily_b", query=DDL),
    ]
    with pytest.raises(DataPlatformMCPError) as exc:
        transfer_tools.get_scheduled_query("daily")
    assert "sq_daily_a" in str(exc.value) and "sq_daily_b" in str(exc.value)


def test_an_exact_name_wins_over_a_substring(fake_transfers):
    fake_transfers.configs = [
        FakeTransferConfig("daily", query=DDL),
        FakeTransferConfig("daily_extended", query=DDL),
    ]
    assert transfer_tools.get_scheduled_query("daily")["name"] == "daily"


def test_an_unknown_name_says_how_to_find_the_right_one(fake_transfers):
    fake_transfers.configs = [FakeTransferConfig("sq_a", query=DDL)]
    with pytest.raises(DataPlatformMCPError) as exc:
        transfer_tools.get_scheduled_query("nope")
    assert "list_scheduled_queries" in str(exc.value)


# --- the permission it needs ------------------------------------------------


def test_a_missing_permission_names_the_right_role(fake_transfers):
    """This is a different API from BigQuery, so the other tools working does
    not imply this one will -- and the BigQuery roles are the wrong advice."""
    fake_transfers.raise_on_list = api_exceptions.Forbidden("denied")
    with pytest.raises(DataPlatformMCPError) as exc:
        transfer_tools.list_scheduled_queries()
    text = str(exc.value)
    assert "roles/bigquerydatatransfer.viewer" in text
    assert "bigquery.dataViewer" not in text


def test_a_wrong_location_is_reported_as_a_location_problem(fake_transfers):
    fake_transfers.raise_on_list = api_exceptions.NotFound("nope")
    with pytest.raises(DataPlatformMCPError) as exc:
        transfer_tools.list_scheduled_queries()
    assert "regional" in str(exc.value)
