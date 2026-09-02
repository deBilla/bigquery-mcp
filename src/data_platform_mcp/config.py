"""Runtime settings for the data-platform-mcp server.

Everything the server needs to reach BigQuery is resolved here, once, and
cached. Two properties matter more than the values themselves:

**Nothing is read at import time.** The previous single-file server raised at
module scope when ``BQ_PROJECT`` was unset, which killed the process before it
could answer ``initialize``. An MCP client shows that as "server failed to
start" with no tools and no message anywhere the user will look. Settings are
resolved lazily instead, so the server always starts and a missing project
surfaces as a tool-call error the agent can read and relay.

**The byte caps are guardrails on this tool, not BigQuery limits.** A query too
large for ``max_bytes_billed`` is not a query BigQuery refuses; it is one this
server declines to run on an analyst's behalf. The error text says so, because
the alternative -- reaching for the Python client directly -- is a perfectly
good answer that people otherwise waste time rediscovering.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from .formatting import human_bytes

GIB = 1024**3

# Hard cap on bytes scanned per query. A query estimated above this never runs,
# even with confirmation.
DEFAULT_MAX_BYTES_BILLED = 5 * GIB

# Soft "this is costly" threshold. Above it, run_query reports the estimate and
# asks the caller to come back with confirm_expensive=True.
DEFAULT_WARN_BYTES = 1 * GIB

# On-demand price used only to render a friendly cost estimate. BigQuery US
# on-demand analysis is ~$6.25 per TiB scanned.
DEFAULT_COST_PER_TIB_USD = 6.25

# Default row cap returned to the model so responses stay small.
DEFAULT_ROW_LIMIT = 200

_PROJECT_ENV_VARS = ("BQ_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT")


@dataclass(frozen=True)
class Settings:
    """One resolvable BigQuery target and the limits applied to it."""

    project: str
    location: str = "US"
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED
    warn_bytes: int = DEFAULT_WARN_BYTES
    cost_per_tib_usd: float = DEFAULT_COST_PER_TIB_USD
    row_limit: int = DEFAULT_ROW_LIMIT
    dataset_allowlist: frozenset[str] = frozenset()

    def dataset_allowed(self, dataset_id: str) -> bool:
        return not self.dataset_allowlist or dataset_id in self.dataset_allowlist


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, ignoring unusable values.

    A typo in a limit must not stop the server from starting; falling back to
    the documented default and carrying on is the safer failure.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _resolve_project() -> str:
    for var in _PROJECT_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    # Fall back to the project associated with Application Default Credentials,
    # so a plain `gcloud auth application-default login` is enough to start.
    try:
        import google.auth

        _, project = google.auth.default()
        return str(project) if project else ""
    except Exception:
        return ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    allowlist = os.environ.get("BQ_DATASET_ALLOWLIST", "")
    return Settings(
        project=_resolve_project(),
        location=os.environ.get("BQ_LOCATION", "US").strip() or "US",
        max_bytes_billed=_env_int("BQ_MAX_BYTES_BILLED", DEFAULT_MAX_BYTES_BILLED),
        warn_bytes=_env_int("BQ_WARN_BYTES", DEFAULT_WARN_BYTES),
        cost_per_tib_usd=_env_float("BQ_COST_PER_TIB_USD", DEFAULT_COST_PER_TIB_USD),
        row_limit=_env_int("BQ_ROW_LIMIT", DEFAULT_ROW_LIMIT),
        dataset_allowlist=frozenset(
            d.strip() for d in allowlist.split(",") if d.strip()
        ),
    )


def require_settings() -> Settings:
    """Settings with a usable project, or an error naming how to supply one.

    Called at the top of every tool rather than at import, so the failure is a
    message the agent can pass on instead of a dead process.
    """
    settings = get_settings()
    if not settings.project:
        raise RuntimeError(
            "No BigQuery project is configured. Ask the user to set BQ_PROJECT "
            "to the GCP project whose datasets they want to query, e.g.\n"
            "    BQ_PROJECT=my-gcp-project\n"
            "Alternatively, `gcloud auth application-default set-quota-project "
            "PROJECT_ID` associates a project with their credentials."
        )
    return settings


def describe_settings() -> str:
    """One-line summary used in the server instructions and by --version."""
    settings = get_settings()
    if not settings.project:
        return ""
    parts = [f"project {settings.project} ({settings.location})"]
    if settings.dataset_allowlist:
        parts.append("datasets " + ", ".join(sorted(settings.dataset_allowlist)))
    return "; ".join(parts)


def config_summary_lines(settings: Settings | None = None) -> list[str]:
    """The effective limits, for `doctor` to print.

    Reporting these matters because they are silently overridable by the
    environment: someone debugging "why did that query get refused" needs to
    see the caps actually in force, not the documented defaults.
    """
    settings = settings or get_settings()
    lines = [
        f"warn above:      {human_bytes(settings.warn_bytes)}",
        f"hard cap:        {human_bytes(settings.max_bytes_billed)}",
        f"default rows:    {settings.row_limit}",
        f"price per TiB:   ${settings.cost_per_tib_usd}",
    ]
    if settings.dataset_allowlist:
        lines.append("allowlist:       " + ", ".join(sorted(settings.dataset_allowlist)))
    else:
        lines.append("allowlist:       (none — all datasets readable)")
    return lines
