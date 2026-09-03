"""``data-platform-mcp doctor``: check that this machine can actually query.

Every prerequisite here fails at a different layer -- credentials, job
permission, dataset visibility, region -- and each one produces a different
opaque error at the first tool call, mid-conversation, once someone has already
asked a question. Checking them up front with the fix printed next to the
failure is the difference between onboarding in a minute and onboarding in a
support thread.

The region check earns its place. BigQuery cannot query a dataset from a
different location, and the resulting error names neither the location it
wanted nor the one the dataset is in. A project whose datasets are split across
``US`` and ``us-central1`` looks perfectly healthy right up to the query that
returns "not found" for a table sitting in plain sight.

This runs as a CLI command, not over the protocol, so printing to stdout here is
safe. The server itself must never do that.
"""

from __future__ import annotations

import sys

from .config import (
    Environment,
    config_path,
    config_summary_lines,
    get_settings,
)

OK = "  ok  "
FAIL = " FAIL "
WARN = " warn "
SKIP = " skip "

# get_dataset is one API call each, and they run about 440ms apiece -- 17
# seconds for a 39-dataset project if done in sequence, which is slow enough
# that a sample looks like the only option. They are independent reads, so a
# small thread pool makes the check complete instead of partial in about two
# seconds. The ceiling only exists so a pathological project cannot hang the
# command, and the report says when it bites.
_LOCATION_LIMIT = 200
# Kept under the BigQuery client's default connection pool size of 10;
# above it the pool churns and logs a warning for every discarded
# connection, which buries the report this command exists to print.
_LOCATION_WORKERS = 8


def _line(status: str, message: str) -> None:
    print(f"[{status}] {message}")


def _fix(text: str) -> None:
    for line in text.strip().splitlines():
        print(f"         {line}")


def _check_credentials() -> bool:
    try:
        import google.auth

        credentials, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except Exception as exc:
        _line(FAIL, "Application Default Credentials")
        # A machine with no SDK at all produces the same exception as an
        # expired login, so the fix names both rather than sending someone to
        # a command that is not on their PATH.
        _fix(
            f"{str(exc)[:200]}\n"
            "Fix:  gcloud --version            # installed at all?\n"
            "      https://cloud.google.com/sdk/docs/install\n"
            "      gcloud auth application-default login\n"
            "Using a key file instead? GOOGLE_APPLICATION_CREDENTIALS must be "
            "set in the MCP client's config for this server, not exported in a "
            "shell."
        )
        return False
    _line(
        OK,
        f"Application Default Credentials ({type(credentials).__name__}, "
        f"quota project: {project or 'unset'})",
    )
    if not project:
        _fix(
            "No quota project is set. Some APIs bill quota to it and will fail "
            "without one.\n"
            "Fix:  gcloud auth application-default set-quota-project PROJECT_ID"
        )
    return True


def _check_environment_header(env: Environment, default: str) -> bool:
    marker = " (default)" if env.name == default else ""
    print(f"environment: {env.name}{marker} -> {env.project or '(no project)'}")
    if not env.project:
        _line(FAIL, "no BigQuery project configured")
        _fix(
            "Set BQ_PROJECT, or give this environment a 'project' in the "
            "config file.\n"
            "Fix:  BQ_PROJECT=my-gcp-project"
        )
        return False
    for line in config_summary_lines(env):
        print(f"         {line}")
    return True


def _check_impersonation(env: Environment) -> bool:
    """Prove the service account can actually be impersonated.

    This is the check that makes read-only a property of the identity rather
    than a promise about the code, so a silent failure here means the server is
    quietly running as the operator instead.
    """
    if not env.impersonate:
        _line(SKIP, "impersonation not configured (using your own credentials)")
        return True

    from google.auth.transport.requests import Request

    from .clients import get_credentials

    try:
        credentials = get_credentials(env.impersonate)
        credentials.refresh(Request())
    except Exception as exc:
        _line(FAIL, f"impersonate {env.impersonate}")
        _fix(
            f"{str(exc)[:250]}\n"
            "Fix:  gcloud iam service-accounts add-iam-policy-binding \\\n"
            f"        {env.impersonate} \\\n"
            "        --member=user:YOUR_EMAIL \\\n"
            "        --role=roles/iam.serviceAccountTokenCreator"
        )
        return False
    _line(OK, f"impersonate {env.impersonate}")
    return True


def _check_can_run_a_query(env: Environment) -> bool:
    """One real query, so job permission is proven rather than assumed.

    ``SELECT 1`` reads no table and scans no bytes, so this costs nothing and
    still exercises the whole path: credentials, the job API, and the location.
    """
    from .clients import get_bigquery_client

    try:
        client = get_bigquery_client(env)
        list(client.query("SELECT 1 AS ok").result())
    except Exception as exc:
        _line(FAIL, f"run a query in {env.project}")
        _fix(
            f"{str(exc)[:250]}\n"
            f"Fix:  grant roles/bigquery.jobUser on {env.project}, and "
            "enable the BigQuery API."
        )
        return False
    _line(OK, f"run a query in {env.project} (location {env.location})")
    return True


def _check_datasets(env: Environment) -> tuple[bool, list[str]]:
    """List datasets, and confirm every allowlisted one actually exists."""
    from .clients import get_bigquery_client

    try:
        client = get_bigquery_client(env)
        names = [d.dataset_id for d in client.list_datasets(project=env.project)]
    except Exception as exc:
        _line(FAIL, f"list datasets in {env.project}")
        _fix(
            f"{str(exc)[:250]}\n"
            f"Fix:  grant roles/bigquery.metadataViewer (or dataViewer) on "
            f"{env.project}."
        )
        return False, []

    if not env.dataset_allowlist:
        _line(OK, f"{len(names)} datasets visible (no allowlist; all are readable)")
        return True, names

    missing = sorted(env.dataset_allowlist - set(names))
    if missing:
        _line(FAIL, f"allowlisted datasets not found: {', '.join(missing)}")
        _fix(
            "BQ_DATASET_ALLOWLIST names datasets that do not exist or are not "
            f"visible in {env.project}. Every tool call for these will be "
            "refused.\nFix:  correct the names, or grant read access to them."
        )
        return False, names
    _line(
        OK,
        f"{len(env.dataset_allowlist)} allowlisted dataset(s) exist "
        f"({len(names)} visible in total)",
    )
    return True, names


