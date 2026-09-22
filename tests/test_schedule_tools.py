"""Scheduled Colab notebooks.

The cases worth pinning are the ones that made this invisible in the first
place: a schedule reports "OK" while its notebook fails, outcome cannot be
filtered server-side, and a quarter of the run history points at schedules
that have since been deleted.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse

import pytest

from data_platform_mcp.errors import DataPlatformMCPError
from data_platform_mcp.tools import schedule_tools
from data_platform_mcp.tools.schedule_tools import (
    get_notebook_schedule,
    list_notebook_runs,
    list_notebook_schedules,
)

PROJECT = "test-project"
REGION = "us-central1"


def _iso(days_ago: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return moment.isoformat().replace("+00:00", "Z")


def _schedule(name, sid, state="ACTIVE", repo="repo-1", notebook=True):
    """A Vertex AI Schedule, shaped as the live API returns one.

    Note ``runResponse: OK`` on every schedule: that is what the real API does
    even for a schedule whose every run failed, and several tests below depend
    on it being present and ignored.
    """
    body = {
        # Vertex AI returns the project *number* here, never the project id.
        "name": f"projects/413621630871/locations/{REGION}/schedules/{sid}",
        "displayName": name,
        "state": state,
        "cron": "TZ=Asia/Singapore 0 8 * * *",
        "nextRunTime": _iso(-1),
        "createTime": _iso(300),
        "startedRunCount": "11",
        "lastScheduledRunResponse": {"runResponse": "OK"},
    }
    if notebook:
        body["createNotebookExecutionJobRequest"] = {
            "notebookExecutionJob": {
                "displayName": name,
                "dataformRepositorySource": {
                    "dataformRepositoryResourceName": (
                        f"projects/{PROJECT}/locations/{REGION}/repositories/{repo}"
                    )
                },
                "gcsOutputUri": f"gs://bucket/{name}/",
                "serviceAccount": "runner@test-project.iam.gserviceaccount.com",
            }
        }
    return body


def _run(name, sid, state, days_ago, error=None, job_id=None):
    job = {
        "name": f"projects/413621630871/locations/{REGION}/notebookExecutionJobs/"
        f"{job_id or (name + str(days_ago))}",
        "displayName": name,
        "jobState": state,
        "createTime": _iso(days_ago),
        "updateTime": _iso(days_ago),
    }
    if sid is not None:
        job["scheduleResourceName"] = (
            f"projects/413621630871/locations/{REGION}/schedules/{sid}"
        )
    if error:
        job["status"] = {"code": 3, "message": error}
    return job


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Pages, filters and orders the way the live endpoint was measured to.

    Deliberately faithful on two points: ``pageSize`` is capped at 100, and a
    ``jobState`` filter is rejected -- the constraint that forces outcome
    filtering to happen client-side.
    """

    def __init__(self, schedules, runs, fail_with=None):
        self.schedules = schedules
        self.runs = runs
        self.fail_with = fail_with
        self.requests = []

    def get(self, url, timeout=None):
        self.requests.append(url)
        if self.fail_with:
            return FakeResponse({"error": {"message": "denied"}}, self.fail_with)

        path, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        page_size = int(params.get("pageSize", 100))
        assert page_size <= 100, "the live API caps pageSize at 100"
        offset = int(params.get("pageToken", "0") or 0)

        if path.endswith("/schedules"):
            items, key = list(self.schedules), "schedules"
        else:
            items, key = list(self.runs), "notebookExecutionJobs"
            criterion = params.get("filter", "")
            if criterion:
                if "jobState" in criterion:
                    return FakeResponse(
                        {"error": {"message": "Provided filter is not valid."}}, 400
                    )
                field, _, value = criterion.partition("=")
                value = value.strip('"')
                if field == "scheduleResourceName":
                    items = [i for i in items if i.get(field) == value]
                elif field == "displayName":
                    items = [i for i in items if i.get("displayName") == value]
            if params.get("orderBy") == "createTime desc":
                items.sort(key=lambda i: i["createTime"], reverse=True)

        page = items[offset : offset + page_size]
        payload = {key: page}
        if offset + page_size < len(items):
            payload["nextPageToken"] = str(offset + page_size)
        return FakeResponse(payload)


