"""Scheduled queries: what writes a table, and why it stopped.

``check_table_freshness`` can say a table went stale but not why. The answer is
almost always here -- a scheduled query that was disabled, or that has been
failing -- and until now that meant leaving the conversation to go and look in
the console.

These read the BigQuery Data Transfer Service, which is a different API from
BigQuery itself and a different permission: ``roles/bigquerydatatransfer.viewer``.
A viewer-only account created for querying does not have it, so both tools
degrade to an error naming the role rather than failing obscurely.

Nothing here can create, pause, or trigger a run. The service offers those and
they are simply not registered.
"""

from __future__ import annotations

import json
import re

from ..config import require_environment
from ..errors import DataPlatformMCPError
from ..registration import register_tool

# 118 configs on a real platform carry ~510 KiB of SQL between them, so the
# listing never includes it. get_scheduled_query returns it for one config,
# which is the shape the question actually takes.
_MAX_PAYLOAD_CHARS = 40_000

# Only scheduled queries, not the other transfer types (S3, Ads, ...) that
# share this API and would be noise in a data-platform context.
_SCHEDULED_QUERY = "scheduled_query"


def _client(env):
    try:
        from google.cloud import bigquery_datatransfer_v1
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise DataPlatformMCPError(
            "The BigQuery Data Transfer library is not installed, so scheduled "
            "queries cannot be read. Reinstall the server: "
            "pip install --upgrade data-platform-mcp"
        ) from exc

    from ..clients import get_credentials

    return bigquery_datatransfer_v1.DataTransferServiceClient(
        credentials=get_credentials(env.impersonate)
    )


def _parent(env) -> str:
    # Transfer configs are addressed by a lowercase location; BigQuery reports
    # multi-regions in capitals ("US"), so they would never match.
    return f"projects/{env.project}/locations/{env.location.lower()}"


def _explain_transfer_failure(exc: Exception, env) -> Exception:
    from google.api_core import exceptions as api_exceptions

    if isinstance(exc, api_exceptions.Forbidden):
        identity = env.impersonate or "the signed-in user"
        return DataPlatformMCPError(
            f"Environment '{env.name}': permission denied reading scheduled "
            f"queries. {identity} needs roles/bigquerydatatransfer.viewer on "
            f"{env.project} -- a different permission from querying, so the "
            "other tools working does not imply this one will.\n"
            f"    gcloud projects add-iam-policy-binding {env.project} \\\n"
            f"      --member='{'serviceAccount:' + env.impersonate if env.impersonate else 'user:EMAIL'}' \\\n"
            "      --role=roles/bigquerydatatransfer.viewer\n"
            f"Underlying error: {str(exc)[:200]}"
        )
    if isinstance(exc, api_exceptions.NotFound):
        return DataPlatformMCPError(
            f"Environment '{env.name}': no scheduled-query service in location "
            f"'{env.location}' for project {env.project}. Scheduled queries are "
            "regional; check the environment's location matches where they were "
            "created."
        )
    return exc


def _state_name(state) -> str:
    from google.cloud import bigquery_datatransfer_v1 as dt

    try:
        return dt.TransferState(state).name
    except ValueError:  # pragma: no cover - defensive
        return str(state)


# Most scheduled queries here declare no destination_dataset_id, because they
# write with DDL/DML and the target lives in the SQL: of 118 real configs, 107
# were shaped that way. Without this, "what writes this table?" -- the question
# the tool exists for -- would be unanswerable for 90% of them. Measured over
# those 107, this recovers 106 with no unqualified false positives; the miss is
# a CALL into a stored procedure, where the target genuinely is not in the text.
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_WRITE_TARGET = re.compile(
    r"(?is)\b(?:create\s+(?:or\s+replace\s+)?(?:table|view)(?:\s+if\s+not\s+exists)?"
    r"|insert\s+into|merge\s+into|merge|delete\s+from|truncate\s+table|update)"
    r"\s+(`[^`]+`|[A-Za-z0-9_][A-Za-z0-9_.\-]*)"
)


def _targets_from_sql(sql: str) -> list[str]:
    """Tables a scheduled query writes to, read out of its SQL.

    A heuristic, and reported as one: the result is surfaced under
    ``writes_to_from_sql`` rather than presented as authoritative metadata.
    """
    body = _COMMENTS.sub(" ", sql or "")
    found: list[str] = []
    for match in _WRITE_TARGET.finditer(body):
        raw = match.group(1).strip()
        target = raw.strip("`")
        # A destination is qualified: backticked, or dataset.table. That one
        # rule drops the keywords a looser match picks up (SET, ROW, VALUES).
        if "." not in target and not raw.startswith("`"):
            continue
        if target not in found:
            found.append(target)
    return found


def _short_id(resource_name: str) -> str:
    """The trailing id of a transferConfigs/... resource name."""
    return resource_name.rsplit("/", 1)[-1]


def _summarise(config) -> dict:
    params = dict(config.params)
    table = params.get("destination_table_name_template", "")
    row = {
        "name": config.display_name,
        "id": _short_id(config.name),
        "schedule": config.schedule or "(on demand)",
        "destination": (
            f"{config.destination_dataset_id}.{table}"
            if config.destination_dataset_id and table
            else config.destination_dataset_id or ""
        ),
        "last_state": _state_name(config.state),
        "disabled": bool(config.disabled),
    }
    if config.next_run_time and not config.disabled:
        row["next_run"] = config.next_run_time.isoformat()
    if not row["destination"]:
        targets = _targets_from_sql(params.get("query", ""))
        if targets:
            row["writes_to_from_sql"] = targets
    return row


