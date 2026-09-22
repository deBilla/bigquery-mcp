"""Scheduled Colab notebooks: what runs on a timer, and what has been failing.

``list_scheduled_queries`` covers the other kind of scheduled work on this
platform. This covers the kind people actually lose track of: a Colab notebook
in BigQuery Studio with a schedule attached to it. Nobody watches those runs,
so a notebook can fail nightly for weeks and the only symptom is a table that
quietly stopped moving.

Three different resources have to be joined to answer one question, which is
most of why this was hard to see:

  - The **schedule** is a Vertex AI ``Schedule`` carrying a
    ``createNotebookExecutionJobRequest``. It holds the cron, the timezone,
    whether it is paused, and the service account the notebook runs as.
  - Each **run** is a Vertex AI ``NotebookExecutionJob``, linked back by
    ``scheduleResourceName``. This is the only place a pass/fail lives.
  - The **notebook** is a Dataform repository -- the same code asset
    ``list_code_assets`` lists -- referenced by
    ``dataformRepositorySource``. So a failing schedule leads straight to
    ``get_code_asset`` for the code that failed.

Four properties of that arrangement drive everything below.

**A schedule reports itself healthy while its notebook fails.** Every one of
49 schedules on this platform reports ``lastScheduledRunResponse.runResponse:
"OK"`` -- including schedules whose last 79 runs failed. ``runResponse`` means
the scheduler successfully *launched* a job; it says nothing about whether the
notebook ran. Reading the Schedule resource alone, which is what the console
page shows first, is exactly how this went unnoticed. Health here is therefore
always computed from execution jobs, never from the schedule's own state.

**The runs cannot be filtered by outcome server-side.** ``jobState`` and
``createTime`` are both rejected as filters (``INVALID_ARGUMENT``); only
``displayName`` and ``scheduleResourceName`` are accepted. Page size is capped
at 100 and the project holds 7,061 jobs, so "show me the failures" means
reading pages and filtering here. ``orderBy=createTime desc`` *is* honoured,
which is what makes a bounded lookback possible: pages arrive newest-first and
paging stops at the cutoff instead of walking a year of history. Measured: 30
days is ~670 jobs over 7 pages (~10s); 90 days is 23 pages (~32s).

**Jobs outlive the schedule that created them.** 7,061 jobs reference 75
distinct schedules, but only 49 still exist -- deleting and recreating a
schedule is the normal way to edit one, so a quarter of the history points at
resource names that now 404. Those runs are real and their failures count, so
they are reported against the id rather than dropped for want of a parent.

**Neither display names nor notebook names are unique or equal.** Two live
schedules share the name ``monthly_subs_mrr``, and a schedule named
``daily_mp_admob_cohort_campaign`` runs a notebook named
``mp_admob_cohort_campaign``. Ambiguity is an error that lists the candidates,
never a silently chosen first match.

This talks to Vertex AI over REST rather than through ``google-cloud-aiplatform``.
That package is a very large dependency for two list endpoints, and the
credentials machinery is already here -- so it costs a new IAM role
(``roles/aiplatform.viewer``) but no new install.

Nothing here can create, pause, resume, trigger or delete a schedule.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse

from ..config import require_environment
from ..errors import DataPlatformMCPError
from ..registration import register_tool

# The API's hard ceiling; asking for more is a 400, not a smaller page.
_PAGE_SIZE = 100

# Enough to cover a monthly schedule, which a 7-day window would report as
# having never run at all. Costs ~7 pages on this platform.
_DEFAULT_LOOKBACK_DAYS = 30

# A guard on the walk, not a target: 100 pages is 10,000 runs, past which the
# answer is almost certainly "narrow the window" rather than "keep reading".
_MAX_PAGES = 100

_SUCCEEDED = "JOB_STATE_SUCCEEDED"
_FAILED = "JOB_STATE_FAILED"
_RUNNING = ("JOB_STATE_RUNNING", "JOB_STATE_PENDING", "JOB_STATE_QUEUED")

# What a caller may ask for, in the words they would use.
_STATUS_ALIASES = {
    "failed": (_FAILED,),
    "failure": (_FAILED,),
    "failing": (_FAILED,),
    "error": (_FAILED,),
    "succeeded": (_SUCCEEDED,),
    "success": (_SUCCEEDED,),
    "successful": (_SUCCEEDED,),
    "ok": (_SUCCEEDED,),
    "running": _RUNNING,
    "pending": _RUNNING,
}

# One failure message arrives as HTML with <b>, <br> and an <a href> in it,
# which renders as noise in a terminal.
_TAGS = re.compile(r"<[^>]+>")
_MAX_ERROR_CHARS = 400


def _session(env):
    """An authorised HTTP session for the Vertex AI REST API."""
    from google.auth.transport.requests import AuthorizedSession

    from ..clients import get_credentials

    return AuthorizedSession(get_credentials(env.impersonate))


def _location(env) -> str:
    """The region holding this environment's notebook schedules.

    BigQuery Studio creates a notebook's schedule in the same region as the
    notebook itself, so this deliberately reuses the code-asset resolution --
    including its discovery path and its cache. A dataset location like 'US'
    is a multi-region and is not a valid Vertex AI region either, so the same
    probe that finds the notebooks finds the schedules.
    """
    from .code_asset_tools import _client as dataform_client
    from .code_asset_tools import _location as resolve

    env_location = (env.code_asset_location or "").strip().lower()
    if env_location:
        return env_location
    return resolve(env, dataform_client(env))


def _endpoint(env, location: str, resource: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{env.project}/locations/{location}/{resource}"
    )


def _explain_vertex_failure(response, env, location: str) -> Exception:
    detail = (response.text or "")[:300]
    if response.status_code == 403:
        identity = env.impersonate or "the signed-in user"
        return DataPlatformMCPError(
            f"Environment '{env.name}': permission denied reading notebook "
            f"schedules in '{location}'. {identity} needs "
            f"roles/aiplatform.viewer on {env.project}. This is a third "
            "permission, separate from BigQuery, from the Data Transfer "
            "Service and from Dataform -- the other tools working does not "
            "imply this one will:\n"
            f"    gcloud projects add-iam-policy-binding {env.project} \\\n"
            f"      --member='{'serviceAccount:' + env.impersonate if env.impersonate else 'user:EMAIL'}' \\\n"
            "      --role=roles/aiplatform.viewer\n"
            f"Underlying error: {detail}"
        )
    if response.status_code == 404:
        return DataPlatformMCPError(
            f"Environment '{env.name}': no Vertex AI scheduling service in "
            f"location '{location}' for project {env.project}. Notebook "
            "schedules are regional; check the region shown in BigQuery "
            "Studio > Settings.\n"
            f"Underlying error: {detail}"
        )
    return DataPlatformMCPError(
        f"Environment '{env.name}': reading notebook schedules in '{location}' "
        f"failed with HTTP {response.status_code}.\nUnderlying error: {detail}"
    )


def _get_pages(
    session,
    url: str,
    key: str,
    env,
    location: str,
    *,
    params=None,
    stop=None,
    max_items: int = 0,
):
    """Walk a paged Vertex AI list endpoint, newest first.

    ``stop`` is called with each item and ends the walk when it returns True;
    ``max_items`` ends it after that many items. Because
    ``orderBy=createTime desc`` is honoured, either one turns a lookback window
    into a few pages rather than a full history scan -- the whole reason a
    time-bounded default is affordable.

    Returns the items and whether the page guard cut the walk short, which the
    caller has to report: a truncated walk under-counts failures, and silently
    under-reporting a failure is the one error this module must not make.
    """
    items, page_token, pages = [], "", 0
    while pages < _MAX_PAGES:
        query = dict(params or {})
        query["pageSize"] = _PAGE_SIZE
        if page_token:
            query["pageToken"] = page_token
        response = session.get(f"{url}?{urllib.parse.urlencode(query)}", timeout=60)
        if response.status_code != 200:
            raise _explain_vertex_failure(response, env, location)
        payload = response.json()
        for item in payload.get(key, []):
            if stop is not None and stop(item):
                return items, False
            items.append(item)
            if max_items and len(items) >= max_items:
                return items, False
        page_token = payload.get("nextPageToken", "")
        pages += 1
        if not page_token:
            return items, False
    return items, True


def _clean_error(status: dict | None) -> str:
    if not status:
        return ""
    message = _TAGS.sub(" ", status.get("message", "") or "")
    return " ".join(message.split())[:_MAX_ERROR_CHARS]


def _short(resource_name: str) -> str:
    return (resource_name or "").rsplit("/", 1)[-1]


def _notebook_names(env) -> dict[str, str]:
    """Map code-asset id to display name, best effort.

    Worth one extra call: a schedule's own name is not the notebook's name
    (``daily_mp_admob_cohort_campaign`` runs ``mp_admob_cohort_campaign``), and
    the id alone is not something anyone recognises. Guarded because this is a
    convenience -- the Dataform read quota failing must not take down a
    question about schedules, which do not live in Dataform at all.
    """
    try:
        from .code_asset_tools import _client, _list_repositories

        repos, _ = _list_repositories(_client(env), env)
        return {_short(r.name): r.display_name for r in repos}
    except Exception:
        return {}


def _notebook_ref(schedule: dict) -> str:
    request = schedule.get("createNotebookExecutionJobRequest", {})
    job = request.get("notebookExecutionJob", {})
    source = job.get("dataformRepositorySource", {})
    return _short(source.get("dataformRepositoryResourceName", ""))


def _cutoff(lookback_days: int) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    return moment.isoformat().replace("+00:00", "Z")


def _fetch_runs(session, env, location: str, cutoff: str, *, schedule_filter: str = ""):
    """Every execution job newer than ``cutoff``, newest first."""
    params = {"orderBy": "createTime desc"}
    if schedule_filter:
        params["filter"] = schedule_filter
    return _get_pages(
        session,
        _endpoint(env, location, "notebookExecutionJobs"),
        "notebookExecutionJobs",
        env,
        location,
        params=params,
        stop=lambda job: job.get("createTime", "") < cutoff,
    )


def _summarise_run(job: dict, schedule_names: dict[str, str] | None = None) -> dict:
    schedule = job.get("scheduleResourceName", "")
    row = {
        "run_id": _short(job.get("name", "")),
        "name": job.get("displayName", ""),
        "state": job.get("jobState", "").replace("JOB_STATE_", "") or "UNKNOWN",
        "started": job.get("createTime"),
        "finished": job.get("updateTime"),
    }
    error = _clean_error(job.get("status"))
    if error:
        row["error"] = error
    if schedule:
        row["schedule_id"] = _short(schedule)
        if schedule_names is not None:
            # A run whose schedule has been deleted is still a real run, and
            # its failures still count -- so it is labelled, not dropped.
            row["schedule"] = schedule_names.get(
                schedule, "(schedule no longer exists)"
            )
    else:
        # No parent schedule at all: someone ran the notebook by hand.
        row["schedule"] = "(manual run, not scheduled)"
    return row


def _health(runs: list[dict]) -> dict:
    """Pass/fail for one schedule's runs, which must arrive newest first."""
    total = len(runs)
    failed = sum(1 for r in runs if r.get("jobState") == _FAILED)
    succeeded = sum(1 for r in runs if r.get("jobState") == _SUCCEEDED)

    consecutive = 0
    for run in runs:
        if run.get("jobState") != _FAILED:
            break
        consecutive += 1

    health = {
        "runs": total,
        "succeeded": succeeded,
        "failed": failed,
    }
    if total:
        health["failure_rate"] = f"{(failed / total) * 100:.0f}%"
        newest = runs[0]
        health["last_run"] = newest.get("createTime")
        health["last_state"] = newest.get("jobState", "").replace("JOB_STATE_", "")
    if consecutive:
        # The number that separates "flaky" from "broken since <date>".
        health["consecutive_failures"] = consecutive

    # Deliberately "last_failure", not "last_error". These are named for the
    # newest *failed* run, which is usually older than the newest run -- a
    # schedule that failed on Tuesday and passed on Wednesday would otherwise
    # carry an error string beside last_state SUCCEEDED and read as broken.
    newest_failure = next((r for r in runs if r.get("jobState") == _FAILED), None)
    if newest_failure is not None:
        health["last_failure"] = newest_failure.get("createTime")
        error = _clean_error(newest_failure.get("status"))
        if error:
            health["last_failure_error"] = error
    return health


