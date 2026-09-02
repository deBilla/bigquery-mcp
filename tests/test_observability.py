"""The audit trail, and the promise that it never copies query text.

The redaction assertions here are the reason this file exists. A WHERE clause
routinely carries the user ids, emails or device tokens the query is about; an
audit log that reproduces them is a second, uncontrolled home for that data.
"""

from __future__ import annotations

import json

import pytest

from data_platform_mcp import observability
from data_platform_mcp.observability import fingerprint, instrument


@pytest.fixture
def audit_file(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BQ_MCP_AUDIT_LOG", str(path))
    return path


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


SENSITIVE_SQL = (
    "SELECT device_token FROM `p.d.users` "
    "WHERE email = 'someone@example.com' AND user_id = 8817263"
)


def test_sql_text_never_reaches_the_audit_file(audit_file):
    @instrument
    def run_query(sql: str = "", max_rows: int = 0) -> dict:
        return {"row_count": 1}

    run_query(sql=SENSITIVE_SQL, max_rows=10)

    raw = audit_file.read_text()
    for secret in ("someone@example.com", "8817263", "device_token", "SELECT"):
        assert secret not in raw, f"{secret!r} leaked into the audit log"


def test_sql_is_recorded_as_a_fingerprint_that_still_identifies_it(audit_file):
    @instrument
    def run_query(sql: str = "") -> dict:
        return {}

    run_query(sql=SENSITIVE_SQL)
    run_query(sql=SENSITIVE_SQL)
    run_query(sql="SELECT 1")

    got = [r["arguments"]["sql"] for r in records(audit_file)]
    assert got[0] == got[1], "the same query must fingerprint identically"
    assert got[0] != got[2]
    assert got[0]["chars"] == len(SENSITIVE_SQL)


def test_fingerprint_identity_ignores_whitespace_reformatting():
    """The hash is the identity, so a reformatted query is recognised as the
    same one. ``chars`` stays the literal input length, which is why only the
    hash is compared here."""
    a, b = fingerprint("SELECT  a\n FROM t"), fingerprint("SELECT a FROM t")
    assert a["sha256"] == b["sha256"]
    assert a["chars"] != b["chars"]


def test_structural_arguments_are_kept_but_unknown_ones_are_redacted(audit_file):
    @instrument
    def some_tool(dataset_id: str = "", table_id: str = "", mystery: str = "") -> dict:
        return {}

    some_tool(dataset_id="sales", table_id="orders", mystery="unexpected-value")
    args = records(audit_file)[0]["arguments"]
    assert args["dataset_id"] == "sales"
    assert args["table_id"] == "orders"
    # An argument added later must default to redacted, never to logged.
    assert args["mystery"] == "<redacted>"


def test_costing_fields_are_lifted_out_of_the_result(audit_file):
    @instrument
    def run_query(sql: str = "") -> dict:
        return {"row_count": 3, "truncated": True, "stopped_for_size": True,
                "bytes_processed": 4096, "rows": [{"a": 1}]}

    run_query(sql="SELECT 1")
    r = records(audit_file)[0]
    assert r["row_count"] == 3 and r["truncated"] is True
    assert r["stopped_for_size"] is True and r["bytes_processed"] == 4096
    assert r["error"] is None
    assert "rows" not in r  # the payload itself is never copied


def test_a_gated_expensive_query_leaves_a_trace(audit_file):
    # confirmation_required is not an error, so without this the audit log
    # would show nothing about a costly query having been proposed.
    @instrument
    def run_query(sql: str = "") -> dict:
        return {"status": "confirmation_required", "estimated_bytes": 9_000_000_000}

    run_query(sql="SELECT *")
    r = records(audit_file)[0]
    assert r["status"] == "confirmation_required"
    assert r["estimated_bytes"] == 9_000_000_000


def test_a_failure_is_recorded_and_the_exception_still_propagates(audit_file):
    @instrument
    def run_query(sql: str = "") -> dict:
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        run_query(sql=SENSITIVE_SQL)

    r = records(audit_file)[0]
    assert r["error"] == "ValueError" and "kaboom" in r["error_message"]
    assert "someone@example.com" not in audit_file.read_text()


def test_auditing_is_disabled_by_the_off_switch(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BQ_MCP_AUDIT_LOG", "off")

    @instrument
    def tool() -> dict:
        return {}

    tool()
    assert not path.exists()


def test_an_unwritable_audit_path_does_not_break_the_tool(monkeypatch, tmp_path):
    """Auditing is best-effort; losing it must never lose the answer."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("BQ_MCP_AUDIT_LOG", str(blocker / "audit.jsonl"))

    @instrument
    def tool() -> dict:
        return {"ok": True}

    assert tool() == {"ok": True}


def test_instrumentation_is_invisible_to_the_protocol():
    """FastMCP builds the tool schema by introspection, so the wrapper must
    preserve everything it reads."""
    import inspect

    def query_something(sql: str = "", max_rows: int = 0) -> dict:
        """A docstring the client will show."""
        return {}

    wrapped = instrument(query_something)
    assert wrapped.__name__ == "query_something"
    assert wrapped.__doc__ == "A docstring the client will show."
    assert inspect.signature(wrapped) == inspect.signature(query_something)
    assert wrapped.__annotations__ == query_something.__annotations__


def test_logging_goes_to_stderr_never_stdout(capsys):
    """stdout carries the JSON-RPC stream; a byte written there drops the
    connection."""
    observability.logger.handlers.clear()
    observability.configure_logging()

    @instrument
    def tool() -> dict:
        return {}

    tool()
    captured = capsys.readouterr()
    assert captured.out == ""
    observability.logger.handlers.clear()
