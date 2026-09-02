"""Running SQL, and the cost gate in front of it.

Three defences, in order, because they fail differently:

1. A dry run validates the SQL and prices it before a byte is scanned. It also
   reports the statement type, which is how anything that is not a SELECT is
   refused -- reliably, rather than by pattern-matching the text.
2. A soft threshold bounces a costly query back to the user for confirmation
   instead of spending their money on a guess.
3. ``maximum_bytes_billed`` on the real job, so an estimate that turns out low
   cannot run away.

A refusal is raised, not returned. An ``{"error": ...}`` payload leaves the
protocol's ``isError`` flag unset, so "I would not run that" arrives looking
exactly like "here are your results" -- the one shape a caller must never
confuse. The single exception is ``confirmation_required``, which is a genuine
outcome the agent is meant to relay and act on, not a failure.

The hard cap is a guardrail on this tool, not a BigQuery limit. Saying so in the
error matters: the correct answer to "too big" is often the Python client, and
people otherwise spend a while rediscovering that.
"""

from __future__ import annotations

import json

from ..clients import get_bigquery_client
from ..config import require_environment
from ..errors import DataPlatformMCPError, explain_exception
from ..formatting import format_cost, human_bytes, scan_estimate
from ..registration import register_tool

# Statement types BigQuery considers read-only and safe to run here.
READONLY_STATEMENT_TYPES = {"SELECT"}

# The real constraint on a result set is the caller's context window, not the
# row count: 200 narrow rows are nothing, 200 rows of a wide GA4 table are not.
# The response is therefore bounded as a whole, so short rows yield many and
# wide rows yield fewer -- and the caller is told when the budget, rather than
# the query, ended the list. Roughly 10k tokens.
_MAX_PAYLOAD_CHARS = 40_000


def run_query(
    sql: str,
    max_rows: int = 0,
    confirm_expensive: bool = False,
    environment: str = "",
) -> dict:
    """Run a read-only (SELECT/WITH) SQL query against BigQuery and return rows.

    Cost safety: the query is ALWAYS dry-run first to estimate how much data it
    will scan. If that estimate is above the warning threshold, the query does
    NOT run — instead this returns `status: "confirmation_required"` with the
    estimated size and cost. Stop there, tell the user the estimated scan and
    cost, and ask. Only re-call with confirm_expensive=true once they have
    agreed: that flag records the user's decision, not yours. Queries above the
    hard cap never run, even with confirmation.

    Always fully-qualify tables as `<project>.<dataset>.<table>`, and check
    get_table_schema first — a WHERE clause only limits the scan on a table
    that is actually partitioned.

    Args:
        sql: A SELECT (or WITH ... SELECT) query.
        max_rows: Max rows to return, to keep responses small. 0 (the default)
            uses the server's configured limit.
        confirm_expensive: Set True only after the user has agreed to a query
            previously flagged as costly. Leave False for the first attempt.
        environment: Which configured BigQuery environment to query. Omit to
            use the default. Call list_environments to see what exists.
    """
    env = require_environment(environment)
    client = get_bigquery_client(env)
    max_rows = max_rows if max_rows > 0 else env.row_limit

    from google.cloud import bigquery

    # 1) Dry run: validate, estimate cost, and confirm it is read-only.
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        dry = client.query(sql, job_config=dry_cfg)
    except Exception as exc:  # syntax errors, unknown tables, permission denials
        explained = explain_exception(exc, environment)
        # A credential or permission failure has a better message than anything
        # this function could add, so it passes through. Everything else is a
        # problem with the SQL, and saying so locates the fault for the caller.
        if explained is not exc:
            raise explained from exc
        raise DataPlatformMCPError(
            f"Environment '{env.name}': the query failed validation and did "
            f"not run: {exc}"
        ) from exc

    if dry.statement_type not in READONLY_STATEMENT_TYPES:
        raise DataPlatformMCPError(
            f"Environment '{env.name}': only read-only SELECT queries are "
            f"allowed; this is a "
            f"'{dry.statement_type}' statement. This server cannot write, "
            "create or delete anything."
        )

    est_bytes = dry.total_bytes_processed or 0
    scan = scan_estimate(est_bytes, env.cost_per_tib_usd)

    # 2a) Above the hard cap — never runs, regardless of confirmation.
    if est_bytes > env.max_bytes_billed:
        raise DataPlatformMCPError(
            f"In environment '{env.name}', this query would scan "
            f"{scan['estimated_scan']} "
            f"({format_cost(scan['estimated_cost_usd'])}), above this server's "
            f"{human_bytes(env.max_bytes_billed)} hard limit, so it did not "
            "run. Select fewer columns, or filter on a partition column if "
            "get_table_schema shows the table has one.\n"
            "Note that this cap is a guardrail on this tool, not a BigQuery "
            "limit — a genuinely large query can be run directly with the "
            "google-cloud-bigquery Python client, which has no such cap."
        )

    # 2b) Costly but allowed — hand the decision back to the user. This is a
    # result, not an error: the agent is supposed to act on it.
    if est_bytes > env.warn_bytes and not confirm_expensive:
        return {
            "environment": env.name,
            "project": env.project,
            "status": "confirmation_required",
            "message": (
                f"This query will scan about {scan['estimated_scan']} "
                f"({format_cost(scan['estimated_cost_usd'])}), which is above the "
                "cost warning threshold. Tell the user the estimated size and "
                "cost and ask whether to proceed. If they agree, call run_query "
                "again with the same SQL and confirm_expensive=true."
            ),
            **scan,
            "warn_threshold_bytes": env.warn_bytes,
        }

    # 3) Real run with a hard byte cap as a second line of defence.
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=env.max_bytes_billed)
    job = client.query(sql, job_config=cfg)

    rows: list[dict] = []
    used = 0
    stopped_for_size = False
    for record in job.result(max_results=max_rows):
        row = dict(record)
        size = len(json.dumps(row, default=str))
        # Always return at least one row, so a single wide row is visible
        # rather than silently producing an empty result set.
        if rows and used + size > _MAX_PAYLOAD_CHARS:
            stopped_for_size = True
            break
        rows.append(row)
        used += size

    result = {
        "environment": env.name,
        "project": env.project,
        "rows": rows,
        "row_count": len(rows),
        "truncated": stopped_for_size or len(rows) >= max_rows,
        "bytes_processed": job.total_bytes_processed,
        "cache_hit": job.cache_hit,
        **scan_estimate(job.total_bytes_processed or 0, env.cost_per_tib_usd),
    }
    if stopped_for_size:
        # Say so, rather than letting a size-limited page look like the whole
        # result set — a partial answer mistaken for a complete one is worse
        # than no answer.
        result["stopped_for_size"] = True
        result["requested_max_rows"] = max_rows
        result["note"] = (
            f"Returned {len(rows)} of up to {max_rows} rows to stay within a "
            "response size budget. These rows are wide; select fewer columns "
            "or aggregate in SQL to see more of the result."
        )
    return result


def register(mcp) -> None:
    register_tool(mcp, run_query)