def list_notebook_schedules(
    environment: str = "",
    state: str = "",
    name_contains: str = "",
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """List scheduled Colab notebooks with how many recent runs passed or failed.

    This is the health overview for scheduled notebook work: what is scheduled,
    whether it is active or paused, and — the part that is otherwise invisible
    — how its actual runs have been going.

    Do not read a schedule's own state as health. A schedule reports its last
    scheduled run as "OK" when it successfully *launched* the notebook, whether
    or not the notebook then failed; on this platform every schedule says OK
    while hundreds of runs have failed. The pass/fail numbers here come from
    the execution jobs, which is the only place the outcome exists.

    Args:
        environment: Which configured environment to read. Omit for the default.
        state: Restrict to 'active' or 'paused'. A paused schedule that used to
            fail is a common find — someone paused it instead of fixing it.
        name_contains: Case-insensitive substring match on the schedule name.
        lookback_days: How far back to read runs for the pass/fail counts.
            Defaults to 30 so monthly schedules show at least one run. Larger
            windows cost proportionally more (90 days is roughly 23 API pages).
            Set to 0 to skip run history entirely and just list what exists.
    """
    env = require_environment(environment)
    location = _location(env)
    session = _session(env)

    schedules, _ = _get_pages(
        session, _endpoint(env, location, "schedules"), "schedules", env, location
    )
    # Vertex AI hosts other kinds of schedule (pipeline jobs); only notebook
    # schedules belong in an answer about Colab notebooks.
    schedules = [s for s in schedules if "createNotebookExecutionJobRequest" in s]

    wanted_state = state.strip().upper()
    needle = name_contains.strip().lower()
    matched = [
        s
        for s in schedules
        if (not wanted_state or s.get("state", "") == wanted_state)
        and (not needle or needle in (s.get("displayName", "") or "").lower())
    ]

    by_schedule: dict[str, list[dict]] = {}
    truncated = False
    days = max(0, min(lookback_days, 365))
    if days:
        runs, truncated = _fetch_runs(session, env, location, _cutoff(days))
        for job in runs:
            by_schedule.setdefault(job.get("scheduleResourceName", ""), []).append(job)

    notebooks = _notebook_names(env)

    rows = []
    for schedule in matched:
        request = schedule.get("createNotebookExecutionJobRequest", {}).get(
            "notebookExecutionJob", {}
        )
        notebook_id = _notebook_ref(schedule)
        row = {
            "name": schedule.get("displayName", ""),
            "id": _short(schedule.get("name", "")),
            "state": schedule.get("state", ""),
            "cron": schedule.get("cron", ""),
            "next_run": schedule.get("nextRunTime"),
            "notebook": notebooks.get(notebook_id, ""),
            "notebook_id": notebook_id,
        }
        if notebook_id and notebooks and notebook_id not in notebooks:
            # A schedule whose notebook was deleted fails every run with a
            # Dataform 404, and nothing in the schedule itself shows it.
            row["notebook"] = "(notebook no longer exists)"
        if days:
            row["health"] = _health(by_schedule.get(schedule.get("name", ""), []))
        rows.append(row)

    # Broken first: that is the question this tool exists to answer.
    rows.sort(
        key=lambda r: (
            -r.get("health", {}).get("failed", 0),
            -r.get("health", {}).get("runs", 0),
            r["name"],
        )
    )

    failing = [r["name"] for r in rows if r.get("health", {}).get("failed")]
    never_ran = [
        r["name"]
        for r in rows
        if days and r.get("state") == "ACTIVE" and not r.get("health", {}).get("runs")
    ]

    result = {
        "environment": env.name,
        "project": env.project,
        "location": location,
        "count": len(rows),
        "active": sum(1 for r in rows if r["state"] == "ACTIVE"),
        "paused": sum(1 for r in rows if r["state"] == "PAUSED"),
        "schedules": rows,
    }
    if days:
        result["lookback_days"] = days
    if failing:
        result["failing"] = failing
        result["note"] = (
            f"{len(failing)} of {len(rows)} schedules had at least one failed "
            f"run in the last {days} days. Call get_notebook_schedule for the "
            "error text and recent run history of one of them."
        )
    if never_ran:
        result["active_but_no_runs"] = never_ran
        result["active_but_no_runs_note"] = (
            f"These are ACTIVE but have no run in the last {days} days. Either "
            "they run less often than that, or they are not firing at all — "
            "check `cron` and `next_run` on each."
        )
    if truncated:
        result["truncated"] = (
            f"Stopped after {_MAX_PAGES} pages of run history; the counts cover "
            "only the runs read. Reduce lookback_days for a complete window."
        )
    if not rows:
        result["note"] = (
            f"No scheduled notebooks found in '{location}'. Notebook schedules "
            "are regional and a wrong region returns empty rather than "
            "erroring — check the region in BigQuery Studio > Settings before "
            "concluding there are none."
        )
    return result


def list_notebook_runs(
    status: str = "failed",
    environment: str = "",
    schedule: str = "",
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    limit: int = 50,
) -> dict:
    """List individual scheduled-notebook runs, by default the failed ones.

    Use this for "what has been failing?" across every scheduled notebook at
    once, rather than per schedule. Runs are returned newest first.

    Outcome cannot be filtered server-side — the API rejects a jobState filter
    — so this reads the runs in the window and filters here. That makes
    lookback_days the cost control: each 100 runs is one API call.

    Args:
        status: 'failed' (default), 'succeeded', 'running', or 'all'.
        environment: Which configured environment to read. Omit for the default.
        schedule: Restrict to one schedule, by its name or id.
        lookback_days: How far back to read. Defaults to 30.
        limit: Maximum runs to return.
    """
    env = require_environment(environment)
    location = _location(env)
    session = _session(env)

    wanted = status.strip().lower()
    if wanted in ("", "all", "any"):
        states: tuple[str, ...] = ()
    elif wanted in _STATUS_ALIASES:
        states = _STATUS_ALIASES[wanted]
    else:
        raise DataPlatformMCPError(
            f"Unknown status '{status}'. Use 'failed', 'succeeded', 'running' "
            "or 'all'."
        )

    schedules, _ = _get_pages(
        session, _endpoint(env, location, "schedules"), "schedules", env, location
    )
    schedule_names = {s["name"]: s.get("displayName", "") for s in schedules if "name" in s}

    schedule_filter = ""
    if schedule.strip():
        target = schedule.strip().lower()
        matches = [
            s
            for s in schedules
            if (s.get("displayName", "") or "").lower() == target
            or _short(s.get("name", "")).lower() == target
        ]
        if not matches:
            raise DataPlatformMCPError(
                f"No schedule named '{schedule}' in environment '{env.name}' "
                f"(location {location}). Call list_notebook_schedules to see "
                "what exists."
            )
        if len(matches) > 1:
            raise DataPlatformMCPError(
                f"'{schedule}' matches {len(matches)} schedules; names are not "
                "unique here. Ask for one by id:\n"
                + "\n".join(
                    f"  {_short(m.get('name', ''))}  ({m.get('state', '')}, "
                    f"cron {m.get('cron', '')})"
                    for m in matches
                )
            )
        schedule_filter = f'scheduleResourceName="{matches[0]["name"]}"'

    days = max(1, min(lookback_days, 365))
    runs, truncated = _fetch_runs(
        session, env, location, _cutoff(days), schedule_filter=schedule_filter
    )

    selected = [j for j in runs if not states or j.get("jobState") in states]
    capped = max(1, min(limit, 500))
    shown = selected[:capped]

    counts: dict[str, int] = {}
    for job in runs:
        key = job.get("jobState", "UNKNOWN").replace("JOB_STATE_", "")
        counts[key] = counts.get(key, 0) + 1

    result = {
        "environment": env.name,
        "project": env.project,
        "location": location,
        "lookback_days": days,
        "status": wanted or "all",
        "runs_in_window": len(runs),
        "by_state": counts,
        "matched": len(selected),
        "runs": [_summarise_run(j, schedule_names) for j in shown],
    }
    if len(shown) < len(selected):
        result["truncated"] = (
            f"Showing {len(shown)} of {len(selected)} matching runs. Raise "
            "limit or narrow with schedule or lookback_days."
        )
    if truncated:
        result["window_truncated"] = (
            f"Stopped after {_MAX_PAGES} pages, so runs older than the newest "
            f"{len(runs)} in this window were not read."
        )
    if not selected and states:
        result["note"] = (
            f"No runs with status '{wanted}' in the last {days} days. "
            f"{len(runs)} runs were read in that window."
        )
    return result


def get_notebook_schedule(
    schedule: str, runs: int = 10, environment: str = ""
) -> dict:
    """Get one scheduled notebook in full: its cron, its notebook, and recent runs.

    Call this after list_notebook_schedules to see why a scheduled notebook is
    failing. The error text for each failed run is included, and `notebook_id`
    is a code asset id — pass it to get_code_asset to read the code that failed.

    Args:
        schedule: The schedule's name or its id from list_notebook_schedules.
        runs: How many recent runs to include, newest first.
        environment: Which configured environment to read. Omit for the default.
    """
    env = require_environment(environment)
    location = _location(env)
    session = _session(env)

    schedules, _ = _get_pages(
        session, _endpoint(env, location, "schedules"), "schedules", env, location
    )
    target = schedule.strip().lower()
    matches = [
        s
        for s in schedules
        if (s.get("displayName", "") or "").lower() == target
        or _short(s.get("name", "")).lower() == target
    ] or [
        s for s in schedules if target in (s.get("displayName", "") or "").lower()
    ]

    if not matches:
        raise DataPlatformMCPError(
            f"No scheduled notebook matching '{schedule}' in environment "
            f"'{env.name}' (location {location}). Call "
            "list_notebook_schedules to see what exists."
        )
    if len(matches) > 1:
        # Two live schedules really do share a name on this platform, so
        # returning the first would answer about the wrong one invisibly.
        raise DataPlatformMCPError(
            f"'{schedule}' matches {len(matches)} schedules; names are not "
            "unique here. Ask for one by id:\n"
            + "\n".join(
                f"  {_short(m.get('name', ''))}  ({m.get('state', '')}, "
                f"cron {m.get('cron', '')})"
                for m in matches[:10]
            )
        )

    found = matches[0]
    job_request = found.get("createNotebookExecutionJobRequest", {}).get(
        "notebookExecutionJob", {}
    )
    notebook_id = _notebook_ref(found)
    notebooks = _notebook_names(env)

    wanted = max(1, min(runs, 100))
    # Filtering by the schedule server-side is supported and keeps this to one
    # page for any sane `runs`, so no lookback window is needed here.
    recent, _ = _get_pages(
        session,
        _endpoint(env, location, "notebookExecutionJobs"),
        "notebookExecutionJobs",
        env,
        location,
        params={
            "orderBy": "createTime desc",
            "filter": f'scheduleResourceName="{found["name"]}"',
        },
        max_items=wanted,
    )

    result = {
        "environment": env.name,
        "project": env.project,
        "location": location,
        "name": found.get("displayName", ""),
        "id": _short(found.get("name", "")),
        "state": found.get("state", ""),
        "cron": found.get("cron", ""),
        "next_run": found.get("nextRunTime"),
        "created": found.get("createTime"),
        "total_runs_started": found.get("startedRunCount"),
        "notebook": notebooks.get(notebook_id, "") or (
            "(notebook no longer exists)" if notebooks and notebook_id else ""
        ),
        "notebook_id": notebook_id,
        "service_account": job_request.get("serviceAccount", ""),
        "output_uri": job_request.get("gcsOutputUri", ""),
        "recent_runs": [_summarise_run(j) for j in recent],
    }

    result["health"] = _health(recent)

    failures = [j for j in recent if j.get("jobState") == _FAILED]
    if failures:
        result["note"] = (
            f"{len(failures)} of the last {len(recent)} runs failed. The error "
            "text is on each run. Most cell-execution failures need the "
            "notebook itself — call get_code_asset with notebook_id "
            f"'{notebook_id}'. Full output for a run is written under "
            f"{result['output_uri'] or 'the schedule output URI'}."
        )
    if found.get("state") == "PAUSED":
        result["paused_note"] = (
            "This schedule is PAUSED, so it is not running at all. If a table "
            "it writes has gone stale, this is the reason."
        )
    return result


def register(mcp) -> None:
    register_tool(mcp, list_notebook_schedules)
    register_tool(mcp, list_notebook_runs)
    register_tool(mcp, get_notebook_schedule)