def _check_locations(env: Environment, names: list[str]) -> bool:
    """Find datasets this server's location cannot reach.

    A dataset in another region is not a misconfiguration on its own -- but one
    on the allowlist is, because no tool call can ever read it.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .clients import get_bigquery_client

    if not names:
        _line(SKIP, "dataset locations (no datasets to check)")
        return True

    client = get_bigquery_client(env)
    if env.dataset_allowlist:
        checking = sorted(env.dataset_allowlist)
    else:
        checking = sorted(names)
    capped = len(checking) > _LOCATION_LIMIT
    checking = checking[:_LOCATION_LIMIT]

    def location_of(name: str) -> tuple[str, str]:
        try:
            return name, (client.get_dataset(name).location or "").upper()
        except Exception:
            # Visibility is the previous check's job, not this one.
            return name, ""

    with ThreadPoolExecutor(max_workers=_LOCATION_WORKERS) as pool:
        found = list(pool.map(location_of, checking))

    want = env.location.upper()
    elsewhere: dict[str, list[str]] = {}
    for name, location in found:
        if location and location != want:
            elsewhere.setdefault(location, []).append(name)

    scope = f" (first {len(checking)} of {len(names)})" if capped else ""
    if not elsewhere:
        _line(OK, f"all {len(checking)} datasets are in {env.location}{scope}")
        return True

    blocked = sorted(env.dataset_allowlist.intersection(
        d for group in elsewhere.values() for d in group
    ))
    status = FAIL if blocked else WARN
    total = sum(len(group) for group in elsewhere.values())
    _line(
        status,
        f"{total} of {len(checking)} datasets are outside location "
        f"{env.location}{scope}",
    )
    for location, group in sorted(elsewhere.items()):
        _fix(f"{location}: {', '.join(sorted(group))}")
    _fix(
        f"BigQuery cannot query these from {env.location}, and cannot join "
        "them with datasets that are in it. The error at query time names "
        "neither location, so it reads as a missing table.\n"
        + (
            f"These are on the allowlist and can never be read: {', '.join(blocked)}\n"
            if blocked else ""
        )
        + "Fix:  set BQ_LOCATION to the region you need, and run a separate "
        "server for datasets in another one."
    )
    return not blocked


def _check_scheduled_queries(env: Environment) -> bool:
    """Scheduled queries need a role the querying tools do not.

    Never fatal: the server is fully usable without it, and reporting a missing
    optional permission as a failure would train people to ignore FAIL lines.
    """
    try:
        from .tools.transfer_tools import _client, _parent

        client = _client(env)
        configs = list(client.list_transfer_configs(parent=_parent(env)))
    except Exception as exc:
        _line(SKIP, "scheduled queries unavailable")
        _fix(
            f"{str(exc)[:160]}\n"
            "Optional. To enable list_scheduled_queries / get_scheduled_query:\n"
            f"      gcloud projects add-iam-policy-binding {env.project} \\\n"
            "        --member=MEMBER --role=roles/bigquerydatatransfer.viewer"
        )
        return True
    queries = [c for c in configs if c.data_source_id == "scheduled_query"]
    disabled = sum(1 for c in queries if c.disabled)
    _line(
        OK,
        f"{len(queries)} scheduled queries readable"
        + (f" ({disabled} disabled)" if disabled else ""),
    )
    return True


def _check_audit_log() -> bool:
    """The audit trail is the only record of what was asked; say if it is off."""
    from .observability import _audit_path

    path = _audit_path()
    if path is None:
        _line(SKIP, "audit log disabled (BQ_MCP_AUDIT_LOG=off)")
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        _line(WARN, f"audit log not writable: {path}")
        _fix(f"{exc}\nTool calls will still work; they just will not be recorded.")
        return True  # never fatal: losing the audit must not lose the answer
    _line(OK, f"audit log writable: {path}")
    return True


def run_doctor() -> int:
    """Print a per-environment readiness report. Returns a process exit code."""
    path = config_path()
    print("data-platform-mcp doctor")
    print(f"config file: {path}{'' if path.exists() else '  (not found)'}")
    print()

    try:
        settings = get_settings()
    except Exception as exc:
        _line(FAIL, "configuration")
        _fix(str(exc))
        return 1

    # Credentials are shared across environments, so a failure here would
    # repeat under every one of them and bury the difference between them.
    healthy = _check_credentials()
    print()

    for env in settings.environments:
        if not _check_environment_header(env, settings.default_environment):
            healthy = False
            print()
            continue
        if healthy:
            results = [
                _check_impersonation(env),
                _check_can_run_a_query(env),
            ]
            datasets_ok, names = _check_datasets(env)
            results.append(datasets_ok)
            results.append(_check_locations(env, names))
            results.append(_check_scheduled_queries(env))
            healthy = all(results) and healthy
        else:
            _line(SKIP, "BigQuery checks (no usable credentials)")
        print()

    _check_audit_log()

    print()
    if healthy:
        print("All checks passed.")
        return 0
    print("Some checks failed. Fix the items marked FAIL above, then re-run.")
    return 1


def main() -> None:  # pragma: no cover - thin CLI wrapper
    sys.exit(run_doctor())
