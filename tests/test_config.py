"""Settings resolution, and the failure that used to kill the process."""

from __future__ import annotations

import pytest

from data_platform_mcp import config
from data_platform_mcp.config import get_settings, require_settings

from conftest import clear_caches


def test_defaults_are_the_documented_ones():
    s = get_settings()
    assert s.project == "test-project"
    assert s.location == "US"
    assert s.max_bytes_billed == 5 * 1024**3
    assert s.warn_bytes == 1 * 1024**3
    assert s.row_limit == 200
    assert s.dataset_allowlist == frozenset()


def test_env_overrides_are_read(monkeypatch):
    monkeypatch.setenv("BQ_LOCATION", "EU")
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED", "2048")
    monkeypatch.setenv("BQ_ROW_LIMIT", "5")
    clear_caches()
    s = get_settings()
    assert (s.location, s.max_bytes_billed, s.row_limit) == ("EU", 2048, 5)


@pytest.mark.parametrize("bad", ["not-a-number", "-1", "0", ""])
def test_an_unusable_limit_falls_back_instead_of_failing(monkeypatch, bad):
    # A typo in a limit must never stop the server from starting.
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED", bad)
    clear_caches()
    assert get_settings().max_bytes_billed == 5 * 1024**3


def test_allowlist_is_parsed_and_enforced(monkeypatch):
    monkeypatch.setenv("BQ_DATASET_ALLOWLIST", " sales , marketing ,, ")
    clear_caches()
    s = get_settings()
    assert s.dataset_allowlist == {"sales", "marketing"}
    assert s.dataset_allowed("sales")
    assert not s.dataset_allowed("payroll")


def test_an_empty_allowlist_permits_everything():
    assert get_settings().dataset_allowed("anything-at-all")


def test_project_falls_back_through_the_documented_variables(monkeypatch):
    monkeypatch.delenv("BQ_PROJECT")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-gcloud")
    clear_caches()
    assert get_settings().project == "from-gcloud"


def test_a_missing_project_is_a_readable_error_not_an_import_crash(monkeypatch):
    """The regression that motivated the restructure.

    Configuration used to be read at module scope, so an unset project killed
    the process before it could answer `initialize` -- which a client reports
    only as "server failed to start", with no tools and no message. It must now
    be a raised error, carrying the command that fixes it.
    """
    monkeypatch.delenv("BQ_PROJECT")
    monkeypatch.setattr(config, "_resolve_project", lambda: "")
    clear_caches()

    assert get_settings().project == ""  # resolving is fine; it just finds nothing
    with pytest.raises(RuntimeError) as exc:
        require_settings()
    assert "BQ_PROJECT" in str(exc.value)
    assert "set-quota-project" in str(exc.value)


def test_describe_settings_is_empty_when_unconfigured(monkeypatch):
    # The server puts this in its instructions; an empty string means "say
    # nothing", never "say project None".
    monkeypatch.setattr(config, "_resolve_project", lambda: "")
    clear_caches()
    assert config.describe_settings() == ""


def test_describe_settings_names_the_target_and_allowlist(monkeypatch):
    monkeypatch.setenv("BQ_DATASET_ALLOWLIST", "sales")
    clear_caches()
    summary = config.describe_settings()
    assert "test-project" in summary and "sales" in summary
