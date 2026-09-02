"""doctor's report, including the failures it exists to catch early.

Every check here corresponds to a real setup mistake that otherwise surfaces as
an opaque error at the first tool call, mid-conversation. The assertions are on
the *fix* being printed, not the wording around it -- a diagnostic that says
"failed" without saying what to do is the thing this replaces.
"""

from __future__ import annotations

import pytest
from google.api_core import exceptions as api_exceptions

from conftest import FakeQueryJob, clear_caches
from data_platform_mcp import diagnostics


class FakeDataset:
    def __init__(self, location):
        self.location = location


@pytest.fixture
def doctor(monkeypatch, fake_client):
    """Point diagnostics at the fake client and a healthy default project."""
    monkeypatch.setattr(diagnostics, "_check_credentials", lambda: True)
    monkeypatch.setattr(
        "data_platform_mcp.clients.get_bigquery_client", lambda _s: fake_client
    )
    fake_client.job = FakeQueryJob(rows=[{"ok": 1}])
    fake_client.datasets = ("sales", "marketing")
    fake_client.locations = {}

    def get_dataset(name):
        return FakeDataset(fake_client.locations.get(name, "US"))

    fake_client.get_dataset = get_dataset
    return fake_client


def report(capsys):
    return capsys.readouterr().out


# Assert on the bracketed status marker, never the bare word: "FAIL" also
# appears in the epilogue ("items marked FAIL above"), "warn" in the config
# summary ("warn above: 1.00 GiB") and "skip" in the audit-log line. Each of
# those made an assertion pass for the wrong reason.
FAILED = f"[{diagnostics.FAIL}]"
WARNED = f"[{diagnostics.WARN}]"
SKIPPED = f"[{diagnostics.SKIP}]"


def test_a_healthy_setup_passes(doctor, capsys):
    assert diagnostics.run_doctor() == 0
    out = report(capsys)
    assert "All checks passed." in out
    assert FAILED not in out and WARNED not in out


def test_the_effective_limits_are_printed(doctor, monkeypatch, capsys):
    """Someone debugging "why was my query refused" needs the caps actually in
    force, not the documented defaults."""
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED", str(2 * 1024**3))
    clear_caches()
    diagnostics.run_doctor()
    assert "hard cap:        2.00 GiB" in report(capsys)


def test_a_missing_project_fails_with_the_variable_to_set(monkeypatch, capsys):
    monkeypatch.delenv("BQ_PROJECT")
    monkeypatch.setattr("data_platform_mcp.config._resolve_project", lambda: "")
    clear_caches()

    assert diagnostics.run_doctor() == 1
    out = report(capsys)
    assert FAILED in out and "BQ_PROJECT" in out


def test_missing_credentials_name_the_login_command(monkeypatch, capsys):
    monkeypatch.setattr("google.auth.default", lambda **kw: (_ for _ in ()).throw(
        RuntimeError("could not find credentials")))

    assert diagnostics.run_doctor() == 1
    out = report(capsys)
    assert "gcloud auth application-default login" in out
    # The BigQuery checks would all fail for the same reason; a wall of
    # identical failures hides the one that matters.
    assert f"{SKIPPED} BigQuery checks" in out


def test_no_job_permission_names_the_role(doctor, capsys):
    doctor.job = api_exceptions.Forbidden("denied")

    def query(sql, job_config=None):
        raise api_exceptions.Forbidden("denied")

    doctor.query = query
    assert diagnostics.run_doctor() == 1
    out = report(capsys)
    assert "roles/bigquery.jobUser" in out


def test_an_allowlisted_dataset_that_does_not_exist_is_a_failure(
    doctor, monkeypatch, capsys
):
    """Every tool call for it would be refused, so this must not pass quietly."""
    monkeypatch.setenv("BQ_DATASET_ALLOWLIST", "sales,typo_dataset")
    clear_caches()

    assert diagnostics.run_doctor() == 1
    out = report(capsys)
    assert "typo_dataset" in out and FAILED in out


def test_a_dataset_in_another_region_warns_and_names_it(doctor, capsys):
    """The trap: BigQuery's own error names neither location, so it reads as a
    missing table."""
    doctor.locations = {"marketing": "us-central1"}

    # Not fatal on its own -- the dataset is simply unreachable from here.
    assert diagnostics.run_doctor() == 0
    out = report(capsys)
    assert WARNED in out
    assert "US-CENTRAL1: marketing" in out
    assert "reads as a missing table" in out


def test_an_allowlisted_dataset_in_another_region_is_fatal(
    doctor, monkeypatch, capsys
):
    """On the allowlist it stops being a warning: no tool call can ever read
    it, so the configuration is simply wrong."""
    monkeypatch.setenv("BQ_DATASET_ALLOWLIST", "marketing")
    clear_caches()
    doctor.locations = {"marketing": "us-central1"}

    assert diagnostics.run_doctor() == 1
    out = report(capsys)
    assert FAILED in out
    assert "can never be read: marketing" in out


def test_the_location_check_is_complete_not_sampled(doctor, capsys):
    """It was sampled at 25 while serial, which reported 2 of 6 offenders as
    though they were all of them."""
    doctor.datasets = tuple(f"ds{i:03d}" for i in range(60))
    doctor.locations = {"ds050": "EU", "ds059": "EU"}

    diagnostics.run_doctor()
    out = report(capsys)
    assert "ds050" in out and "ds059" in out
    assert "2 of 60 datasets are outside" in out


def test_a_disabled_audit_log_is_reported_not_hidden(doctor, monkeypatch, capsys):
    monkeypatch.setenv("BQ_MCP_AUDIT_LOG", "off")
    diagnostics.run_doctor()
    assert "audit log disabled" in report(capsys)


def test_an_unwritable_audit_log_warns_without_failing(doctor, monkeypatch,
                                                       tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("BQ_MCP_AUDIT_LOG", str(blocker / "audit.jsonl"))

    # Losing the audit trail must not be reported as an inability to work.
    assert diagnostics.run_doctor() == 0
    assert "audit log not writable" in report(capsys)
