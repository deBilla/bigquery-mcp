"""The MCP contract, exercised through a real client session.

``create_connected_server_and_client_session`` wires a client to the server over
in-memory streams, so these assertions cover the path a real client takes --
schema generation, annotations, error shape -- without a subprocess, a network,
or credentials.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from conftest import FakeQueryJob
from data_platform_mcp.server import _instructions, mcp

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "list_environments",
    "list_datasets",
    "list_tables",
    "get_table_schema",
    "check_table_freshness",
    "run_query",
}


async def _tools():
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        return (await client.list_tools()).tools


async def _call(name, arguments):
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        return await client.call_tool(name, arguments)


async def test_every_tool_is_exposed():
    assert {t.name for t in await _tools()} == EXPECTED_TOOLS


async def test_all_tools_declare_themselves_read_only():
    for tool in await _tools():
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.readOnlyHint is True, tool.name
        assert tool.annotations.destructiveHint is False, tool.name


async def test_every_tool_documents_itself():
    for tool in await _tools():
        assert tool.description, f"{tool.name} has no description"
        if tool.name != "list_environments":
            # Without a documented `environment`, an agent cannot route a
            # question to the right warehouse and will silently use the default.
            assert "environment" in tool.inputSchema["properties"], tool.name


async def test_only_the_config_tool_is_closed_world():
    """Everything else reaches out to BigQuery; list_environments does not."""
    tools = await _tools()
    closed = {t.name for t in tools if t.annotations.openWorldHint is False}
    assert closed == {"list_environments"}


async def test_instrumentation_did_not_alter_the_schemas():
    """The audit wrapper sits between FastMCP and every tool; if it were not
    transparent, the arguments would silently stop being generated."""
    tools = {t.name: t for t in await _tools()}

    assert set(tools["run_query"].inputSchema["properties"]) == {
        "sql", "max_rows", "confirm_expensive", "environment"
    }
    assert tools["run_query"].inputSchema["required"] == ["sql"]
    assert tools["get_table_schema"].inputSchema["required"] == [
        "dataset_id", "table_id"
    ]
    # table_id is optional: omitting it checks the whole dataset.
    assert tools["check_table_freshness"].inputSchema["required"] == ["dataset_id"]
    # list_environments reports this server's own config and takes nothing.
    assert tools["list_environments"].inputSchema["properties"] == {}


async def test_a_refusal_arrives_as_a_protocol_error(fake_client):
    """The Phase 3 contract change. A returned {"error": ...} left isError
    unset, so a refusal was indistinguishable from a result."""
    fake_client.dry = FakeQueryJob(statement_type="DELETE")
    result = await _call("run_query", {"sql": "DELETE FROM t"})

    assert result.isError is True
    assert "DELETE" in result.content[0].text


async def test_a_costly_query_arrives_as_a_result_the_agent_must_relay(fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=2 * 1024**3)
    result = await _call("run_query", {"sql": "SELECT *"})

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "confirmation_required"


async def test_results_are_structured_not_prose(fake_client):
    fake_client.datasets = ("sales", "marketing")
    result = await _call("list_datasets", {})

    payload = json.loads(result.content[0].text)
    assert payload["datasets"] == ["marketing", "sales"]
    assert payload["count"] == 2


async def test_the_instructions_carry_the_behaviour_the_evals_check():
    """Tool and server descriptions are the highest-leverage thing to change
    here, so the load-bearing sentences are pinned rather than left to drift."""
    text = _instructions()
    assert "NEVER SELF-CONFIRM" in text
    assert "confirm_expensive" in text
    assert "does NOT mean a table is partitioned" in text.replace("\n", " ")
    assert "free" in text  # discovery costs nothing; spend those calls first
    assert "stale" in text
