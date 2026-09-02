"""Turn opaque Google auth and BigQuery failures into instructions to relay.

The client reading these messages is a language model, so an error naming the
identity, the project and the exact command that fixes it gets passed on to the
user as a fix. "Your default credentials were not found" does not.
"""

from __future__ import annotations


class DataPlatformMCPError(RuntimeError):
    """An error whose message is written to be read by an agent and a human."""


def _project_label() -> str:
    try:
        from .config import get_settings

        project = get_settings().project
        return f"project {project}" if project else "the configured project"
    except Exception:
        return "the configured project"


def explain_exception(exc: Exception) -> Exception:
    """Return a clearer error for known failure shapes, or the original."""
    try:
        from google.auth import exceptions as auth_exceptions
    except ImportError:  # pragma: no cover - google-auth is a hard dependency
        return exc

    label = _project_label()
    detail = str(exc)

    if isinstance(exc, auth_exceptions.DefaultCredentialsError):
        return DataPlatformMCPError(
            f"{label}: no Application Default Credentials were found. "
            "Ask the user to run:\n"
            "    gcloud auth application-default login\n"
            "Then retry. If they use a service-account key instead, set "
            "GOOGLE_APPLICATION_CREDENTIALS to its path."
        )

    if isinstance(exc, auth_exceptions.RefreshError):
        return DataPlatformMCPError(
            f"{label}: the stored credentials could not be refreshed, usually "
            "because they expired or were revoked. Ask the user to run:\n"
            "    gcloud auth application-default login"
        )

    try:
        from google.api_core import exceptions as api_exceptions
    except ImportError:  # pragma: no cover
        return exc

    # PermissionDenied (gRPC) subclasses Forbidden (HTTP 403), so this covers both.
    if isinstance(exc, api_exceptions.Forbidden):
        return DataPlatformMCPError(
            f"{label}: permission denied. Running a query needs "
            "roles/bigquery.jobUser on the project, and reading a table needs "
            "roles/bigquery.dataViewer on its dataset -- which is often in a "
            "different project from the one the job runs in.\n"
            f"Underlying error: {detail[:300]}"
        )

    if isinstance(exc, api_exceptions.NotFound):
        return DataPlatformMCPError(
            f"{label}: the dataset or table does not exist. Check the name with "
            "list_datasets / list_tables; note that a table in another region "
            "cannot be read from this one.\n"
            f"Underlying error: {detail[:300]}"
        )

    return exc
