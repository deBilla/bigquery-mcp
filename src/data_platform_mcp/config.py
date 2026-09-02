"""Runtime settings: a registry of named BigQuery environments.

One server process answers questions about several targets -- a warehouse and a
staging copy, or two regions of the same project -- so every tool takes an
optional ``environment`` argument that is resolved here. Omitting it uses the
configured default.

Configuration comes from, in order of precedence:

1. ``BQ_MCP_ENVIRONMENTS``, a JSON object mapping name to settings.
2. A TOML file at ``~/.config/data-platform-mcp/config.toml`` (or wherever
   ``BQ_MCP_CONFIG`` points), which is far kinder than a JSON blob squeezed
   into an environment variable and can be committed and shared by a team.
3. The original single-project variables (``BQ_PROJECT`` and friends), exposed
   as one environment named ``default``. An existing setup keeps working
   unchanged, which is the point.

Two properties matter more than the values:

**Nothing is read at import time.** Configuration used to be read at module
scope, so an unset project killed the process before it could answer
``initialize`` -- which a client reports only as "server failed to start", with
no tools and no message the user can act on.

**An unknown environment name is an error, never a fallback.** Silently
resolving a typo to the default would answer a question about one warehouse
from another, which no reader of the answer could detect.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .formatting import human_bytes

GIB = 1024**3

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "data-platform-mcp" / "config.toml"

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

DEFAULT_LOCATION = "US"

_PROJECT_ENV_VARS = ("BQ_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT")

# Spoken shorthands an agent picks up from a prompt ("check prod"). Listed in
# both directions so an environment can be named either way, and only applied
# when they do not collide with a real environment name.
_BUILTIN_ALIASES = {
    "prod": "production",
    "prd": "production",
    "live": "production",
    "production": "prod",
    "stg": "staging",
    "stage": "staging",
    "staging": "stage",
    "dev": "development",
    "development": "dev",
    "test": "testing",
    "testing": "test",
}

# With no explicit default, prefer a target where a mistake is cheap over one
# where it is not, rather than whichever key happened to be listed first.
_PREFERRED_DEFAULTS = ("staging", "development", "dev", "test", "testing")


@dataclass(frozen=True)
class Environment:
    """One resolvable BigQuery target and the limits applied to it."""

    name: str
    project: str
    location: str = DEFAULT_LOCATION
    impersonate: str = ""
    dataset_allowlist: frozenset[str] = frozenset()
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED
    warn_bytes: int = DEFAULT_WARN_BYTES
    row_limit: int = DEFAULT_ROW_LIMIT
    cost_per_tib_usd: float = DEFAULT_COST_PER_TIB_USD
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def dataset_allowed(self, dataset_id: str) -> bool:
        return not self.dataset_allowlist or dataset_id in self.dataset_allowlist


@dataclass(frozen=True)
class Settings:
    environments: tuple[Environment, ...]
    default_environment: str


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


def _as_allowlist(value) -> frozenset[str]:
    """Accept either a comma-separated string or a list of dataset ids."""
    if not value:
        return frozenset()
    if isinstance(value, str):
        value = value.split(",")
    return frozenset(str(v).strip() for v in value if str(v).strip())


def _adc_project() -> str:
    try:
        import google.auth

        _, project = google.auth.default()
        return str(project) if project else ""
    except Exception:
        return ""


def _resolve_project() -> str:
    for var in _PROJECT_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    # Fall back to the project associated with Application Default Credentials,
    # so a plain `gcloud auth application-default login` is enough to start.
    return _adc_project()


def _positive_int(spec: dict, key: str, default: int) -> int:
    try:
        value = int(spec.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _build_environment(name: str, spec, defaults: dict) -> Environment:
    """Turn one registry entry into an Environment.

    A bare string is accepted as a project id, because ``{"prod": "my-project"}``
    is what people write first and there is no reason to reject it.
    """
    key = str(name).strip().lower()
    if isinstance(spec, str):
        spec = {"project": spec}
    if not isinstance(spec, dict):
        raise RuntimeError(
            f"Environment '{name}' must be an object or a project-id string."
        )

    project = str(spec.get("project", "")).strip()
    if not project:
        raise RuntimeError(f"Environment '{name}' is missing a 'project'.")

    aliases = spec.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]

    try:
        cost = float(spec.get("cost_per_tib_usd", defaults["cost_per_tib_usd"]))
    except (TypeError, ValueError):
        cost = defaults["cost_per_tib_usd"]

    return Environment(
        name=key,
        project=project,
        location=str(spec.get("location") or defaults["location"]).strip(),
        impersonate=str(spec.get("impersonate") or defaults["impersonate"]).strip(),
        dataset_allowlist=(
            _as_allowlist(spec["dataset_allowlist"])
            if "dataset_allowlist" in spec
            else defaults["dataset_allowlist"]
        ),
        max_bytes_billed=_positive_int(
            spec, "max_bytes_billed", defaults["max_bytes_billed"]
        ),
        warn_bytes=_positive_int(spec, "warn_bytes", defaults["warn_bytes"]),
        row_limit=_positive_int(spec, "row_limit", defaults["row_limit"]),
        cost_per_tib_usd=cost if cost > 0 else defaults["cost_per_tib_usd"],
        aliases=tuple(str(a).strip().lower() for a in aliases if str(a).strip()),
    )


def _global_defaults() -> dict:
    """Values an environment inherits when it does not set its own.

    These are the original single-project variables, which keeps a pre-existing
    configuration working when environments are introduced around it.
    """
    return {
        "location": os.environ.get("BQ_LOCATION", "").strip() or DEFAULT_LOCATION,
        "impersonate": os.environ.get("BQ_IMPERSONATE_SERVICE_ACCOUNT", "").strip(),
        "dataset_allowlist": _as_allowlist(os.environ.get("BQ_DATASET_ALLOWLIST", "")),
        "max_bytes_billed": _env_int("BQ_MAX_BYTES_BILLED", DEFAULT_MAX_BYTES_BILLED),
        "warn_bytes": _env_int("BQ_WARN_BYTES", DEFAULT_WARN_BYTES),
        "row_limit": _env_int("BQ_ROW_LIMIT", DEFAULT_ROW_LIMIT),
        "cost_per_tib_usd": _env_float("BQ_COST_PER_TIB_USD", DEFAULT_COST_PER_TIB_USD),
    }


def _parse_registry(table: dict) -> tuple[Environment, ...]:
    if not isinstance(table, dict) or not table:
        raise RuntimeError(
            "The environment registry must be a non-empty mapping of name -> "
            'settings, e.g. {"staging": {"project": "..."}}'
        )
    defaults = _global_defaults()
    environments = [
        _build_environment(name, spec, defaults)
        for name, spec in table.items()
        if str(name).strip()
    ]
    if not environments:
        raise RuntimeError("The environment registry defined no usable environments.")
    return tuple(environments)


def config_path() -> Path:
    """Path of the config file, whether or not it exists."""
    override = os.environ.get("BQ_MCP_CONFIG", "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _load_config_file() -> dict:
    path = config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"Could not read config file {path}: {exc}") from exc
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Config file {path} is not valid TOML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Config file {path} must define a TOML table.")
    return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    file_config = _load_config_file()

    raw_registry = os.environ.get("BQ_MCP_ENVIRONMENTS", "").strip()
    if raw_registry:
        try:
            parsed = json.loads(raw_registry)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"BQ_MCP_ENVIRONMENTS is not valid JSON: {exc}"
            ) from exc
        environments = _parse_registry(parsed)
    elif file_config.get("environments"):
        table = file_config["environments"]
        if not isinstance(table, dict):
            raise RuntimeError(
                "The [environments] section of the config file must be a table "
                "of named environments, e.g. [environments.warehouse]."
            )
        environments = _parse_registry(table)
    else:
        # Single-environment mode: exactly the previous behaviour.
        defaults = _global_defaults()
        environments = (
            Environment(
                name="default",
                project=_resolve_project(),
                location=defaults["location"],
                impersonate=defaults["impersonate"],
                dataset_allowlist=defaults["dataset_allowlist"],
                max_bytes_billed=defaults["max_bytes_billed"],
                warn_bytes=defaults["warn_bytes"],
                row_limit=defaults["row_limit"],
                cost_per_tib_usd=defaults["cost_per_tib_usd"],
            ),
        )

    requested = os.environ.get("BQ_MCP_DEFAULT_ENVIRONMENT", "").strip().lower()
    if not requested:
        requested = str(file_config.get("default_environment", "")).strip().lower()

    known = {e.name for e in environments}
    if requested and requested not in known:
        raise RuntimeError(
            f"BQ_MCP_DEFAULT_ENVIRONMENT='{requested}' is not one of the "
            f"configured environments: {sorted(known)}"
        )
    if requested:
        default_environment = requested
    else:
        default_environment = next(
            (name for name in _PREFERRED_DEFAULTS if name in known),
            environments[0].name,
        )

    return Settings(
        environments=environments, default_environment=default_environment
    )


@lru_cache(maxsize=1)
def _lookup_table() -> dict[str, Environment]:
    """Every accepted spelling of an environment, mapped to it."""
    table: dict[str, Environment] = {}
    for env in get_settings().environments:
        table[env.name] = env
        for alias in env.aliases:
            table.setdefault(alias, env)
        # Let the agent name the project id directly.
        if env.project:
            table.setdefault(env.project.lower(), env)
    for alias, canonical in _BUILTIN_ALIASES.items():
        if canonical in table:
            table.setdefault(alias, table[canonical])
    return table


def list_environments() -> tuple[Environment, ...]:
    return get_settings().environments


def resolve_environment(name: str = "") -> Environment:
    """Resolve a name, alias or project id to an Environment.

    An empty name yields the default. An unknown one raises a ValueError naming
    the valid options rather than falling back, so a typo can never send a
    question about one warehouse to another.
    """
    settings = get_settings()
    key = (name or "").strip().lower()
    table = _lookup_table()

    if not key:
        env = table.get(settings.default_environment)
        if env is None:  # pragma: no cover - guarded by get_settings()
            raise RuntimeError("No environments are configured.")
        return env

    env = table.get(key)
    if env is None:
        valid = sorted({e.name for e in settings.environments})
        raise ValueError(
            f"Unknown environment '{name}'. Configured environments: "
            f"{', '.join(valid)}."
        )
    return env


def require_environment(name: str = "") -> Environment:
    """Resolve an environment and insist it has a usable project.

    Called at the top of every tool rather than at import, so the failure is a
    message the agent can relay instead of a dead process.
    """
    env = resolve_environment(name)
    if not env.project:
        raise RuntimeError(
            f"Environment '{env.name}' has no BigQuery project configured. Ask "
            "the user to set BQ_PROJECT to the GCP project whose datasets they "
            "want to query, e.g.\n"
            "    BQ_PROJECT=my-gcp-project\n"
            "Alternatively, `gcloud auth application-default set-quota-project "
            "PROJECT_ID` associates a project with their credentials."
        )
    return env


def describe_environments() -> str:
    """One line per environment, for the server instructions."""
    try:
        settings = get_settings()
    except Exception:
        return ""
    lines = []
    for env in settings.environments:
        if not env.project:
            continue
        marker = " (default)" if env.name == settings.default_environment else ""
        detail = f"project {env.project}, location {env.location}"
        if env.dataset_allowlist:
            detail += f", datasets {', '.join(sorted(env.dataset_allowlist))}"
        lines.append(f"- {env.name}{marker}: {detail}")
    return "\n".join(lines)


def config_summary_lines(env: Environment) -> list[str]:
    """The effective limits for one environment, for `doctor` to print.

    Reporting these matters because they are silently overridable: someone
    debugging "why did that query get refused" needs the caps actually in
    force, not the documented defaults.
    """
    lines = [
        f"warn above:      {human_bytes(env.warn_bytes)}",
        f"hard cap:        {human_bytes(env.max_bytes_billed)}",
        f"default rows:    {env.row_limit}",
        f"price per TiB:   ${env.cost_per_tib_usd}",
        f"impersonate:     {env.impersonate or '(none — your own credentials)'}",
    ]
    if env.dataset_allowlist:
        lines.append("allowlist:       " + ", ".join(sorted(env.dataset_allowlist)))
    else:
        lines.append("allowlist:       (none — all datasets readable)")
    return lines
