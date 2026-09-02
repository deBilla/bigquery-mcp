"""Auth failures must arrive as instructions, not as jargon.

The client reading these is a language model that will relay them to a person,
so each assertion checks for the actionable part -- the command to run or the
role to grant -- rather than the wording around it.
"""

from __future__ import annotations

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions

from data_platform_mcp.errors import DataPlatformMCPError, explain_exception


def test_missing_credentials_names_the_login_command():
    out = explain_exception(auth_exceptions.DefaultCredentialsError("boom"))
    assert isinstance(out, DataPlatformMCPError)
    assert "gcloud auth application-default login" in str(out)


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
