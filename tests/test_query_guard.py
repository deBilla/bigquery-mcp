"""run_query's cost gate and its refusals.

Every path here is a decision about spending someone else's money or handing
back a partial answer, so each one is pinned: what runs, what is refused, what
is handed back to the user, and what the caller is told about limits.
"""

from __future__ import annotations

import pytest
from google.api_core import exceptions as api_exceptions

from conftest import FakeQueryJob, clear_caches
from data_platform_mcp.errors import DataPlatformMCPError
from data_platform_mcp.tools import query_tools

GIB = 1024**3


def rows(n, width=10):
    return [{"id": i, "pad": "x" * width} for i in range(n)]


# --- the read-only guarantee ------------------------------------------------


@pytest.mark.parametrize(
    "statement", ["INSERT", "UPDATE", "DELETE", "MERGE", "SCRIPT",
                  "CREATE_TABLE", "DROP_TABLE"]
)
def test_only_select_is_allowed(fake_client, statement):
    """The statement type comes from the dry run, so this holds however the SQL
    is written -- no pattern-matching on the text to evade."""
    fake_client.dry = FakeQueryJob(statement_type=statement)
    with pytest.raises(DataPlatformMCPError) as exc:
        query_tools.run_query("anything at all")
    assert statement in str(exc.value)
    assert fake_client.queries == ["anything at all"]  # dry run only; never executed


def test_a_select_runs(fake_client):
    fake_client.dry = FakeQueryJob(statement_type="SELECT", total_bytes_processed=100)
    fake_client.job = FakeQueryJob(rows=rows(3), total_bytes_processed=100)
    result = query_tools.run_query("SELECT 1")
    assert result["row_count"] == 3
    assert result["truncated"] is False


# --- the cost gate ----------------------------------------------------------


def test_a_cheap_query_runs_without_asking(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=1024)
    fake_client.job = FakeQueryJob(rows=rows(1), total_bytes_processed=1024)
    assert "status" not in query_tools.run_query("SELECT 1")


def test_a_costly_query_is_handed_back_to_the_user_not_run(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=2 * GIB)
    result = query_tools.run_query("SELECT *")

    assert result["status"] == "confirmation_required"
    assert "rows" not in result
    assert len(fake_client.queries) == 1  # dry run only
    # The user is asked to approve a number they can act on.
    assert result["estimated_scan"] == "2.00 GiB"
    assert "$" in result["message"]


def test_confirmation_required_is_a_result_not_an_error(fake_client):
    """It must not raise: the agent is supposed to relay it and come back."""
    fake_client.dry = FakeQueryJob(total_bytes_processed=2 * GIB)
    assert isinstance(query_tools.run_query("SELECT *"), dict)


def test_the_users_confirmation_lets_it_through(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=2 * GIB)
    fake_client.job = FakeQueryJob(rows=rows(2), total_bytes_processed=2 * GIB)
    result = query_tools.run_query("SELECT *", confirm_expensive=True)
    assert result["row_count"] == 2
    assert len(fake_client.queries) == 2  # dry run, then the real one


def test_the_hard_cap_refuses_even_with_confirmation(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=6 * GIB)
    with pytest.raises(DataPlatformMCPError) as exc:
        query_tools.run_query("SELECT *", confirm_expensive=True)
    assert len(fake_client.queries) == 1  # never executed


def test_the_cap_message_says_it_is_this_tools_limit_not_bigquerys(fake_client):
    """The confusion this wording exists to prevent: people conclude BigQuery
    refused the query and stop, when the Python client would run it fine."""
    fake_client.dry = FakeQueryJob(total_bytes_processed=5 * 1024**4)
    with pytest.raises(DataPlatformMCPError) as exc:
        query_tools.run_query("SELECT *")
    text = str(exc.value)
    assert "5.00 TiB" in text and "$31.25" in text
    assert "not a BigQuery limit" in text
    assert "google-cloud-bigquery" in text


def test_thresholds_come_from_configuration(monkeypatch, fake_client):
    monkeypatch.setenv("BQ_WARN_BYTES", "100")
    clear_caches()
    fake_client.dry = FakeQueryJob(total_bytes_processed=200)
    assert query_tools.run_query("SELECT 1")["status"] == "confirmation_required"


# --- response bounding ------------------------------------------------------


def test_a_wide_result_is_cut_to_a_size_budget_and_says_so(fake_client, monkeypatch):
    monkeypatch.setattr(query_tools, "_MAX_PAYLOAD_CHARS", 500)
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=rows(500, width=200), total_bytes_processed=10)

    result = query_tools.run_query("SELECT *", max_rows=500)

    assert result["stopped_for_size"] is True
    assert result["truncated"] is True
    assert 0 < result["row_count"] < 500
    assert result["requested_max_rows"] == 500
    assert "response size budget" in result["note"]


def test_one_row_wider_than_the_budget_is_still_returned(fake_client, monkeypatch):
    """Returning nothing would look like an empty result set, which is a
    different and much more misleading answer than a big one."""
    monkeypatch.setattr(query_tools, "_MAX_PAYLOAD_CHARS", 50)
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=rows(3, width=5000), total_bytes_processed=10)
    assert query_tools.run_query("SELECT *")["row_count"] == 1


def test_a_result_within_budget_is_not_marked_partial(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=rows(5), total_bytes_processed=10)
    result = query_tools.run_query("SELECT *", max_rows=100)
    assert result["truncated"] is False
    assert "stopped_for_size" not in result


def test_hitting_the_row_cap_is_reported_as_truncated(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=rows(10), total_bytes_processed=10)
    assert query_tools.run_query("SELECT *", max_rows=10)["truncated"] is True


def test_max_rows_defaults_to_the_configured_limit(monkeypatch, fake_client):
    monkeypatch.setenv("BQ_ROW_LIMIT", "2")
    clear_caches()
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=rows(50), total_bytes_processed=10)
    assert query_tools.run_query("SELECT *")["row_count"] == 2


# --- validation failures ----------------------------------------------------


def test_bad_sql_raises_and_locates_the_fault(fake_client):
    fake_client.dry = api_exceptions.BadRequest("Syntax error: unexpected SELEKT")
    with pytest.raises(DataPlatformMCPError) as exc:
        query_tools.run_query("SELEKT 1")
    assert "failed validation and did not run" in str(exc.value)
    assert "SELEKT" in str(exc.value)


def test_a_permission_failure_keeps_its_actionable_message(fake_client):
    """explain_exception writes a better message for this than run_query could,
    so it must pass through rather than be wrapped in "failed validation"."""
    fake_client.dry = api_exceptions.Forbidden("denied")
    with pytest.raises(DataPlatformMCPError) as exc:
        query_tools.run_query("SELECT 1")
    assert "bigquery.dataViewer" in str(exc.value)
    assert "failed validation" not in str(exc.value)
