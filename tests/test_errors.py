"""Auth failures must arrive as instructions, not as jargon.

The client reading these is a language model that will relay them to a person,
so each assertion checks for the actionable part -- the command to run or the
role to grant -- rather than the wording around it.
"""

from __future__ import annotations

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions

from data_platform_mcp.errors import DataPlatformMCPError, explain_exception


def test_missing_credentials_names_the_login_command(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    out = explain_exception(auth_exceptions.DefaultCredentialsError("boom"))
    assert isinstance(out, DataPlatformMCPError)
    assert "gcloud auth application-default login" in str(out)


def test_missing_credentials_covers_the_sdk_not_being_installed(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    """A machine with no SDK raises the same exception as an expired login.

    Reported from the field: the message sent someone to run a login command
    that was not on their PATH, on a machine with no SDK at all.
    """
    out = str(explain_exception(auth_exceptions.DefaultCredentialsError("boom")))
    assert "gcloud --version" in out
    assert "cloud.google.com/sdk/docs/install" in out
    # ...and the check that would have told them which of the two it was.
    assert "doctor" in out


def test_a_bad_key_path_is_reported_as_a_key_path_problem(monkeypatch):
    """google-auth says "File X was not found", which is more actionable than
    a generic credentials checklist. Replacing it sends an analyst to install
    an SDK they do not need when the real fault is a path they can see."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/keys/typo.json")
    out = str(explain_exception(
        auth_exceptions.DefaultCredentialsError("File /keys/typo.json was not found.")
    ))
    assert "/keys/typo.json" in out
    assert "was not found" in out            # the underlying detail survives
    assert "not a missing login" in out
    assert "gcloud --version" not in out     # and the wrong advice is not given


def test_the_generic_checklist_is_used_when_no_key_file_is_configured(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    out = str(explain_exception(auth_exceptions.DefaultCredentialsError("boom")))
    assert "gcloud --version" in out


def test_key_file_guidance_says_where_the_variable_must_be_set(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    """Exporting it in a shell does nothing: the server is a subprocess that
    sees only the environment its client config declares. That failure looks
    identical to having no credentials at all."""
    out = str(explain_exception(auth_exceptions.DefaultCredentialsError("boom")))
    assert "GOOGLE_APPLICATION_CREDENTIALS" in out
    assert "not exported in a shell" in out


def test_expired_credentials_names_the_login_command():
    out = explain_exception(auth_exceptions.RefreshError("token expired"))
    assert isinstance(out, DataPlatformMCPError)
    assert "gcloud auth application-default login" in str(out)


def test_permission_denied_names_both_roles_a_query_needs():
    """jobUser and dataViewer fail identically and are fixed differently.

    The dataset is frequently in a different project from the one the job runs
    in, which is the part people miss.
    """
    out = explain_exception(api_exceptions.Forbidden("no"))
    assert isinstance(out, DataPlatformMCPError)
    text = str(out)
    assert "bigquery.jobUser" in text and "bigquery.dataViewer" in text
    assert "different project" in text


def test_grpc_permission_denied_is_covered_by_the_same_branch():
    # PermissionDenied subclasses Forbidden; this guards that assumption.
    out = explain_exception(api_exceptions.PermissionDenied("no"))
    assert isinstance(out, DataPlatformMCPError)
    assert "bigquery.dataViewer" in str(out)


def test_not_found_mentions_the_cross_region_trap():
    out = explain_exception(api_exceptions.NotFound("gone"))
    assert "another region" in str(out)


def test_the_underlying_error_is_kept_for_debugging():
    out = explain_exception(api_exceptions.Forbidden("the-original-detail"))
    assert "the-original-detail" in str(out)


def test_an_unrecognised_error_passes_through_untouched():
    original = ValueError("something else entirely")
    assert explain_exception(original) is original