def list_scheduled_queries(
    dataset: str = "",
    contains: str = "",
    include_disabled: bool = True,
    environment: str = "",
) -> dict:
    """List scheduled queries: what they write, when they run, and their state.

    Use this to answer "what populates this table?" and "why is this table
    stale?" — a disabled or failing scheduled query is the usual cause, and
    check_table_freshness can see the staleness but not the reason.

    The SQL is not included here; call get_scheduled_query for one of them.

    Args:
        dataset: Only queries writing into this destination dataset.
        contains: Only queries whose name contains this text.
        include_disabled: Keep disabled queries in the result. They are the
            most likely explanation for a table that stopped updating, so this
            defaults to True.
        environment: Which configured environment to look in. Scheduled queries
            are regional, so this must be the environment whose location holds
            them.
    """
    env = require_environment(environment)
    client = _client(env)

    try:
        configs = list(client.list_transfer_configs(parent=_parent(env)))
    except Exception as exc:
        raise _explain_transfer_failure(exc, env) from exc

    rows, used, truncated = [], 0, False
    for config in configs:
        if config.data_source_id != _SCHEDULED_QUERY:
            continue
        if not include_disabled and config.disabled:
            continue
        if dataset and config.destination_dataset_id != dataset:
            # A query writing via DDL declares no destination dataset, so
            # filtering on the declared field alone would hide most of them.
            derived = _targets_from_sql(dict(config.params).get("query", ""))
            if not any(f".{dataset}." in t or t.startswith(f"{dataset}.")
                       for t in derived):
                continue
        if contains and contains.lower() not in config.display_name.lower():
            continue
        row = _summarise(config)
        size = len(json.dumps(row, default=str))
        if rows and used + size > _MAX_PAYLOAD_CHARS:
            truncated = True
            break
        rows.append(row)
        used += size

    disabled = [r["name"] for r in rows if r["disabled"]]
    failing = [r["name"] for r in rows if r["last_state"] == "FAILED"]

    result = {
        "environment": env.name,
        "project": env.project,
        "location": env.location,
        "count": len(rows),
        "scheduled_queries": rows,
    }
    if disabled:
        result["disabled_queries"] = disabled
    if failing:
        result["failing_queries"] = failing
    if disabled or failing:
        result["note"] = (
            f"{len(disabled)} disabled, {len(failing)} failing. If a table is "
            "stale, check whether the query that writes it is one of these "
            "before assuming the data source is at fault."
        )
    if truncated:
        result["truncated"] = True
    return result


def get_scheduled_query(
    query: str, runs: int = 5, environment: str = ""
) -> dict:
    """Get one scheduled query in full: its SQL, destination, and recent runs.

    Call this after list_scheduled_queries to see why a query is failing, or
    what SQL actually produces a table.

    Args:
        query: The scheduled query's name, or the id from list_scheduled_queries.
        runs: How many recent runs to include, newest first.
        environment: Which configured environment to look in.
    """
    import itertools

    env = require_environment(environment)
    client = _client(env)

    try:
        configs = [
            c
            for c in client.list_transfer_configs(parent=_parent(env))
            if c.data_source_id == _SCHEDULED_QUERY
        ]
    except Exception as exc:
        raise _explain_transfer_failure(exc, env) from exc

    wanted = query.strip().lower()
    matches = [
        c
        for c in configs
        if c.display_name.lower() == wanted or _short_id(c.name).lower() == wanted
    ] or [c for c in configs if wanted in c.display_name.lower()]

    if not matches:
        raise DataPlatformMCPError(
            f"No scheduled query matching '{query}' in environment "
            f"'{env.name}'. Call list_scheduled_queries to see what exists."
        )
    if len(matches) > 1:
        names = ", ".join(sorted(c.display_name for c in matches)[:8])
        raise DataPlatformMCPError(
            f"'{query}' matches {len(matches)} scheduled queries: {names}. "
            "Use the full name or the id."
        )

    config = matches[0]
    params = dict(config.params)
    result = _summarise(config)
    result.update(
        {
            "environment": env.name,
            "project": env.project,
            "write_disposition": params.get("write_disposition", ""),
            "sql": params.get("query", ""),
        }
    )

    try:
        recent = list(
            itertools.islice(
                client.list_transfer_runs(request={"parent": config.name}),
                max(1, min(runs, 50)),
            )
        )
    except Exception as exc:
        raise _explain_transfer_failure(exc, env) from exc

    result["runs"] = [
        {
            "run_time": r.run_time.isoformat() if r.run_time else None,
            "state": _state_name(r.state),
            "error": r.error_status.message or None,
        }
        for r in recent
    ]
    errors = [r for r in result["runs"] if r["error"]]
    if errors:
        result["note"] = (
            f"{len(errors)} of the last {len(result['runs'])} runs failed. The "
            "error text is on each run."
        )
    return result


def register(mcp) -> None:
    register_tool(mcp, list_scheduled_queries)
    register_tool(mcp, get_scheduled_query)
