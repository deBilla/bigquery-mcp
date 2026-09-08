"""BigQuery Studio code assets: the Colab notebooks, saved queries and data
canvases people actually work in.

The naming here is a trap worth stating once. Google calls these "code assets";
users call them Colab notebooks, or just "colab scripts", because BigQuery
Studio notebooks *are* Colab Enterprise notebooks. The tools are named for the
storage concept because it covers saved queries too, so every docstring names
the user's word as well -- a tool nobody can find by the word they use is a
tool nobody uses.

``list_scheduled_queries`` answers "what writes this table". This answers the
question that comes before a schema change -- "what *reads* it" -- and the one
that comes after a stale-table finding: the logic was often never a scheduled
query at all, it was a saved query someone runs by hand.

These are not BigQuery resources. BigQuery Studio stores each code asset as a
Dataform repository holding exactly one file (``content.sql``,
``content.ipynb``, ``content.json``), which means a third API and a third
permission -- ``roles/dataform.viewer`` -- on top of BigQuery and the Data
Transfer Service. They are also invisible in the Dataform UI, so the console
gives no hint that this is where they live.

Two things about that storage drive the shape of everything below:

  - **Code assets are regional, and their region is not the dataset region.**
    On the platform this was built against, datasets are multi-region ``US``
    while every one of 616 code assets is in ``us-central1``. Worse, asking
    Dataform for ``US`` is a 403 and asking for the wrong *region* returns an
    empty list -- indistinguishable from "you have no notebooks". So the
    location is configured separately and always echoed back in the result.

  - **Reads are quota-limited by volume, not by concurrency.** Fetching all
    616 assets returned ResourceExhausted for 234 of them. The obvious reading
    -- too many threads -- is wrong: re-running 60 reads at 1, 2, 4 and 8
    threads immediately afterwards failed 8, 3, 0 and 0 times respectively.
    Failures fall as the run goes on and rise with how recently the quota was
    hit, which is a refilling token bucket, not a concurrency limit. So the
    mitigation is retry-with-backoff and a cap on how many assets one call may
    touch -- turning threads down would only make it slower, not safer.

Nothing here can create, edit or delete a code asset. The Dataform client
offers all three and they are simply not registered.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..config import require_environment
from ..errors import DataPlatformMCPError
from ..registration import register_tool

# A notebook is mostly its own output. Across 52 real notebooks, cell outputs
# were 77% of raw bytes -- 3.9 MB of file for 0.9 MB of source, and the largest
# single notebook was 1.44 MB of which 80 KB was code. Returning raw .ipynb
# would spend a context window on base64 PNGs of charts.
_STRIP_OUTPUT_KEYS = ("outputs", "execution_count")

# Same reasoning as transfer_tools: a listing never carries bodies.
_MAX_PAYLOAD_CHARS = 40_000

# The label BigQuery Studio stamps on each repository, and the single file each
# asset type stores its body in.
_TYPE_LABEL = "single-file-asset-type"
_CONTENT_FILE = {
    "sql": "content.sql",
    "notebook": "content.ipynb",
    "data_canvas": "content.json",
}

# Concurrency is not what the quota measures (see module docstring), so this is
# set for latency: 8 threads read 60 assets in 3.3s where 1 thread took 54.8s.
# The retry budget is what actually absorbs a depleted bucket.
_MAX_WORKERS = 8
_MAX_RETRIES = 4

# A search that quietly read 600 files would be a surprise on someone's quota.
_DEFAULT_SCAN_CAP = 250


def _client(env):
    try:
        from google.cloud import dataform_v1beta1
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise DataPlatformMCPError(
            "The Dataform library is not installed, so BigQuery Studio "
            "notebooks and saved queries cannot be read. Reinstall the server: "
            "pip install --upgrade data-platform-mcp"
        ) from exc

    from ..clients import get_credentials

    return dataform_v1beta1.DataformClient(credentials=get_credentials(env.impersonate))


# A regional location carries a digit (us-central1); a multi-region does not
# (US, EU). Dataform rejects multi-regions, so one cannot be used or guessed
# from -- it has to be discovered.
_REGIONAL = re.compile(r"\d")

# Tried first when discovering, because BigQuery Studio defaults new code
# assets here and it is where they are in practice.
_CONVENTIONAL_FIRST = "us-central1"

# Dataform's wording for "this region is not one you have assets in". Anything
# else behind a 403 is treated as "could not tell" rather than as an answer.
_NOT_A_REGION = re.compile(
    r"is not found or access is unauthorized|not found or unauthorized", re.I
)

# Which regions a BigQuery multi-region could plausibly map to.
_MULTIREGION_PREFIX = {"us": "us-", "eu": "europe-"}

# Discovery costs a handful of calls, so each environment pays it once.
_discovered: dict[tuple[str, str], str] = {}


def _configured_location(env) -> str:
    return (env.code_asset_location or "").strip().lower()


def _probe(client, project: str, location: str) -> bool | None:
    """Does this region hold any code assets? One page of one result.

    Returns None for "could not tell", which is not the same as False and must
    never be collapsed into it: this originally caught every GoogleAPIError as
    "empty", and since ResourceExhausted is one, a depleted quota reported a
    project with 616 code assets as having none. Absence is a conclusion, and
    a failed lookup does not support it.
    """
    from google.api_core import exceptions as api_exceptions

    def once():
        pager = client.list_repositories(
            request={
                "parent": f"projects/{project}/locations/{location}",
                "page_size": 1,
            }
        )
        return next(iter(pager), None) is not None

    try:
        return _retrying(once)
    except api_exceptions.NotFound:
        return False
    except api_exceptions.PermissionDenied as exc:
        # 403 is overloaded. Dataform answers an unused region with
        # "Location <x> is not found or access is unauthorized", which is a
        # real answer; GCP also reports rate limiting as 403 on some APIs,
        # which is not. Only the first licenses "not here", so the message has
        # to say so -- classifying by exception type alone is what let a
        # transient failure be reported as absence.
        return False if _NOT_A_REGION.search(str(exc)) else None
    except api_exceptions.GoogleAPIError:
        return None


def _candidate_regions(client, project: str, dataset_location: str) -> list[str]:
    """Regions worth probing, most likely first."""
    prefix = _MULTIREGION_PREFIX.get(dataset_location.lower().strip(), "")
    try:
        available = [
            loc.location_id
            for loc in _retrying(
                lambda: client.list_locations(request={"name": f"projects/{project}"})
            ).locations
        ]
    except Exception:
        # Falling back to the conventional region keeps discovery working when
        # locations cannot be listed. It narrows what gets probed, so the
        # caller is told when the candidate list came from here rather than
        # from the API -- "probed one region" and "probed twelve" support very
        # different conclusions.
        available = [_CONVENTIONAL_FIRST]

    matching = [loc for loc in available if not prefix or loc.startswith(prefix)]
    ordered = ([_CONVENTIONAL_FIRST] if _CONVENTIONAL_FIRST in matching else []) + [
        loc for loc in matching if loc != _CONVENTIONAL_FIRST
    ]
    # Probing forty regions to answer one question is not a convenience.
    return ordered[:12]


def _discover_location(client, env) -> str:
    """Find the region holding this project's code assets.

    Reached when the configured location is a multi-region, which can never
    work: Dataform rejects ``US`` outright, so honouring it would guarantee
    the failure this exists to avoid. Discovery replaces a config edit and a
    client restart with a few one-row probes, and the result says which region
    it found so it can be pinned.
    """
    key = (env.project, env.location.lower())
    if key in _discovered:
        return _discovered[key]

    candidates = _candidate_regions(client, env.project, env.location)
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        outcome = dict(
            pool.map(lambda loc: (loc, _probe(client, env.project, loc)), candidates)
        )

    for loc in candidates:  # candidate order is the preference order
        if outcome.get(loc) is True:
            _discovered[key] = loc
            return loc

    # Nothing found -- but "looked and it is not there" and "could not look"
    # are different answers, and only the first one licenses "none".
    unknown = [loc for loc in candidates if outcome.get(loc) is None]
    if unknown:
        raise DataPlatformMCPError(
            f"Environment '{env.name}': could not determine which region holds "
            f"this project's code assets. {len(unknown)} of "
            f"{len(candidates)} regions could not be checked "
            f"({', '.join(unknown[:5])}), most likely the Dataform read quota, "
            "which refills over tens of seconds -- retrying shortly will "
            "usually work.\nThis is NOT a report that the project has no code "
            "assets; that was not established. To skip discovery entirely, set "
            f"code_asset_location on environment '{env.name}' (or "
            "BQ_CODE_ASSET_LOCATION) to the region shown in BigQuery Studio > "
            "Settings."
        )
    raise DataPlatformMCPError(
        f"Environment '{env.name}': found no BigQuery Studio code assets in "
        f"any of the {len(candidates)} regions reachable from location "
        f"'{env.location}'.\n"
        f"Probed, all answered successfully: {', '.join(candidates)}.\n"
        f"'{env.location}' is a multi-region, which Dataform rejects, so the "
        "region had to be discovered. If the assets are in a region outside "
        "that list, name it explicitly -- set code_asset_location on the "
        "environment (or BQ_CODE_ASSET_LOCATION) to the region shown in "
        "BigQuery Studio > Settings. If the project genuinely has none, this "
        "is the right answer."
    )


def _location(env, client=None) -> str:
    """The region holding this environment's code assets.

    Explicit configuration always wins. A configured *regional* dataset
    location is used as-is. A multi-region is discovered around, because using
    it is not an option -- see _discover_location.
    """
    configured = _configured_location(env)
    if configured:
        return configured
    dataset_location = env.location.strip().lower()
    if _REGIONAL.search(dataset_location):
        return dataset_location
    if client is None:
        return dataset_location
    return _discover_location(client, env)


def _parent(env, client=None) -> str:
    return f"projects/{env.project}/locations/{_location(env, client)}"


def _explain_dataform_failure(exc: Exception, env, loc: str = "") -> Exception:
    from google.api_core import exceptions as api_exceptions

    loc = loc or _configured_location(env) or env.location
    if isinstance(exc, api_exceptions.PermissionDenied):
        identity = env.impersonate or "the signed-in user"
        # A bad location and a missing role both arrive as 403 here, and the
        # message differs only in wording, so name both possibilities.
        return DataPlatformMCPError(
            f"Environment '{env.name}': cannot read BigQuery Studio code assets "
            f"in location '{loc}'. Two different causes look identical here:\n"
            f"  1. {identity} lacks roles/dataform.viewer on {env.project}. "
            "This is a separate permission from BigQuery and from the Data "
            "Transfer Service, so the other tools working does not imply this "
            "one will:\n"
            f"       gcloud projects add-iam-policy-binding {env.project} \\\n"
            f"         --member='{'serviceAccount:' + env.impersonate if env.impersonate else 'user:EMAIL'}' \\\n"
            "         --role=roles/dataform.viewer\n"
            f"  2. '{loc}' is not a valid Dataform region. Code assets are "
            "regional and multi-regions are rejected, so a dataset location "
            "like 'US' or 'EU' will always fail -- set code_asset_location on "
            "the environment to the region shown in BigQuery Studio's settings "
            "(commonly us-central1).\n"
            f"Underlying error: {str(exc)[:200]}"
        )
    if isinstance(exc, api_exceptions.ResourceExhausted):
        return DataPlatformMCPError(
            f"Environment '{env.name}': the Dataform read quota is exhausted "
            "and did not recover within the retry budget. It refills over tens "
            "of seconds, so retrying this call shortly will usually work. If "
            "this was a search, narrow it with asset_type or name_contains so "
            "it reads fewer assets."
        )
    return exc


def _retrying(fn, *, retries: int = _MAX_RETRIES):
    """Call ``fn``, backing off on quota exhaustion.

    The quota refills on a timescale of seconds, so sleeping is genuinely the
    fix rather than a way of hiding a real error. Jitter keeps a thread pool
    from re-colliding in lockstep after each wait.
    """
    from google.api_core import exceptions as api_exceptions

    for attempt in range(retries):
        try:
            return fn()
        except api_exceptions.ResourceExhausted:
            if attempt == retries - 1:
                raise
            time.sleep((2**attempt) * 0.75 + random.random() * 0.5)
    raise AssertionError("unreachable")  # pragma: no cover


def _asset_type(repo) -> str:
    return dict(repo.labels).get(_TYPE_LABEL, "")


def _last_modified(repo) -> str | None:
    """Best-effort modification time.

    It lives in ``internal_metadata``, a JSON string the API documents no
    schema for. The name says it may change without notice, so every access is
    guarded and a miss simply reports nothing rather than failing the call.
    """
    try:
        return json.loads(repo.internal_metadata or "{}").get("last_modified_time")
    except (ValueError, TypeError, AttributeError):
        return None


def _summarise(repo) -> dict:
    return {
        "name": repo.display_name,
        "id": repo.name.rsplit("/", 1)[-1],
        "type": _asset_type(repo) or "unknown",
        "created": repo.create_time.isoformat() if repo.create_time else None,
        "last_modified": _last_modified(repo),
    }


def _list_repositories(client, env) -> tuple[list, str]:
    """Every tool's single entry point, so location resolution lives here.

    Returns the repositories and the location they came from -- the caller
    reports it, and after discovery it is not knowable any other way.
    """
    location = _location(env, client)
    parent = f"projects/{env.project}/locations/{location}"
    try:
        repos = _retrying(lambda: list(client.list_repositories(request={"parent": parent})))
    except Exception as exc:
        raise _explain_dataform_failure(exc, env, location) from exc
    return repos, location


def _read_body(client, repo) -> str:
    """The single file a code asset stores its body in, decoded.

    One asset in 616 carried no type label at all. Guessing an extension for it
    produces a NotFound that says nothing useful, so unknown types ask the API
    what the file is actually called instead.
    """
    kind = _asset_type(repo)
    path = _CONTENT_FILE.get(kind)
    if path is None:
        entries = _retrying(
            lambda: [
                e.file
                for e in client.query_repository_directory_contents(
                    request={"name": repo.name}
                )
                if e.file
            ]
        )
        if not entries:
            return ""
        path = entries[0]

    raw = _retrying(
        lambda: client.read_repository_file(
            request={"name": repo.name, "path": path}
        ).contents
    )
    return raw.decode("utf-8", errors="replace")


def _notebook_source(body: str) -> tuple[list[dict], dict]:
    """Cells with their outputs removed, plus what removing them saved.

    Reporting the saving is not decoration: it tells the reader that what they
    are looking at is the whole of the *code*, and that the missing bulk was
    rendered output rather than logic someone might need.
    """
    try:
        notebook = json.loads(body)
    except ValueError:
        return [], {"note": "content is not valid notebook JSON; returned as text"}

    cells = []
    for index, cell in enumerate(notebook.get("cells", [])):
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if not source.strip():
            continue
        cells.append(
            {
                "cell": index,
                "type": cell.get("cell_type", "code"),
                "source": source,
            }
        )
    kept = sum(len(c["source"]) for c in cells)
    return cells, {
        "raw_chars": len(body),
        "source_chars": kept,
        "outputs_stripped": len(body) > kept,
    }


def list_code_assets(
    environment: str = "",
    asset_type: str = "",
    name_contains: str = "",
    limit: int = 100,
) -> dict:
    """List Colab notebooks and saved queries in BigQuery Studio.

    Use this for anything the user calls a Colab notebook, Colab Enterprise
    notebook, "colab script", BigQuery notebook, saved query or data canvas --
    BigQuery Studio stores all of them as code assets and this lists them all.

    Free -- this reads metadata only and never opens an asset. Bodies are what
    cost quota, so filter here first and open individual assets afterwards.

    Args:
        environment: Which configured environment to read. Omit for the default.
        asset_type: Restrict to one of 'sql', 'notebook', 'data_canvas'.
            Saved queries usually outnumber notebooks by a wide margin, so
            this is the difference between a readable answer and 600 rows.
        name_contains: Case-insensitive substring match on the display name.
        limit: Maximum assets to return.
    """
    env = require_environment(environment)
    client = _client(env)
    repos, location = _list_repositories(client, env)

    counts: dict[str, int] = {}
    for repo in repos:
        kind = _asset_type(repo) or "unknown"
        counts[kind] = counts.get(kind, 0) + 1

    wanted = asset_type.strip().lower()
    needle = name_contains.strip().lower()
    matched = [
        repo
        for repo in repos
        if (not wanted or _asset_type(repo) == wanted)
        and (not needle or needle in (repo.display_name or "").lower())
    ]
    capped = max(1, min(limit, 500))
    shown = matched[:capped]

    result = {
        "environment": env.name,
        "project": env.project,
        # Always stated: an empty list here usually means the wrong region
        # rather than an empty project, and that is invisible otherwise.
        "location": location,
        "total_in_project": len(repos),
        "by_type": counts,
        "matched": len(matched),
        "assets": [_summarise(repo) for repo in shown],
    }
    if not _configured_location(env) and location != env.location.strip().lower():
        result["location_note"] = (
            f"'{env.location}' is a multi-region, which Dataform rejects, so "
            f"'{location}' was discovered by probing. Set "
            f"code_asset_location = \"{location}\" on environment "
            f"'{env.name}' to skip that on future calls."
        )
    if len(shown) < len(matched):
        result["truncated"] = (
            f"Showing {len(shown)} of {len(matched)} matching assets. Narrow "
            "with asset_type or name_contains, or raise limit."
        )
    if not repos:
        result["note"] = (
            f"No code assets in '{location}'. This location was used as "
            "given rather than discovered, and a wrong region returns empty "
            "rather than erroring -- check the region in BigQuery Studio > "
            "Settings before concluding the project has none."
        )
    return result


def get_code_asset(asset: str, environment: str = "") -> dict:
    """Return one Colab notebook or saved query's contents, by name or id.

    Notebook outputs are stripped -- across 52 real notebooks they were 77% of
    the bytes, and none of the logic.

    Args:
        asset: Display name (as shown in BigQuery Studio) or the asset id.
        environment: Which configured environment to read. Omit for the default.
    """
    env = require_environment(environment)
    client = _client(env)
    repos, location = _list_repositories(client, env)

    wanted = asset.strip().lower()
    exact = [
        r
        for r in repos
        if (r.display_name or "").lower() == wanted
        or r.name.rsplit("/", 1)[-1].lower() == wanted
    ]
    if not exact:
        near = [r.display_name for r in repos if wanted in (r.display_name or "").lower()]
        raise DataPlatformMCPError(
            f"No code asset named '{asset}' in environment '{env.name}' "
            f"(location {location})."
            + (
                "\nDid you mean: " + ", ".join(near[:10])
                if near
                else "\nUse list_code_assets to see what exists."
            )
        )

    # Display names are not unique in BigQuery Studio -- two people can save a
    # query under the same name. Returning the first silently would answer a
    # question about one asset with the contents of another.
    if len(exact) > 1:
        raise DataPlatformMCPError(
            f"'{asset}' matches {len(exact)} code assets in environment "
            f"'{env.name}'. Display names are not unique; ask for one by id:\n"
            + "\n".join(
                f"  {r.name.rsplit('/', 1)[-1]}  ({_asset_type(r) or 'unknown'}, "
                f"modified {_last_modified(r) or 'unknown'})"
                for r in exact[:10]
            )
        )

    repo = exact[0]
    try:
        body = _read_body(client, repo)
    except Exception as exc:
        raise _explain_dataform_failure(exc, env) from exc

    result = _summarise(repo)
    result.update({"environment": env.name, "project": env.project, "location": location})

    if _asset_type(repo) == "notebook":
        cells, stats = _notebook_source(body)
        result["cells"] = cells
        result.update(stats)
        payload = "\n".join(c["source"] for c in cells)
    else:
        payload = body
        result["content"] = body

    if len(payload) > _MAX_PAYLOAD_CHARS:
        if "content" in result:
            result["content"] = payload[:_MAX_PAYLOAD_CHARS]
        result["stopped_for_size"] = (
            f"Asset body is {len(payload)} characters; the first "
            f"{_MAX_PAYLOAD_CHARS} are shown."
        )

    # The heuristic that answers "what writes this table" for scheduled queries
    # works on saved queries too -- they are the same kind of SQL. Notebooks
    # need one extra filter: their SQL is built in Python, so the same regex
    # happily reports `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}` as a destination.
    # An interpolation marker means the real target is only known at runtime.
    if _asset_type(repo) in ("sql", "notebook"):
        from .transfer_tools import _targets_from_sql

        targets = [
            t
            for t in _targets_from_sql(payload)
            if not any(marker in t for marker in ("{", "}", "$", "%s"))
        ]
        if targets:
            result["writes_to_from_sql"] = targets
    return result


def find_code_assets_using_table(
    table: str,
    environment: str = "",
    asset_type: str = "",
    max_assets: int = _DEFAULT_SCAN_CAP,
) -> dict:
    """Find which Colab notebooks and saved queries reference a table.

    The question to ask before changing or dropping a table:
    ``list_scheduled_queries`` says what writes it, this says who reads it.

    Unlike the other tools here this one opens every asset it considers, which
    costs Dataform read quota. It is bounded by ``max_assets`` and reports how
    much of the project it actually covered -- a result is evidence about the
    assets scanned, never proof that nothing else uses the table.

    Args:
        table: Table name to search for. A bare name matches any qualification;
            'dataset.table' or a fully-qualified name narrows it.
        environment: Which configured environment to read. Omit for the default.
        asset_type: Restrict to 'sql', 'notebook' or 'data_canvas'.
        max_assets: Ceiling on how many bodies to read.
    """
    env = require_environment(environment)
    client = _client(env)
    repos, location = _list_repositories(client, env)

    wanted = asset_type.strip().lower()
    if wanted:
        repos = [r for r in repos if _asset_type(r) == wanted]

    # Match on the last path segment so `proj.ds.tbl`, `ds.tbl` and `tbl` all
    # find each other, then confirm with a word-ish boundary so searching for
    # "users" does not report every "users_daily_snapshot".
    needle = table.strip().strip("`").split(".")[-1].lower()
    if not needle:
        raise DataPlatformMCPError("Pass a table name to search for.")

    cap = max(1, min(max_assets, 500))
    scanned = repos[:cap]

    lock = threading.Lock()
    hits: list[dict] = []
    failed: list[str] = []

    def scan(repo) -> None:
        try:
            body = _read_body(client, repo)
        except Exception as exc:  # one unreadable asset must not fail the search
            with lock:
                failed.append(f"{repo.display_name}: {type(exc).__name__}")
            return
        lowered = body.lower()
        if needle not in lowered:
            return
        lines = [
            line.strip()[:200]
            for line in body.splitlines()
            if needle in line.lower()
        ]
        with lock:
            hits.append(
                {
                    **_summarise(repo),
                    # Occurrences, not matching lines. A query that joins a
                    # table to itself puts both references on one line, and
                    # counting lines would rank it below a single mention.
                    "matches": lowered.count(needle),
                    "lines": len(lines),
                    "sample": lines[:3],
                }
            )

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        list(pool.map(scan, scanned))

    hits.sort(key=lambda h: (-h["matches"], h["name"] or ""))
    result = {
        "environment": env.name,
        "project": env.project,
        "location": location,
        "searched_for": needle,
        "assets_scanned": len(scanned),
        "assets_available": len(repos),
        "found_in": len(hits),
        "assets": hits,
    }
    if len(scanned) < len(repos):
        result["truncated"] = (
            f"Scanned {len(scanned)} of {len(repos)} assets (max_assets={cap}). "
            "Assets not scanned may also reference this table; narrow with "
            "asset_type or raise max_assets."
        )
    if failed:
        # Silence here would read as "not used by these", which is the one
        # conclusion the caller must not draw from a failed read.
        result["unread"] = {
            "count": len(failed),
            "detail": failed[:10],
            "note": (
                "These assets could not be read and were NOT searched. Usually "
                "the Dataform read quota; retry shortly for full coverage."
            ),
        }
    return result


def register(mcp) -> None:
    register_tool(mcp, list_code_assets)
    register_tool(mcp, get_code_asset)
    register_tool(mcp, find_code_assets_using_table)