@pytest.fixture
def platform(monkeypatch):
    """Install a fake Vertex AI for one test, with the region already known."""

    def install(schedules, runs, fail_with=None, notebooks=None):
        session = FakeSession(schedules, runs, fail_with)
        monkeypatch.setattr(schedule_tools, "_session", lambda env: session)
        monkeypatch.setattr(schedule_tools, "_location", lambda env: REGION)
        monkeypatch.setattr(
            schedule_tools,
            "_notebook_names",
            lambda env: notebooks if notebooks is not None else {"repo-1": "nb_one"},
        )
        return session

    return install


def test_health_comes_from_runs_not_from_the_schedules_own_status(platform):
    """The bug this tool exists for: runResponse says OK while runs fail.

    Every live schedule reports its last scheduled run as OK, because that
    field means "the job was launched". Reading it as health is exactly how
    hundreds of failures went unnoticed.
    """
    platform(
        [_schedule("nightly", "1")],
        [
            _run("nightly", "1", "JOB_STATE_FAILED", 1, "Error encountered"),
            _run("nightly", "1", "JOB_STATE_FAILED", 2, "Error encountered"),
            _run("nightly", "1", "JOB_STATE_SUCCEEDED", 3),
        ],
    )
    result = list_notebook_schedules()
    health = result["schedules"][0]["health"]

    assert health["failed"] == 2
    assert health["succeeded"] == 1
    assert health["failure_rate"] == "67%"
    assert health["consecutive_failures"] == 2
    assert result["failing"] == ["nightly"]
    # The schedule's own optimistic field must not appear anywhere.
    assert "OK" not in json.dumps(result)


def test_last_failure_is_not_reported_as_the_last_state(platform):
    """A schedule that failed on Tuesday and passed today is not failing.

    The error string is named for the failed run it came from, so it cannot be
    read as the outcome of the newest run.
    """
    platform(
        [_schedule("flaky", "1")],
        [
            _run("flaky", "1", "JOB_STATE_SUCCEEDED", 1),
            _run("flaky", "1", "JOB_STATE_FAILED", 8, "Error encountered"),
        ],
    )
    health = list_notebook_schedules()["schedules"][0]["health"]

    assert health["last_state"] == "SUCCEEDED"
    assert health["last_failure_error"] == "Error encountered"
    assert health["last_failure"] < health["last_run"]
    assert "consecutive_failures" not in health
    assert "last_error" not in health


def test_outcome_is_filtered_client_side(platform):
    """A jobState filter is a 400 from the live API, so it is never sent."""
    session = platform(
        [_schedule("nightly", "1")],
        [
            _run("nightly", "1", "JOB_STATE_FAILED", 1, "boom"),
            _run("nightly", "1", "JOB_STATE_SUCCEEDED", 2),
            _run("nightly", "1", "JOB_STATE_RUNNING", 0),
        ],
    )
    result = list_notebook_runs(status="failed")

    assert result["matched"] == 1
    assert result["runs"][0]["state"] == "FAILED"
    assert result["by_state"] == {"FAILED": 1, "SUCCEEDED": 1, "RUNNING": 1}
    assert not any("jobState" in url for url in session.requests)


@pytest.mark.parametrize(
    "status,expected",
    [("succeeded", "SUCCEEDED"), ("success", "SUCCEEDED"), ("running", "RUNNING")],
)
def test_status_accepts_the_words_people_use(platform, status, expected):
    platform(
        [_schedule("nightly", "1")],
        [
            _run("nightly", "1", "JOB_STATE_FAILED", 1, "boom"),
            _run("nightly", "1", "JOB_STATE_SUCCEEDED", 2),
            _run("nightly", "1", "JOB_STATE_RUNNING", 0),
        ],
    )
    result = list_notebook_runs(status=status)
    assert [r["state"] for r in result["runs"]] == [expected]


