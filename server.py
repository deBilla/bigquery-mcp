"""
BigQuery MCP server.

A read-only Model Context Protocol server over a BigQuery project (set via the
`BQ_PROJECT` environment variable). It lets an AI client (Claude Desktop,
Claude Code, etc.) discover schema and run SELECT queries so people can ask
data questions in plain language.

Safety design:
  - Every query is dry-run first to validate it and estimate bytes scanned.
  - Only SELECT / WITH statements are allowed (no writes, no DDL/DML).
  - `maximum_bytes_billed` caps the cost of any single query.
  - Optional dataset allowlist restricts what can be read.

Auth: uses Application Default Credentials (gcloud auth application-default login).
"""

import json
import os

from google.cloud import bigquery
from mcp.server.fastmcp import FastMCP

# --- Configuration (override via environment variables) ----------------------

PROJECT_ID = os.environ.get("BQ_PROJECT")
if not PROJECT_ID:
    raise RuntimeError(
        "BQ_PROJECT environment variable is required — set it to the GCP project ID "
        "whose BigQuery datasets you want to query, e.g. BQ_PROJECT=my-gcp-project"
    )
LOCATION = os.environ.get("BQ_LOCATION", "US")

# Cap bytes scanned per query. Default 5 GB (~$0.03 at on-demand pricing).
MAX_BYTES_BILLED = int(os.environ.get("BQ_MAX_BYTES_BILLED", 5 * 1024**3))

# Default row cap returned to the model so responses stay small.
DEFAULT_ROW_LIMIT = int(os.environ.get("BQ_ROW_LIMIT", 200))

# Comma-separated dataset allowlist. Empty = all datasets allowed.
_allow = os.environ.get("BQ_DATASET_ALLOWLIST", "").strip()
DATASET_ALLOWLIST = {d.strip() for d in _allow.split(",") if d.strip()}

# Statement types BigQuery considers read-only and safe to run here.
READONLY_STATEMENT_TYPES = {"SELECT"}

# -----------------------------------------------------------------------------

mcp = FastMCP("data-platform")
_client: bigquery.Client | None = None


def client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=PROJECT_ID, location=LOCATION)
    return _client


def _dataset_allowed(dataset_id: str) -> bool:
    return not DATASET_ALLOWLIST or dataset_id in DATASET_ALLOWLIST


@mcp.tool()
def list_datasets() -> str:
    """List the BigQuery datasets available in the data platform project.

    Call this first to discover what data exists. Returns dataset IDs.
    """
    datasets = [
        ds.dataset_id
        for ds in client().list_datasets(project=PROJECT_ID)
        if _dataset_allowed(ds.dataset_id)
    ]
    return json.dumps({"project": PROJECT_ID, "datasets": sorted(datasets)}, indent=2)


@mcp.tool()
def list_tables(dataset_id: str) -> str:
    """List tables (and views) inside a given dataset.

    Args:
        dataset_id: The dataset to inspect, e.g. "sales".
    """
    if not _dataset_allowed(dataset_id):
        return json.dumps({"error": f"dataset '{dataset_id}' is not in the allowlist"})
    ref = bigquery.DatasetReference(PROJECT_ID, dataset_id)
    tables = [
        {"table_id": t.table_id, "type": t.table_type}
        for t in client().list_tables(ref)
    ]
    return json.dumps({"dataset": dataset_id, "tables": tables}, indent=2)


@mcp.tool()
def get_table_schema(dataset_id: str, table_id: str) -> str:
    """Get the column schema for a table, plus row count and size.

    Use this before writing a query so the SQL references real columns.

    Args:
        dataset_id: The dataset, e.g. "sales".
        table_id: The table or view name.
    """
    if not _dataset_allowed(dataset_id):
        return json.dumps({"error": f"dataset '{dataset_id}' is not in the allowlist"})
    ref = bigquery.TableReference(
        bigquery.DatasetReference(PROJECT_ID, dataset_id), table_id
    )
    table = client().get_table(ref)
    fields = [
        {
            "name": f.name,
            "type": f.field_type,
            "mode": f.mode,
            "description": f.description or "",
        }
        for f in table.schema
    ]
    return json.dumps(
        {
            "table": f"{PROJECT_ID}.{dataset_id}.{table_id}",
            "type": table.table_type,
            "num_rows": table.num_rows,
            "size_bytes": table.num_bytes,
            "description": table.description or "",
            "columns": fields,
        },
        indent=2,
        default=str,
    )


@mcp.tool()
def run_query(sql: str, max_rows: int = DEFAULT_ROW_LIMIT) -> str:
    """Run a read-only (SELECT/WITH) SQL query against BigQuery and return rows.

    The query is validated with a dry run first; only SELECT statements are
    permitted and a byte-scan cap protects against expensive queries. Always
    fully-qualify tables as `<project>.<dataset>.<table>`.

    Args:
        sql: A SELECT (or WITH ... SELECT) query.
        max_rows: Max rows to return to keep responses small (default 200).
    """
    # 1) Dry run: validate, estimate cost, and confirm it is read-only.
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        dry = client().query(sql, job_config=dry_cfg)
    except Exception as e:  # syntax errors, unknown tables, permission denials
        return json.dumps({"error": f"query validation failed: {e}"})

    if dry.statement_type not in READONLY_STATEMENT_TYPES:
        return json.dumps(
            {
                "error": (
                    f"only read-only SELECT queries are allowed; "
                    f"this is a '{dry.statement_type}' statement"
                )
            }
        )

    est_bytes = dry.total_bytes_processed or 0
    if est_bytes > MAX_BYTES_BILLED:
        return json.dumps(
            {
                "error": "query would scan too much data",
                "estimated_bytes": est_bytes,
                "limit_bytes": MAX_BYTES_BILLED,
                "hint": "Add filters (e.g. on a date/partition column) or select fewer columns.",
            }
        )

    # 2) Real run with a hard byte cap as a second line of defense.
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)
    job = client().query(sql, job_config=cfg)
    rows = [dict(r) for r in job.result(max_results=max_rows)]

    return json.dumps(
        {
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) >= max_rows,
            "bytes_processed": job.total_bytes_processed,
        },
        indent=2,
        default=str,
    )


if __name__ == "__main__":
    mcp.run()
