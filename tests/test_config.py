"""The environment registry, and the failure that used to kill the process."""

from __future__ import annotations

import json

import pytest

from conftest import clear_caches
from data_platform_mcp import config
from data_platform_mcp.config import (
    get_settings,
    require_environment,
    resolve_environment,
)

TWO_ENVIRONMENTS = {
    "warehouse": {"project": "demo-warehouse", "aliases": ["dwh"]},
    "production": {
        "project": "demo-prod",
        "location": "us-central1",
        "dataset_allowlist": ["sales"],
        "warn_bytes": 512,
    },
}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps(TWO_ENVIRONMENTS))
    clear_caches()
    yield
    clear_caches()


# --- single-environment mode (the pre-existing setup) -----------------------


def test_bare_bq_project_still_works_without_any_registry():
    """An existing single-project configuration must keep working untouched."""
    settings = get_settings()
    assert [e.name for e in settings.environments] == ["default"]
    assert settings.environments[0].project == "test-project"


def test_defaults_are_the_documented_ones():
    env = resolve_environment()
    assert env.location == "US"
    assert env.max_bytes_billed == 5 * 1024**3
    assert env.warn_bytes == 1 * 1024**3
    assert env.row_limit == 200
    assert env.dataset_allowlist == frozenset()


@pytest.mark.parametrize("bad", ["not-a-number", "-1", "0", ""])
def test_an_unusable_limit_falls_back_instead_of_failing(monkeypatch, bad):
    # A typo in a limit must never stop the server from starting.
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED", bad)
    clear_caches()
    assert resolve_environment().max_bytes_billed == 5 * 1024**3


def test_allowlist_is_parsed_and_enforced(monkeypatch):
    monkeypatch.setenv("BQ_DATASET_ALLOWLIST", " sales , marketing ,, ")
    clear_caches()
    env = resolve_environment()
    assert env.dataset_allowlist == {"sales", "marketing"}
    assert env.dataset_allowed("sales")
    assert not env.dataset_allowed("payroll")


def test_an_empty_allowlist_permits_everything():
    assert resolve_environment().dataset_allowed("anything-at-all")


def test_project_falls_back_through_the_documented_variables(monkeypatch):
    monkeypatch.delenv("BQ_PROJECT")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-gcloud")
    clear_caches()
    assert resolve_environment().project == "from-gcloud"


def test_a_missing_project_is_a_readable_error_not_an_import_crash(monkeypatch):
    """The regression that motivated the restructure.

    Configuration used to be read at module scope, so an unset project killed
    the process before it could answer `initialize` -- which a client reports
    only as "server failed to start", with no tools and no message.
    """
    monkeypatch.delenv("BQ_PROJECT")
    monkeypatch.setattr(config, "_resolve_project", lambda: "")
    clear_caches()

    assert resolve_environment().project == ""  # resolving finds nothing, fine
    with pytest.raises(RuntimeError) as exc:
        require_environment()
    assert "BQ_PROJECT" in str(exc.value)
    assert "set-quota-project" in str(exc.value)


# --- the registry -----------------------------------------------------------


def test_environments_are_registered_with_their_own_settings(registry):
    prod = resolve_environment("production")
    assert prod.project == "demo-prod"
    assert prod.location == "us-central1"
    assert prod.dataset_allowlist == {"sales"}
    assert prod.warn_bytes == 512
    # Unset values fall back to the documented defaults, not to another
    # environment's.
    assert prod.max_bytes_billed == 5 * 1024**3


def test_a_bare_project_id_string_is_accepted_as_an_environment(monkeypatch):
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps({"prod": "just-a-project"}))
    clear_caches()
    assert resolve_environment("prod").project == "just-a-project"


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("production", "production"),
        ("prod", "production"),      # built-in shorthand
        ("PROD", "production"),      # case-insensitive
        ("live", "production"),
        ("dwh", "warehouse"),        # explicit alias
        ("demo-prod", "production"),  # the project id itself
    ],
)
def test_an_environment_can_be_named_the_ways_a_user_speaks(registry, spoken, expected):
    assert resolve_environment(spoken).name == expected


