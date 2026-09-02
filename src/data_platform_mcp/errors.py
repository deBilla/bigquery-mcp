"""Turn opaque Google auth and BigQuery failures into instructions to relay.

The client reading these messages is a language model, so an error naming the
environment, the identity and the exact command that fixes it gets passed on to
the user as a fix. "Your default credentials were not found" does not.

Each message names the environment, because with several configured, "permission
denied" without one leaves the reader unsure which warehouse it was about.
"""

from __future__ import annotations


class DataPlatformMCPError(RuntimeError):
    """An error whose message is written to be read by an agent and a human."""


def _environment_hint(environment: str) -> tuple[str, str]:
    """Best-effort (label, impersonated service account) for the target in play."""
    try:
        from .config import resolve_environment

        env = resolve_environment(environment)
        return f"environment '{env.name}' (project {env.project})", env.impersonate
    except Exception:
        return "the requested environment", ""


def explain_exception(exc: Exception, environment: str = "") -> Exception:
    """Return a clearer error for known failure shapes, or the original."""
    try:
        from google.auth import exceptions as auth_exceptions
    except ImportError:  # pragma: no cover - google-auth is a hard dependency
        return exc

    label, service_account = _environment_hint(environment)
    detail = str(exc)

    if isinstance(exc, auth_exceptions.DefaultCredentialsError):
        # This fires both when a login has expired and when the SDK was never
        # installed, and the two look identical from here. Naming the second
        # possibility first saves the user chasing a login command that is not
        # on their PATH -- the failure that actually happened.
        return DataPlatformMCPError(
            f"{label}: no Application Default Credentials were found. Ask the "
            "user to check, in order:\n"
            "  1. Is the SDK installed?  gcloud --version\n"
            "     If not: https://cloud.google.com/sdk/docs/install\n"
            "  2. Are they logged in?    gcloud auth application-default login\n"
            "  3. Is a quota project set?\n"
            "     gcloud auth application-default set-quota-project PROJECT_ID\n"
            "If they use a service-account key instead, GOOGLE_APPLICATION_"
            "CREDENTIALS must be set in the MCP client's config for this "
            "server, not exported in a shell -- the server is a subprocess and "
            "sees only what that config declares.\n"
            "`data-platform-mcp doctor` reports which of these is missing."
        )

    if isinstance(exc, auth_exceptions.RefreshError):
        # An impersonation denial arrives as a RefreshError wrapping a 403 from
        # the IAM Credentials API, which is a different fix from expiry: the
        # user needs a role on the service account, not a fresh login.
        if service_account and (
            "iam.serviceAccounts.getAccessToken" in detail
            or "403" in detail
            or "Permission" in detail
        ):
            return DataPlatformMCPError(
                f"{label}: could not impersonate the read-only service account "
                f"'{service_account}'. The signed-in user needs the Service "
                "Account Token Creator role on it. Ask an admin to run:\n"
                f"    gcloud iam service-accounts add-iam-policy-binding {service_account} \\\n"
                "      --member=user:USER_EMAIL \\\n"
                "      --role=roles/iam.serviceAccountTokenCreator\n"
                f"Underlying error: {detail[:300]}"
            )
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
        identity = service_account or "the signed-in user"
        return DataPlatformMCPError(
            f"{label}: permission denied. {identity} needs "
            "roles/bigquery.jobUser on the project to run a query, and "
            "roles/bigquery.dataViewer on the dataset to read a table -- and "
            "the dataset is often in a different project from the one the job "
            f"runs in.\nUnderlying error: {detail[:300]}"
        )

    if isinstance(exc, api_exceptions.NotFound):
        return DataPlatformMCPError(
            f"{label}: the dataset or table does not exist. Check the name with "
            "list_datasets / list_tables; note that a table in another region "
            "cannot be read from this one, which BigQuery also reports as not "
            f"found.\nUnderlying error: {detail[:300]}"
        )

    return exc
