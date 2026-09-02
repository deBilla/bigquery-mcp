"""What each tool actually returns, against fakes rather than BigQuery.

These assertions are about shape and refusal behaviour, so they run free, in
milliseconds, with no credentials. The values in the fakes are taken from real
tables on the platform this server was built for -- notably a pair of sibling
tables with identical schemas and opposite partitioning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeField, FakeQueryJob, FakeTable, FakeTimePartitioning, clear_caches
from data_platform_mcp.errors import DataPlatformMCPError
from data_platform_mcp.tools import discovery_tools, query_tools

GIB = 1024**3


# --- get_table_schema -------------------------------------------------------


def ga_schema():
    """The GA4 event shape: a repeated key/value struct nested two deep."""
    return [
        FakeField("event_date"),
        FakeField("partition_date", "DATE"),
        FakeField(
            "event_params", "RECORD", "REPEATED",
            fields=[
                FakeField("key"),
                FakeField("value", "RECORD", fields=[
                    FakeField("string_value"),
                    FakeField("int_value", "INTEGER"),
                ]),
            ],
        ),
    ]


def test_nested_columns_are_flattened_to_dotted_paths(fake_client):
    fake_client.table = FakeTable(schema=ga_schema())

    result = discovery_tools.get_table_schema("events_raw", "events_click")
    names = [c["name"] for c in result["columns"]]

    assert "event_params.key" in names
    assert "event_params.value.string_value" in names
    # Three top-level columns expand to seven fields; reporting only the top
    # level would say event_params exists without saying how to read it.
    assert result["column_count"] == 7


def test_a_repeated_column_is_flagged_so_unnest_is_not_a_guess(fake_client):
    fake_client.table = FakeTable(schema=ga_schema())
    result = discovery_tools.get_table_schema("d", "t")
    flags = {c["name"]: c.get("repeated", False) for c in result["columns"]}
    assert flags["event_params"] is True
    assert flags["event_params.key"] is False


def test_partitioning_is_reported_from_metadata(fake_client):
    fake_client.table = FakeTable(
        schema=ga_schema(),
        num_bytes=6860 * GIB,
        time_partitioning=FakeTimePartitioning(field="partition_date"),
    )
    result = discovery_tools.get_table_schema("events_raw", "events_click")
    assert result["partitioning"] == {
        "kind": "time",
        "field": "partition_date",
        "granularity": "DAY",
        "require_filter": False,
    }
    assert "unpartitioned_warning" not in result


def test_a_date_column_does_not_make_a_table_partitioned(fake_client):
    """The trap this tool exists for.

    events_notification has a partition_date column and no partitioning,
    while its sibling in the same dataset is partitioned on exactly that
    column. Inferring from the name turns a filtered query into a full scan of
    several terabytes.
    """
    fake_client.table = FakeTable(
        schema=ga_schema(),          # includes a partition_date column
        num_bytes=5208 * GIB,
        time_partitioning=None,      # ...and is not partitioned
    )
    result = discovery_tools.get_table_schema(
        "events_raw", "events_notification"
    )
    assert result["partitioning"] is None
    warning = result["unpartitioned_warning"]
    assert "NOT partitioned" in warning and "5208 GiB" in warning


def test_a_small_unpartitioned_table_is_not_warned_about(fake_client):
    fake_client.table = FakeTable(schema=ga_schema(), num_bytes=1024)
    result = discovery_tools.get_table_schema("d", "t")
    assert result["partitioning"] is None
    assert "unpartitioned_warning" not in result


def test_ingestion_time_partitioning_names_the_pseudocolumn(fake_client):
    fake_client.table = FakeTable(
        schema=ga_schema(), time_partitioning=FakeTimePartitioning(field=None)
    )
    result = discovery_tools.get_table_schema("d", "t")
    assert result["partitioning"]["field"] == "_PARTITIONTIME"


def test_a_view_carries_its_definition(fake_client):
    # A view's own size is 0 and says nothing about what querying it costs.
    fake_client.table = FakeTable(
        schema=ga_schema(), table_type="VIEW", view_query="SELECT 1", num_bytes=0
    )
    result = discovery_tools.get_table_schema("d", "v")
    assert result["view_query"] == "SELECT 1"


def test_a_pathological_schema_is_capped_and_says_so(fake_client, monkeypatch):
    monkeypatch.setattr(discovery_tools, "_MAX_FIELDS", 5)
    fake_client.table = FakeTable(schema=[FakeField(f"c{i}") for i in range(20)])
    result = discovery_tools.get_table_schema("d", "t")
    assert len(result["columns"]) == 5
    assert result["columns_truncated"] is True
    assert result["column_count"] == 20  # the true count is still reported


# --- check_table_freshness --------------------------------------------------


def test_a_dead_table_is_named_not_just_listed(fake_client):
    now = datetime.now(timezone.utc)
    rows = [
        type("R", (), {"table_id": "live", "row_count": 5, "size_bytes": 1,
                       "last_modified": now})(),
        type("R", (), {"table_id": "abandoned", "row_count": 5, "size_bytes": 1,
                       "last_modified": now - timedelta(days=321)})(),
    ]
    fake_client.job = FakeQueryJob(rows=rows)

    result = discovery_tools.check_table_freshness("events_raw")
    assert result["stale_tables"] == ["abandoned"]
    assert "abandoned" in result["note"]
    assert result["tables"][1]["days_since_modified"] == 321


def test_freshness_for_a_whole_dataset_scans_no_data(fake_client):
    fake_client.job = FakeQueryJob(rows=[])
    discovery_tools.check_table_freshness("d")
    # __TABLES__ is metadata: it dry-runs at zero bytes, which is the only
    # reason this tool can be called freely before trusting a source.
    assert "__TABLES__" in fake_client.queries[0]


# --- allowlist --------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: discovery_tools.list_tables("payroll"),
        lambda: discovery_tools.get_table_schema("payroll", "t"),
        lambda: discovery_tools.check_table_freshness("payroll"),
    ],
)
def test_a_disallowed_dataset_raises_rather_than_returning_an_error(
    monkeypatch, fake_client, call
):
    """A returned {"error": ...} leaves isError unset, so a refusal arrives
    looking exactly like a result."""
    monkeypatch.setenv("BQ_DATASET_ALLOWLIST", "sales")
    clear_caches()
    with pytest.raises(DataPlatformMCPError) as exc:
        call()
    assert "payroll" in str(exc.value) and "sales" in str(exc.value)