def test_an_unknown_environment_raises_and_names_the_options(registry):
    """Falling back to the default would answer a question about one warehouse
    from another, and nothing in the answer would reveal it."""
    with pytest.raises(ValueError) as exc:
        resolve_environment("waerhouse")
    assert "waerhouse" in str(exc.value)
    assert "warehouse" in str(exc.value) and "production" in str(exc.value)


def test_the_default_is_explicit_when_configured(registry, monkeypatch):
    monkeypatch.setenv("BQ_MCP_DEFAULT_ENVIRONMENT", "production")
    clear_caches()
    assert resolve_environment().name == "production"


def test_a_default_naming_an_unknown_environment_is_rejected(registry, monkeypatch):
    monkeypatch.setenv("BQ_MCP_DEFAULT_ENVIRONMENT", "nope")
    clear_caches()
    with pytest.raises(RuntimeError) as exc:
        get_settings()
    assert "nope" in str(exc.value)


def test_a_safer_environment_is_preferred_as_the_default(monkeypatch):
    """With no explicit default, a mistake should land somewhere cheap rather
    than on whichever key happened to be listed first."""
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps({
        "production": {"project": "p"},
        "staging": {"project": "s"},
    }))
    clear_caches()
    assert get_settings().default_environment == "staging"


def test_the_first_environment_is_the_default_when_none_is_safer(registry):
    assert get_settings().default_environment == "warehouse"


def test_malformed_json_is_a_readable_error(monkeypatch):
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", "{not json")
    clear_caches()
    with pytest.raises(RuntimeError) as exc:
        get_settings()
    assert "BQ_MCP_ENVIRONMENTS" in str(exc.value)


def test_an_environment_without_a_project_is_rejected_by_name(monkeypatch):
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps({"broken": {"location": "EU"}}))
    clear_caches()
    with pytest.raises(RuntimeError) as exc:
        get_settings()
    assert "broken" in str(exc.value) and "project" in str(exc.value)


# --- the TOML config file ---------------------------------------------------


def test_a_config_file_registers_environments(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'default_environment = "central"\n'
        "[environments.warehouse]\n"
        'project = "from-file"\n'
        "[environments.central]\n"
        'project = "from-file"\n'
        'location = "us-central1"\n'
        'dataset_allowlist = ["raw"]\n'
    )
    monkeypatch.setenv("BQ_MCP_CONFIG", str(path))
    clear_caches()

    assert get_settings().default_environment == "central"
    assert resolve_environment("central").dataset_allowlist == {"raw"}
    assert resolve_environment("warehouse").location == "US"


def test_the_environment_variable_wins_over_the_config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[environments.from_file]\nproject = "f"\n')
    monkeypatch.setenv("BQ_MCP_CONFIG", str(path))
    monkeypatch.setenv("BQ_MCP_ENVIRONMENTS", json.dumps({"from_env": {"project": "e"}}))
    clear_caches()
    assert [e.name for e in get_settings().environments] == ["from_env"]


def test_a_malformed_config_file_names_the_path(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not = = toml")
    monkeypatch.setenv("BQ_MCP_CONFIG", str(path))
    clear_caches()
    with pytest.raises(RuntimeError) as exc:
        get_settings()
    assert str(path) in str(exc.value)


def test_a_missing_config_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BQ_MCP_CONFIG", str(tmp_path / "absent.toml"))
    clear_caches()
    assert resolve_environment().project == "test-project"


# --- reporting --------------------------------------------------------------


def test_describe_environments_marks_the_default_and_names_projects(registry):
    summary = config.describe_environments()
    assert "warehouse (default): project demo-warehouse" in summary
    assert "production: project demo-prod, location us-central1" in summary


def test_the_effective_limits_are_reportable(registry):
    lines = "\n".join(config.config_summary_lines(resolve_environment("production")))
    assert "512 B" in lines          # this environment's own warn_bytes
    assert "sales" in lines          # its allowlist
