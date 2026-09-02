"""Schema-discovery tools: what data exists, what shape it is, and is it alive.

These are the calls that make a query correct before it is expensive. All three
are free -- schema and metadata reads scan no bytes -- so an agent should always
land here before run_query rather than guessing column names and paying for the
mistake in scanned bytes.

Two things here exist because of specific, repeated failures against this
platform:

**Partitioning is reported from the table metadata, never inferred from column
names.** ``events_notification`` has a ``partition_date`` column and is
not partitioned, while its siblings in the same dataset are partitioned on
exactly that column. Guessing from the name turns a filtered query into a
several-hundred-gigabyte full scan.

**Nested fields are flattened to dotted paths.** A GA4 event table reports 32
top-level columns and 218 fields once RECORDs are expanded. Reporting only the
top level tells the agent that ``event_params`` exists but not that it is a
repeated key/value struct, which is the difference between working SQL and a
guess at ``UNNEST``.
"""

from __future__ import annotations

from ..clients import get_bigquery_client
from ..config import require_environment
from ..errors import DataPlatformMCPError
from ..registration import register_tool

# Flattening a GA4 event table yields ~218 fields. That is worth returning; a
# pathological schema is not, so the list is capped and says when it was cut.
_MAX_FIELDS = 400

# Datasets here hold up to a few dozen tables. The cap exists so an unexpected
# dataset cannot dominate the context window, not because it is usually near.
_MAX_TABLES = 500

# Below this, an unpartitioned table is not worth warning about: a full scan of
# it is cheap enough that the advice would be noise.
_UNPARTITIONED_WARNING_BYTES = 10 * 1024**3


def _require_allowed(env, dataset_id: str) -> None:
    """Reject a dataset outside the allowlist, as an error the client can see.

    Returning an ``{"error": ...}`` payload instead would leave ``isError``
    unset, so a refusal would arrive looking exactly like a result.
    """
    if not env.dataset_allowed(dataset_id):
        allowed = ", ".join(sorted(env.dataset_allowlist))
        raise DataPlatformMCPError(
            f"Dataset '{dataset_id}' is not in the allowlist for environment "
            f"'{env.name}'. Readable datasets: {allowed}."
        )


def _flatten(fields, prefix: str = "") -> list[dict]:
    """Expand a BigQuery schema into dotted paths, depth-first."""
    out: list[dict] = []
    for f in fields:
        name = f"{prefix}{f.name}"
        row = {"name": name, "type": f.field_type}
        # Only the facts that change how SQL must be written are recorded.
        # REPEATED means the column needs UNNEST; NULLABLE is the default and
        # saying so on every one of 218 rows is pure payload.
        if f.mode == "REPEATED":
            row["repeated"] = True
        elif f.mode == "REQUIRED":
            row["required"] = True
        if f.description:
            row["description"] = f.description
        out.append(row)
        if f.fields:
            out.extend(_flatten(f.fields, f"{name}."))
    return out


def _partitioning(table) -> dict | None:
    """Report partitioning as BigQuery actually has it, or None."""
    tp = table.time_partitioning
    if tp is not None:
        return {
            "kind": "time",
            # A None field means the ingestion-time pseudo-column _PARTITIONTIME.
            "field": tp.field or "_PARTITIONTIME",
            "granularity": tp.type_,
            "require_filter": bool(table.require_partition_filter),
        }
    rp = table.range_partitioning
    if rp is not None:
        rng = rp.range_
        return {
            "kind": "range",
            "field": rp.field,
            "start": getattr(rng, "start", None),
            "end": getattr(rng, "end", None),
            "interval": getattr(rng, "interval", None),
            "require_filter": bool(table.require_partition_filter),
        }
    return None


def list_datasets(environment: str = "") -> dict:
    """List the BigQuery datasets available in the data platform project.

    Call this first to discover what data exists. Free — scans no data.

    Args:
        environment: Which configured BigQuery environment to use. Omit to
            use the default. Call list_environments to see what exists.
    """
    env = require_environment(environment)
    client = get_bigquery_client(env)
    datasets = sorted(
        ds.dataset_id
        for ds in client.list_datasets(project=env.project)
        if env.dataset_allowed(ds.dataset_id)
    )
    return {
        "environment": env.name,
        "project": env.project,
        "location": env.location,
        "count": len(datasets),
        "datasets": datasets,
    }


def list_tables(dataset_id: str, environment: str = "") -> dict:
    """List tables and views inside a dataset. Free — scans no data.

    Args:
        dataset_id: The dataset to inspect, e.g. "events_raw".
        environment: Which configured BigQuery environment to use. Omit to
            use the default. Call list_environments to see what exists.
    """
    env = require_environment(environment)
    _require_allowed(env, dataset_id)

    from google.cloud import bigquery

    ref = bigquery.DatasetReference(env.project, dataset_id)
    tables = [
        {"table_id": t.table_id, "type": t.table_type}
        for t in get_bigquery_client(env).list_tables(ref, max_results=_MAX_TABLES)
    ]
    result = {
        "environment": env.name,
        "dataset": dataset_id,
        "count": len(tables),
        "tables": tables,
    }
    if len(tables) >= _MAX_TABLES:
        result["truncated"] = True
        result["note"] = (
            f"Listing stopped at {_MAX_TABLES} tables; the dataset may hold more."
        )
    return result


