"""BigQuery clients, built once per environment and reused.

A ``bigquery.Client`` performs credential discovery on construction, so building
one per tool call adds latency to every query for no benefit.

**Impersonation is what makes "read-only" a property of the identity.** The
SELECT-only guard and the ``readOnlyHint`` annotations are promises about this
code; pointing the server at a service account holding only
``roles/bigquery.jobUser`` and a dataset-scoped ``roles/bigquery.dataViewer``
makes it a fact about the credentials, enforced by IAM whatever the code does
and whatever the operator's own roles allow. It also moves the dataset
allowlist from an ``if`` statement in this process to a grant Google enforces.
"""

from __future__ import annotations

from functools import lru_cache

from .config import Environment

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@lru_cache(maxsize=8)
def get_credentials(impersonate: str = ""):
    """Credentials for an environment: ADC, optionally impersonating.

    The caller running this needs ``roles/iam.serviceAccountTokenCreator`` on
    the target account; without it, impersonation fails at token refresh with a
    403 that names neither the account nor the role, which errors.py translates.
    """
    import google.auth

    source, _ = google.auth.default(scopes=_SCOPES)
    if not impersonate:
        return source

    from google.auth import impersonated_credentials

    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=impersonate,
        target_scopes=_SCOPES,
    )


@lru_cache(maxsize=8)
def _client_for(project: str, location: str, impersonate: str):
    from google.cloud import bigquery

    return bigquery.Client(
        project=project,
        location=location,
        credentials=get_credentials(impersonate),
    )


def get_bigquery_client(env: Environment):
    """Return the cached client for this environment."""
    return _client_for(env.project, env.location, env.impersonate)


def reset_clients() -> None:
    """Drop cached clients and credentials. Used by tests and after a config change.

    Tolerates either function having been replaced: this runs in test teardown,
    where a test may still have ``get_credentials`` monkeypatched to a plain
    function with no cache. Failing there would report a fixture-ordering
    detail as a test error and hide whatever the test actually found.
    """
    for cached in (_client_for, get_credentials):
        clear = getattr(cached, "cache_clear", None)
        if clear is not None:
            clear()
