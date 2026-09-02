#!/usr/bin/env python3
"""Measure what a client actually receives from every tool.

Calls each tool through a real MCP client session against live BigQuery and
records latency, serialised payload size and an estimated token cost. It goes
through the protocol rather than calling functions directly, so what is measured
is what a client actually gets.

Run this after changing a tool's response shape. The numbers are the only way to
see the failures that look fine in code review: a truncation limit that fires on
almost every record, a field that is always empty, an identifier repeated three
times per row.

Usage:
    python evals/measure.py                       # default environment
    python evals/measure.py --environment central

Output lands in evals/measurements/ (git-ignored). **Those files contain live
table names and query results** -- redact before using any of it as a fixture.

Needs live GCP credentials. Every call here is free or nearly so: the discovery
tools scan no bytes, and the queries are deliberately tiny.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

MEASUREMENTS = Path(__file__).resolve().parent / "measurements"

# Roughly four characters per token for JSON. Good enough to compare tools and
# to notice a response that will not fit in a context window; not a billing
# figure, and not presented as one.
CHARS_PER_TOKEN = 4


def calls(dataset: str, table: str, project: str) -> list[tuple[str, dict]]:
    fq = f"{project}.{dataset}.{table}"
    return [
        ("list_environments", {}),
        ("list_datasets", {}),
        ("list_tables", {"dataset_id": dataset}),
        ("get_table_schema", {"dataset_id": dataset, "table_id": table}),
        ("check_table_freshness", {"dataset_id": dataset}),
        ("run_query", {"sql": f"SELECT * FROM `{fq}` LIMIT 50"}),
        ("run_query", {"sql": f"SELECT COUNT(*) AS n FROM `{fq}`"}),
    ]


async def measure(environment: str, dataset: str, table: str, project: str) -> list[dict]:
    from data_platform_mcp.server import mcp

    rows = []
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        for name, arguments in calls(dataset, table, project):
            if environment and name != "list_environments":
                arguments = {**arguments, "environment": environment}
            started = time.perf_counter()
            result = await client.call_tool(name, arguments)
            elapsed = (time.perf_counter() - started) * 1000

            text = result.content[0].text if result.content else ""
            row = {
                "tool": name,
                "arguments": {k: v for k, v in arguments.items() if k != "sql"},
                "ms": round(elapsed, 1),
                "chars": len(text),
                "est_tokens": len(text) // CHARS_PER_TOKEN,
                "is_error": result.isError,
            }
            if result.isError:
                row["error"] = text[:300]
            else:
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = {}
                if isinstance(payload, dict):
                    # The fields that say whether the caller got the whole
                    # answer or a bounded slice of it.
                    for field in ("count", "row_count", "column_count", "truncated",
                                  "stopped_for_size", "columns_truncated", "status",
                                  "estimated_bytes"):
                        if field in payload:
                            row[field] = payload[field]
            rows.append(row)
            print(
                f"  {name:<24} {row['ms']:>8.1f}ms {row['chars']:>8,}ch "
                f"~{row['est_tokens']:>6,}tok"
                + ("  ERROR" if result.isError else "")
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="")
    parser.add_argument("--dataset", default="reporting")
    parser.add_argument("--table", default="daily_active_users")
    parser.add_argument("--project", default="example-warehouse")
    args = parser.parse_args()

    print(f"measuring {args.project}.{args.dataset}.{args.table}")
    rows = asyncio.run(
        measure(args.environment, args.dataset, args.table, args.project)
    )

    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = MEASUREMENTS / f"{stamp}.json"
    path.write_text(json.dumps(rows, indent=2))

    total = sum(r["est_tokens"] for r in rows)
    widest = max(rows, key=lambda r: r["chars"])
    print()
    print(f"total ~{total:,} tokens across {len(rows)} calls")
    print(f"largest: {widest['tool']} at ~{widest['est_tokens']:,} tokens")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