def get_table_schema(dataset_id: str, table_id: str, environment: str = "") -> dict:
    """Get a table's columns, partitioning, size and freshness. Free — scans no data.

    Call this before writing a query, for two reasons beyond column names:

    - `partitioning` says whether a WHERE clause can actually limit the scan. A
      date-shaped column name does NOT mean the table is partitioned; if this
      field is null, every query reads the whole table.
    - Nested columns are expanded to dotted paths and flagged `repeated`, which
      is what tells you a column needs UNNEST.

    Args:
        dataset_id: The dataset, e.g. "events_raw".
        table_id: The table or view name.
        environment: Which configured BigQuery environment to use. Omit to
            use the default. Call list_environments to see what exists.
    """
    env = require_environment(environment)
    _require_allowed(env, dataset_id)

    from google.cloud import bigquery

    ref = bigquery.TableReference(
        bigquery.DatasetReference(env.project, dataset_id), table_id
    )
    table = get_bigquery_client(env).get_table(ref)

    columns = _flatten(table.schema)
    truncated = len(columns) > _MAX_FIELDS
    partitioning = _partitioning(table)

    result = {
        "environment": env.name,
        "table": f"{env.project}.{dataset_id}.{table_id}",
        "type": table.table_type,
        "num_rows": table.num_rows,
        "size_bytes": table.num_bytes,
        "last_modified": table.modified.isoformat() if table.modified else None,
        "partitioning": partitioning,
        "clustering_fields": table.clustering_fields or [],
        "description": table.description or "",
        "column_count": len(columns),
        "columns": columns[:_MAX_FIELDS],
    }
    if truncated:
        result["columns_truncated"] = True
        result["note"] = (
            f"Showing {_MAX_FIELDS} of {len(columns)} fields."
        )
    if table.table_type == "VIEW" and table.view_query:
        # A view's cost comes from what it reads, which the definition is the
        # only way to see. Its own size_bytes is 0 and says nothing.
        result["view_query"] = table.view_query
    if partitioning is None and (table.num_bytes or 0) > _UNPARTITIONED_WARNING_BYTES:
        result["unpartitioned_warning"] = (
            "This table is NOT partitioned, so every query scans all "
            f"{(table.num_bytes or 0) / 1024**3:.0f} GiB no matter what the WHERE "
            "clause says — a date-named column here does not limit the scan. "
            "Select fewer columns, or look for a partitioned equivalent in the "
            "same dataset."
        )
    return result


def check_table_freshness(dataset_id: str, table_id: str = "", environment: str = "") -> dict:
    """Report when tables were last written, to catch stale or dead sources.

    Several plausible-looking tables on this platform stopped being updated
    without being dropped, so a query against one silently returns old data.
    Check before trusting a table you have not used before.

    Free — reads table metadata only, scanning no data.

    Args:
        dataset_id: The dataset to check, e.g. "events_raw".
        table_id: A single table to check. Omit to report every table in the
            dataset, which is the faster way to spot a dead one.
        environment: Which configured BigQuery environment to use. Omit to
            use the default. Call list_environments to see what exists.
    """
    from datetime import datetime, timezone

    env = require_environment(environment)
    _require_allowed(env, dataset_id)
    client = get_bigquery_client(env)
    now = datetime.now(timezone.utc)

    def age(modified) -> int | None:
        return (now - modified).days if modified else None

    if table_id:
        from google.cloud import bigquery

        table = client.get_table(
            bigquery.TableReference(
                bigquery.DatasetReference(env.project, dataset_id), table_id
            )
        )
        rows = [
            {
                "table_id": table.table_id,
                "num_rows": table.num_rows,
                "size_bytes": table.num_bytes,
                "last_modified": table.modified.isoformat() if table.modified else None,
                "days_since_modified": age(table.modified),
            }
        ]
    else:
        # __TABLES__ costs zero bytes and answers for the whole dataset in one
        # job, where get_table would be one API call per table.
        query = (
            "SELECT table_id, row_count, size_bytes, "
            "TIMESTAMP_MILLIS(last_modified_time) AS last_modified "
            f"FROM `{env.project}.{dataset_id}.__TABLES__` "
            "ORDER BY last_modified_time DESC"
        )
        rows = [
            {
                "table_id": r.table_id,
                "num_rows": r.row_count,
                "size_bytes": r.size_bytes,
                "last_modified": r.last_modified.isoformat() if r.last_modified else None,
                "days_since_modified": age(r.last_modified),
            }
            for r in client.query(query).result()
        ]

    stale = [r["table_id"] for r in rows if (r["days_since_modified"] or 0) > 30]
    result = {
        "environment": env.name,
        "dataset": dataset_id,
        "checked_at": now.isoformat(),
        "count": len(rows),
        "tables": rows,
    }
    if stale:
        result["stale_tables"] = stale
        result["note"] = (
            f"{len(stale)} table(s) have not been written to in over 30 days: "
            f"{', '.join(stale[:10])}. Confirm with the user before treating "
            "these as current."
        )
    return result


def register(mcp) -> None:
    register_tool(mcp, list_datasets)
    register_tool(mcp, list_tables)
    register_tool(mcp, get_table_schema)
    register_tool(mcp, check_table_freshness)
