"""The BigQuery client, built once and reused.

A ``bigquery.Client`` performs credential discovery on construction, so building
one per tool call adds latency to every query for no benefit. It is cached here
rather than in a tool module so that the cache has one owner and tests have one
thing to clear.
"""

from __future__ import annotations

from functools import lru_cache

from .config import Settings


@lru_cache(maxsize=4)
def _client_for(project: str, location: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project, location=location)


def get_bigquery_client(settings: Settings):
    """Return the cached client for these settings."""
    return _client_for(settings.project, settings.location)


def reset_clients() -> None:
    """Drop cached clients. Used by tests and after a config change."""
    _client_for.cache_clear()
