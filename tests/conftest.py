"""Shared fixtures and fakes.

Two things have to be true before any test runs, and both are easy to get
wrong:

**No ambient configuration reaches a test.** Settings are cached with
``lru_cache`` so the server resolves them once per process, and the developer
running these tests almost certainly has BQ_PROJECT and live credentials in
their environment. The module-level block below neutralises both *before* the
package is imported, because importing ``server`` builds the FastMCP object and
resolves settings as a side effect -- a fixture would run too late.

**No test touches BigQuery.** Everything here runs against the fakes in this
file, so the suite is deterministic, free, and works with no credentials at all.
"""

from __future__ import annotations

import os

# Set before any data_platform_mcp import; see the docstring.
os.environ["BQ_PROJECT"] = "test-project"
os.environ["BQ_LOCATION"] = "US"
os.environ["BQ_MCP_AUDIT_LOG"] = "off"
for _leaked in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "BQ_DATASET_ALLOWLIST",
                "BQ_WARN_BYTES", "BQ_MAX_BYTES_BILLED", "BQ_ROW_LIMIT",
                "BQ_COST_PER_TIB_USD", "BQ_MCP_LOG_LEVEL"):
    os.environ.pop(_leaked, None)

import pytest  # noqa: E402

from data_platform_mcp import clients, config  # noqa: E402


def clear_caches() -> None:
    config.get_settings.cache_clear()
    clients.reset_clients()


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    """Restore the baseline environment around every test."""
    monkeypatch.setenv("BQ_PROJECT", "test-project")
    monkeypatch.setenv("BQ_MCP_AUDIT_LOG", "off")
    clear_caches()
    yield
    clear_caches()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- Fakes standing in for google-cloud-bigquery -----------------------------


class FakeField:
    """A schema field, including nested ones."""

    def __init__(self, name, field_type="STRING", mode="NULLABLE", description="",
                 fields=()):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.description = description
        self.fields = list(fields)


class FakeTimePartitioning:
    def __init__(self, field=None, type_="DAY"):
        self.field = field
        self.type_ = type_


class FakeTable:
    def __init__(self, table_id="t", schema=(), num_rows=10, num_bytes=1024,
                 time_partitioning=None, clustering_fields=None, modified=None,
                 table_type="TABLE", view_query=None, require_partition_filter=False):
        self.table_id = table_id
        self.schema = list(schema)
        self.num_rows = num_rows
        self.num_bytes = num_bytes
        self.time_partitioning = time_partitioning
        self.range_partitioning = None
        self.require_partition_filter = require_partition_filter
        self.clustering_fields = clustering_fields
        self.modified = modified
        self.table_type = table_type
        self.view_query = view_query
        self.description = ""


class FakeQueryJob:
    """A completed query job. ``rows`` are plain dicts; ``dict(row)`` copies."""

    def __init__(self, rows=(), total_bytes_processed=0, statement_type="SELECT",
                 cache_hit=False):
        self._rows = list(rows)
        self.total_bytes_processed = total_bytes_processed
        self.statement_type = statement_type
        self.cache_hit = cache_hit

    def result(self, max_results=None):
        return iter(self._rows[:max_results] if max_results else self._rows)


class FakeClient:
    """Stands in for bigquery.Client.

    ``query`` returns the dry-run job when the config asks for one, so the same
    fake exercises both halves of run_query's cost gate.
    """

    def __init__(self, dry=None, job=None, tables=None, datasets=(), table=None):
        self.dry = dry or FakeQueryJob()
        self.job = job or FakeQueryJob()
        self.tables = tables or []
        self.datasets = datasets
        self.table = table
        self.queries: list[str] = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if job_config is not None and getattr(job_config, "dry_run", False):
            if isinstance(self.dry, Exception):
                raise self.dry
            return self.dry
        return self.job

    def list_datasets(self, project=None):
        return [type("DS", (), {"dataset_id": d})() for d in self.datasets]

    def list_tables(self, ref, max_results=None):
        return self.tables[:max_results] if max_results else self.tables

    def get_table(self, ref):
        return self.table


@pytest.fixture
def fake_client(monkeypatch):
    """Install a FakeClient into both tool modules; the test fills it in."""
    from data_platform_mcp.tools import discovery_tools, query_tools

    holder = FakeClient()

    def install(client):
        holder.__dict__.update(client.__dict__)
        return holder

    for module in (discovery_tools, query_tools):
        monkeypatch.setattr(module, "get_bigquery_client", lambda _settings: holder)
    holder.install = install
    return holder