def test_unknown_status_is_an_error_not_an_empty_list(platform):
    platform([_schedule("nightly", "1")], [])
    with pytest.raises(DataPlatformMCPError, match="Unknown status"):
        list_notebook_runs(status="borked")


def test_lookback_stops_paging_instead_of_reading_all_history(platform):
    """Newest-first ordering is what makes a bounded window affordable."""
    runs = [_run("nightly", "1", "JOB_STATE_SUCCEEDED", day) for day in range(400)]
    session = platform([_schedule("nightly", "1")], runs)

    result = list_notebook_runs(status="all", lookback_days=7)

    assert result["runs_in_window"] == 7
    # 400 runs is 4 pages; a 7-day window must not have read them all.
    assert sum("notebookExecutionJobs" in url for url in session.requests) == 1


def test_runs_outlive_the_schedule_that_created_them(platform):
    """Live: 7,061 jobs reference 75 schedules, of which only 49 still exist."""
    platform(
        [_schedule("kept", "1")],
        [
            _run("kept", "1", "JOB_STATE_FAILED", 1, "boom"),
            _run("deleted", "999", "JOB_STATE_FAILED", 2, "boom"),
        ],
    )
    result = list_notebook_runs(status="failed")

    orphan = [r for r in result["runs"] if r["schedule_id"] == "999"][0]
    assert orphan["schedule"] == "(schedule no longer exists)"
    # Its failure still counts; dropping it would under-report.
    assert result["matched"] == 2


def test_a_run_with_no_schedule_is_labelled_as_manual(platform):
    platform(
        [_schedule("nightly", "1")],
        [_run("adhoc", None, "JOB_STATE_FAILED", 1, "boom")],
    )
    run = list_notebook_runs(status="failed")["runs"][0]

    assert run["schedule"] == "(manual run, not scheduled)"
    assert "schedule_id" not in run


def test_duplicate_display_names_are_an_error_with_the_ids(platform):
    """Two live schedules really do share the name monthly_subs_mrr."""
    platform(
        [_schedule("dupe", "1"), _schedule("dupe", "2", state="PAUSED")],
        [],
    )
    with pytest.raises(DataPlatformMCPError) as excinfo:
        get_notebook_schedule("dupe")

    message = str(excinfo.value)
    assert "matches 2 schedules" in message
    assert "1" in message and "2" in message


def test_unknown_schedule_names_what_to_call_next(platform):
    platform([_schedule("nightly", "1")], [])
    with pytest.raises(DataPlatformMCPError, match="list_notebook_schedules"):
        get_notebook_schedule("nope")


def test_paused_schedules_say_so(platform):
    """A paused schedule is the usual reason a notebook-written table is stale."""
    platform([_schedule("nightly", "1", state="PAUSED")], [])
    result = get_notebook_schedule("nightly")

    assert result["state"] == "PAUSED"
    assert "PAUSED" in result["paused_note"]


def test_get_schedule_links_the_notebook_for_get_code_asset(platform):
    platform(
        [_schedule("nightly", "1", repo="repo-1")],
        [_run("nightly", "1", "JOB_STATE_FAILED", 1, "Error encountered")],
    )
    result = get_notebook_schedule("nightly")

    assert result["notebook_id"] == "repo-1"
    assert result["notebook"] == "nb_one"
    assert "get_code_asset" in result["note"]
    assert result["output_uri"] == "gs://bucket/nightly/"


def test_a_schedule_whose_notebook_was_deleted_is_flagged(platform):
    """Live history shows these failing every run with a Dataform 404."""
    platform([_schedule("nightly", "1", repo="gone")], [], notebooks={"repo-1": "nb"})
    result = list_notebook_schedules()

    assert result["schedules"][0]["notebook"] == "(notebook no longer exists)"


