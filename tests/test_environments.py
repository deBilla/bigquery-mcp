"""Routing between environments, and the identity each one runs as.

The failure this guards is quiet: a question about one warehouse answered from
another. Nothing in the reply reveals it, so the protections are structural —
an unknown name raises, and every result names the environment it came from.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeQueryJob, clear_caches
from data_platform_mcp import clients
from data_platform_mcp.tools import environment_tools, query_tools

REGISTRY = {
    "warehouse": {"project": "demo-warehouse", "aliases": ["dwh"]},
    "production": {
        "project": "demo-prod",
        "impersonate": "ro@demo-prod.iam.gserviceaccount.com",
        "dataset_allowlist": ["sales"],
    },
}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps(REGISTRY))
    monkeypatch.setenv("BQ_MCP_DEFAULT_ENVIRONMENT", "warehouse")
    clear_caches()
    yield
    clear_caches()


# --- results name where they came from --------------------------------------


def test_a_query_result_names_the_environment_it_came_from(registry, fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=[{"n": 1}], total_bytes_processed=10)

    result = query_tools.run_query("SELECT 1", environment="production")
    assert result["environment"] == "production"
    assert result["project"] == "demo-prod"


def test_omitting_the_environment_uses_the_default(registry, fake_client):
    fake_client.dry = FakeQueryJob(total_bytes_processed=10)
    fake_client.job = FakeQueryJob(rows=[], total_bytes_processed=10)
    assert query_tools.run_query("SELECT 1")["environment"] == "warehouse"


def test_a_gated_query_also_names_its_environment(registry, fake_client):
    """confirmation_required is what the user is asked to approve, so it has to
    say which warehouse the money would be spent on."""
    fake_client.dry = FakeQueryJob(total_bytes_processed=2 * 1024**3)
    result = query_tools.run_query("SELECT *", environment="production")
    assert result["status"] == "confirmation_required"
    assert result["environment"] == "production"


def test_an_unknown_environment_raises_before_any_query_runs(registry, fake_client):
    with pytest.raises(ValueError) as exc:
        query_tools.run_query("SELECT 1", environment="prodution")
    assert "prodution" in str(exc.value)
    assert fake_client.queries == []


# --- per-environment settings -----------------------------------------------


def test_each_environment_enforces_its_own_allowlist(registry, fake_client):
    from data_platform_mcp.errors import DataPlatformMCPError
    from data_platform_mcp.tools import discovery_tools

    fake_client.datasets = ("sales", "payroll")

    # warehouse has no allowlist, so everything is readable there...
    discovery_tools.list_tables("payroll", environment="warehouse")

    # ...while production restricts to sales, and says so by name.
    with pytest.raises(DataPlatformMCPError) as exc:
        discovery_tools.list_tables("payroll", environment="production")
    assert "production" in str(exc.value) and "sales" in str(exc.value)


def test_limits_are_per_environment(monkeypatch, fake_client):
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps({
        "strict": {"project": "p", "warn_bytes": 100},
        "loose": {"project": "p", "warn_bytes": 10_000_000_000},
    }))
    clear_caches()
    fake_client.dry = FakeQueryJob(total_bytes_processed=1000)
    fake_client.job = FakeQueryJob(rows=[{"n": 1}], total_bytes_processed=1000)

    assert query_tools.run_query("SELECT 1", environment="strict")["status"] == (
        "confirmation_required"
    )
    assert "status" not in query_tools.run_query("SELECT 1", environment="loose")


# --- list_environments ------------------------------------------------------


def test_list_environments_reports_the_registry(registry):
    result = environment_tools.list_environments()
    assert result["default_environment"] == "warehouse"
    by_name = {e["name"]: e for e in result["environments"]}
    assert by_name["warehouse"]["is_default"] is True
    assert by_name["warehouse"]["aliases"] == ["dwh"]
    # Naming the readable datasets lets an agent explain a refusal rather than
    # discovering it by triggering one.
    assert by_name["production"]["readable_datasets"] == ["sales"]
    assert by_name["warehouse"]["readable_datasets"] == "all"


def test_list_environments_touches_nothing_outside_this_process(registry, fake_client):
    environment_tools.list_environments()
    assert fake_client.queries == []


# --- impersonation ----------------------------------------------------------


def test_no_impersonation_configured_uses_your_own_credentials(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("google.auth.default", lambda **kw: (sentinel, "p"))
    clients.reset_clients()
    assert clients.get_credentials("") is sentinel


def test_impersonation_targets_the_configured_service_account(monkeypatch):
    """This is what makes read-only a property of the identity rather than a
    promise about the code, so the target must be exactly what was configured."""
    source = object()
    captured = {}

    class FakeImpersonated:
        def __init__(self, source_credentials, target_principal, target_scopes):
            captured["source"] = source_credentials
            captured["principal"] = target_principal
            captured["scopes"] = target_scopes

    monkeypatch.setattr("google.auth.default", lambda **kw: (source, "p"))
    monkeypatch.setattr(
        "google.auth.impersonated_credentials.Credentials", FakeImpersonated
    )
    clients.reset_clients()

    result = clients.get_credentials("ro@demo-prod.iam.gserviceaccount.com")
    assert isinstance(result, FakeImpersonated)
    assert captured["principal"] == "ro@demo-prod.iam.gserviceaccount.com"
    assert captured["source"] is source
    assert captured["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]


def test_clients_are_cached_per_environment_not_globally(monkeypatch):
    """Two environments must never share a client: they differ in project,
    location and identity, and reusing one would query the wrong warehouse."""
    built = []

    class FakeBQClient:
        def __init__(self, project, location, credentials):
            built.append((project, location))

    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps({
        "warehouse": {"project": "demo-warehouse"},
        "central": {"project": "demo-warehouse", "location": "us-central1"},
    }))
    # Patch beneath get_credentials rather than over it, so the real cached
    # function stays in place and reset_clients() keeps working in teardown.
    monkeypatch.setattr("google.auth.default", lambda **kw: (object(), "p"))
    monkeypatch.setattr("google.cloud.bigquery.Client", FakeBQClient)
    clear_caches()

    from data_platform_mcp.config import resolve_environment

    clients.get_bigquery_client(resolve_environment("warehouse"))
    clients.get_bigquery_client(resolve_environment("central"))
    clients.get_bigquery_client(resolve_environment("warehouse"))  # cached

    # Same project, different location: still two clients, because a query
    # cannot cross locations.
    assert built == [("demo-warehouse", "US"), ("demo-warehouse", "us-central1")]


def test_an_impersonation_denial_names_the_role_to_grant(registry):
    """A 403 from the IAM Credentials API arrives as a RefreshError naming
    neither the account nor the role, which is a different fix from expiry."""
    from google.auth import exceptions as auth_exceptions

    from data_platform_mcp.errors import explain_exception

    denial = auth_exceptions.RefreshError(
        "Unable to acquire impersonated credentials: 403 ... "
        "iam.serviceAccounts.getAccessToken"
    )
    out = explain_exception(denial, "production")
    text = str(out)
    assert "ro@demo-prod.iam.gserviceaccount.com" in text
    assert "roles/iam.serviceAccountTokenCreator" in text


def test_ordinary_expiry_still_says_to_log_in_again(registry):
    from google.auth import exceptions as auth_exceptions

    from data_platform_mcp.errors import explain_exception

    out = explain_exception(auth_exceptions.RefreshError("token expired"), "warehouse")
    assert "gcloud auth application-default login" in str(out)
    assert "TokenCreator" not in str(out)
