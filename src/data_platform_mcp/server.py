"""data-platform-mcp: a read-only BigQuery MCP server.

Lets an AI client discover schema and run SELECT queries against a BigQuery
project, so people can ask data questions in plain language.

Safety design:
  - Every query is dry-run first to validate it and estimate bytes scanned.
  - Only SELECT / WITH statements are allowed (no writes, no DDL/DML).
  - ``maximum_bytes_billed`` caps the cost of any single query.
  - An optional dataset allowlist restricts what can be read.

Auth uses Application Default Credentials
(``gcloud auth application-default login``).

Nothing here reads configuration at import time. A server that dies during
import cannot answer ``initialize``, so the client reports only that it failed
to start -- with no tools and no message the user can act on. Configuration
problems are raised when a tool runs, where the agent can relay them.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import describe_environments
from .observability import configure_logging, logger
from .tools import (
    discovery_tools,
    environment_tools,
    query_tools,
    transfer_tools,
)


def _instructions() -> str:
    base = (
        "Read-only BigQuery tools for a data platform.\n\n"
        "DISCOVER BEFORE YOU QUERY. list_datasets, list_tables, "
        "get_table_schema and check_table_freshness are all free — they scan "
        "no data. Only run_query costs money, so spend the free calls first. "
        "Always fully-qualify tables as `<project>.<dataset>.<table>`.\n\n"
        "READ get_table_schema BEFORE WRITING SQL, for the partitioning as "
        "much as the columns. A date-shaped column name does NOT mean a table "
        "is partitioned. When `partitioning` is null, no WHERE clause reduces "
        "the scan — the whole table is read every time, which on this platform "
        "can be hundreds of gigabytes. Two tables in the same dataset with the "
        "same columns often differ here. Columns come back as dotted paths; a "
        "column marked `repeated` needs UNNEST.\n\n"
        "WHEN A TABLE IS STALE, FIND WHAT WRITES IT. "
        "list_scheduled_queries shows which scheduled query populates a table "
        "and whether it is disabled or failing — usually the actual answer to "
        "\"why has this not updated\", and cheaper than reasoning about the "
        "data. get_scheduled_query returns one query's SQL and recent runs.\n\n"
        "CHECK FRESHNESS BEFORE TRUSTING A TABLE you have not used before. "
        "Some tables on this platform stopped being written to without being "
        "dropped, so they return stale data rather than an error. If "
        "check_table_freshness reports a table as stale, say so before "
        "presenting its numbers.\n\n"
        "NEVER SELF-CONFIRM A COSTLY QUERY. If run_query returns "
        "`status: \"confirmation_required\"`, stop and report the estimated "
        "scan size and cost to the user, then wait. `confirm_expensive=true` "
        "records the user's decision, not yours — setting it yourself spends "
        "their money on your guess.\n\n"
        "REPORT LIMITS RATHER THAN HIDING THEM. When a result carries "
        "`truncated` or `stopped_for_size`, the rows shown are a partial "
        "answer; say so instead of summarising them as the whole.\n\n"
        "PICK THE ENVIRONMENT FROM THE USER'S WORDS. Every tool takes an "
        "optional `environment`; set it from wording like 'in prod' or 'on "
        "staging', and omit it to use the default. Never guess a name — call "
        "list_environments if unsure. Each result echoes back the environment "
        "it came from, and answering from the wrong one is invisible in the "
        "reply, so when a question does not name an environment, answer from "
        "the default alone rather than surveying them all."
    )
    try:
        configured = describe_environments()
    except Exception:
        # Misconfiguration must surface when a tool runs, not break startup and
        # hide every tool from the client.
        configured = ""
    return f"{base}\n\nConfigured environments:\n{configured}" if configured else base


mcp = FastMCP("data-platform", instructions=_instructions())

for module in (environment_tools, discovery_tools, transfer_tools, query_tools):
    module.register(mcp)


def _run_server() -> None:
    """Serve MCP, over stdio by default.

    stdio is the right choice when a client (Claude Desktop, Claude Code)
    spawns this as a subprocess. Set BQ_MCP_TRANSPORT=http (or sse) to serve
    over the network instead, so a remote or containerized client can connect
    by URL -- auth stays here on the host via ADC, so no Google credentials
    ever enter the container.
    """
    transport = os.environ.get("BQ_MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http", "sse"):
        # Loopback by default: this transport has no authentication of its own,
        # so anything that can reach the port can query as these credentials.
        # Binding beyond localhost has to be a deliberate act.
        mcp.settings.host = os.environ.get("BQ_MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("BQ_MCP_PORT", "8765"))
        logger.info(
            "serving %s on %s:%s", transport, mcp.settings.host, mcp.settings.port
        )
        # FastMCP names the streamable-HTTP transport "streamable-http".
        mcp.run(transport="sse" if transport == "sse" else "streamable-http")
    else:
        mcp.run()


def main() -> None:
    import argparse
    import sys

    # 'setup' forwards every remaining argument to the setup script, so it has
    # to be dispatched before argparse sees flags it knows nothing about.
    if sys.argv[1:2] == ["setup"]:
        from .provisioning import run_setup

        raise SystemExit(run_setup(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="data-platform-mcp",
        description=(
            "Read-only BigQuery MCP server. With no arguments it serves the "
            "Model Context Protocol over stdio, which is how an MCP client "
            "starts it. Run `data-platform-mcp doctor` to check setup."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "doctor", "setup"],
        default="serve",
        help=(
            "'serve' (default) runs the server; 'doctor' checks that this "
            "machine can reach BigQuery and reports how to fix what it cannot; "
            "'setup --project X' creates the read-only service account (pass "
            "--help after it for its own options)."
        ),
    )
    args = parser.parse_args()

    if args.command == "doctor":
        # Imported lazily: doctor is a one-off, and serve should not pay for it.
        from .diagnostics import run_doctor

        raise SystemExit(run_doctor())

    configure_logging()
    logger.info("starting data-platform-mcp %s", __version__)
    _run_server()


if __name__ == "__main__":
    main()