def test_notebook_names_are_optional(platform):
    """The Dataform quota failing must not take down a question about schedules."""
    platform([_schedule("nightly", "1")], [], notebooks={})
    result = list_notebook_schedules()

    assert result["count"] == 1
    assert result["schedules"][0]["notebook_id"] == "repo-1"


def test_non_notebook_schedules_are_excluded(platform):
    """Vertex AI also schedules pipeline jobs, which are not Colab notebooks."""
    platform(
        [_schedule("nightly", "1"), _schedule("pipeline", "2", notebook=False)],
        [],
    )
    result = list_notebook_schedules()

    assert [s["name"] for s in result["schedules"]] == ["nightly"]


def test_html_is_stripped_from_error_messages(platform):
    """One real failure message arrives as HTML with <b>, <br> and an <a>."""
    platform(
        [_schedule("nightly", "1")],
        [
            _run(
                "nightly",
                "1",
                "JOB_STATE_FAILED",
                1,
                "The <b>us-central1</b> region<br><ul><li>does not have "
                '<a href="http://x">enough</a></li></ul> resources.',
            )
        ],
    )
    error = list_notebook_runs(status="failed")["runs"][0]["error"]

    assert "<" not in error and ">" not in error
    assert "us-central1 region does not have enough resources." in error


def test_active_schedules_with_no_runs_are_called_out(platform):
    platform([_schedule("silent", "1")], [])
    result = list_notebook_schedules(lookback_days=30)

    assert result["active_but_no_runs"] == ["silent"]
    assert "not firing" in result["active_but_no_runs_note"]


def test_lookback_zero_skips_run_history(platform):
    session = platform([_schedule("nightly", "1")], [_run("n", "1", "JOB_STATE_FAILED", 1)])
    result = list_notebook_schedules(lookback_days=0)

    assert "health" not in result["schedules"][0]
    assert not any("notebookExecutionJobs" in url for url in session.requests)


def test_broken_schedules_sort_first(platform):
    platform(
        [_schedule("healthy", "1"), _schedule("broken", "2")],
        [
            _run("healthy", "1", "JOB_STATE_SUCCEEDED", 1),
            _run("broken", "2", "JOB_STATE_FAILED", 1, "boom"),
        ],
    )
    result = list_notebook_schedules()

    assert [s["name"] for s in result["schedules"]] == ["broken", "healthy"]


def test_state_and_name_filters(platform):
    platform(
        [_schedule("daily_one", "1"), _schedule("weekly_two", "2", state="PAUSED")],
        [],
    )
    assert [s["name"] for s in list_notebook_schedules(state="paused")["schedules"]] == [
        "weekly_two"
    ]
    assert [
        s["name"] for s in list_notebook_schedules(name_contains="DAILY")["schedules"]
    ] == ["daily_one"]


def test_permission_denied_names_the_third_role(platform):
    """aiplatform.viewer is separate from BigQuery, Transfer and Dataform."""
    platform([], [], fail_with=403)
    with pytest.raises(DataPlatformMCPError) as excinfo:
        list_notebook_schedules()

    message = str(excinfo.value)
    assert "roles/aiplatform.viewer" in message
    assert "add-iam-policy-binding" in message


def test_empty_region_warns_rather_than_concluding_none(platform):
    """A wrong region returns empty here, exactly as it does for code assets."""
    platform([], [])
    result = list_notebook_schedules()

    assert result["count"] == 0
    assert "regional" in result["note"]


def test_run_limit_is_reported_when_it_bites(platform):
    platform(
        [_schedule("nightly", "1")],
        [_run("nightly", "1", "JOB_STATE_FAILED", d, "boom") for d in range(10)],
    )
    result = list_notebook_runs(status="failed", limit=3)

    assert len(result["runs"]) == 3
    assert result["matched"] == 10
    assert "Showing 3 of 10" in result["truncated"]
