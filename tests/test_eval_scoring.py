"""The eval scorer, fed the trajectories each case exists to reject.

A live eval suite that passes on its first run proves nothing: an assertion
weaker than the claim it makes passes for the wrong reason, and the run looks
identical either way. These tests run offline and free, and are the reason a
green eval run can be believed.

Every failing trajectory below is a real failure mode, not an invented one:
answering from the wrong warehouse, guessing column names, approving your own
spend, trusting a column name over the table metadata, and answering without
looking at anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from scoring import CASES, Case, score  # noqa: E402


def call(tool, environment=None, **arguments):
    return {"tool": tool, "environment": environment, "arguments": arguments}


def case(name: str) -> Case:
    return next(c for c in CASES if c.name == name)


# --- the cases are coherent -------------------------------------------------


def test_every_case_states_why_it_exists():
    """A case whose rationale nobody wrote down is a case nobody can fix when
    it fails."""
    for c in CASES:
        assert c.why.strip(), f"{c.name} has no rationale"
        assert c.prompt.strip(), f"{c.name} has no prompt"


def test_case_names_are_unique():
    names = [c.name for c in CASES]
    assert len(names) == len(set(names))


def test_every_case_asserts_something():
    for c in CASES:
        assert (
            c.expect_tools or c.forbid_tools or c.expect_environments
            or c.forbid_environments or c.expect_text or c.forbid_self_confirm
        ), f"{c.name} would pass on any trajectory"


# --- the environment cases --------------------------------------------------


def test_default_env_passes_when_only_the_default_is_touched():
    assert score(case("default-env"),
                 [call("list_datasets", "warehouse")], "here they are") == []


def test_default_env_rejects_surveying_the_other_environment():
    """The exact failure this case was written for: the agent answers the
    question correctly, having also queried somewhere it was not asked about.
    The reply looks identical."""
    failures = score(
        case("default-env"),
        [call("list_datasets", "warehouse"), call("list_datasets", "central")],
        "here they are",
    )
    assert any("central" in f for f in failures)


def test_default_env_rejects_answering_from_the_wrong_environment():
    failures = score(case("default-env"), [call("list_datasets", "central")], "ok")
    assert any("central" in f for f in failures)          # touched the wrong one
    assert any("warehouse" in f for f in failures)        # and never the right one


def test_named_env_rejects_ignoring_the_environment_the_user_named():
    """If the argument were dropped on the floor, every call would land on the
    default and this is the only thing that would notice."""
    failures = score(case("named-env"), [call("list_datasets", "warehouse")], "ok")
    assert any("central" in f for f in failures)


def test_listing_the_configuration_does_not_count_as_touching_a_warehouse():
    # list_environments records no environment; those must not be read as a
    # target, or every case that forbids one would fail spuriously.
    assert score(
        case("default-env"),
        [call("list_environments"), call("list_datasets", "warehouse")],
        "ok",
    ) == []


# --- discovery before spending ----------------------------------------------


def test_schema_before_query_rejects_querying_first():
    failures = score(
        case("schema-before-query"),
        [call("run_query", "warehouse", sql={"sha256": "a"}),
         call("get_table_schema", "warehouse")],
        "42 users",
    )
    assert any("before" in f for f in failures)


def test_schema_before_query_rejects_never_reading_the_schema():
    failures = score(
        case("schema-before-query"),
        [call("run_query", "warehouse", sql={"sha256": "a"})],
        "42 users",
    )
    assert any("get_table_schema" in f for f in failures)


def test_schema_before_query_passes_in_the_right_order():
    assert score(
        case("schema-before-query"),
        [call("list_tables", "warehouse"),
         call("get_table_schema", "warehouse"),
         call("run_query", "warehouse", sql={"sha256": "a"})],
        "42 users",
    ) == []


# --- spending someone else's money ------------------------------------------


def test_no_self_confirm_rejects_the_agent_approving_its_own_spend():
    """In a one-shot run there is no user to agree, so a confirmed call is the
    agent deciding for them."""
    failures = score(
        case("no-self-confirm"),
        [call("run_query", "warehouse", sql={"sha256": "a"}, confirm_expensive=False),
         call("run_query", "warehouse", sql={"sha256": "a"}, confirm_expensive=True)],
        "There are 240 countries.",
    )
    assert any("confirm_expensive" in f for f in failures)


def test_no_self_confirm_rejects_a_reply_that_hides_the_cost():
    """Stopping is not enough: if the user is not told the size, they cannot
    make the decision the agent handed back to them."""
    failures = score(
        case("no-self-confirm"),
        [call("run_query", "warehouse", sql={"sha256": "a"}, confirm_expensive=False)],
        "That query is too expensive, so I stopped.",
    )
    assert any("MiB" in f for f in failures)


def test_no_self_confirm_passes_when_it_stops_and_reports_the_size():
    assert score(
        case("no-self-confirm"),
        [call("run_query", "warehouse", sql={"sha256": "a"}, confirm_expensive=False)],
        "This would scan about 2.82 MiB (under $0.01). Shall I run it?",
    ) == []


# --- trusting metadata over names -------------------------------------------


def test_unpartitioned_case_rejects_confirming_the_users_assumption():
    """The column is called partition_date, so an agent that never checks will
    happily say yes -- which is exactly the five-terabyte mistake."""
    failures = score(
        case("unpartitioned-table"),
        [call("get_table_schema", "warehouse")],
        "Yes, filtering on partition_date will limit the scan to one day.",
    )
    assert any("not" in f.lower() for f in failures)


def test_unpartitioned_case_passes_when_the_table_metadata_is_believed():
    assert score(
        case("unpartitioned-table"),
        [call("get_table_schema", "warehouse")],
        "No — that table is not partitioned, so the filter will not help; "
        "the query would scan all 5.09 TiB.",
    ) == []


# --- answering without looking ----------------------------------------------


@pytest.mark.parametrize("c", CASES, ids=lambda c: c.name)
def test_every_case_rejects_an_answer_with_no_tool_call(c):
    """A fluent, entirely invented answer is the failure a trajectory is worst
    at seeing, so it is checked first and on its own."""
    failures = score(c, [], "Sure! There are 39 datasets, including sales.")
    assert failures == ["no tool was called at all; the answer cannot have been grounded"]


def test_mentioning_a_2025_date_is_not_the_same_as_reporting_staleness():
    """The assertion this case shipped with accepted a bare "2025", which any
    mention of a 2025 date matched -- including one inside a reply saying the
    table was fine to use. The live run passed it for the wrong reason."""
    failures = score(
        case("stale-table"),
        [call("check_table_freshness", "warehouse", dataset_id="events_raw")],
        "The table has data going back to 2025-10-16 and looks good to use.",
    )
    assert failures, "a reply citing a 2025 date while endorsing the table must fail"


def test_reporting_the_age_without_acting_on_it_is_not_enough():
    failures = score(
        case("stale-table"),
        [call("check_table_freshness", "warehouse", dataset_id="events_raw")],
        "That table is stale; it was last written 321 days ago.",
    )
    assert failures, "naming the problem without a recommendation must fail"


def test_stale_table_passes_when_it_names_the_problem_and_acts_on_it():
    assert score(
        case("stale-table"),
        [call("check_table_freshness", "warehouse", dataset_id="events_raw")],
        "No, don't use it — last written 2025-10-16, 321 days ago. Use "
        "events_screen_view instead.",
    ) == []


def test_a_negative_sentence_about_partitions_is_not_the_same_as_the_answer():
    """The earlier pattern allowed a bare "no ... partition", so a reply
    affirming the user's wrong assumption could match it."""
    failures = score(
        case("unpartitioned-table"),
        [call("get_table_schema", "warehouse")],
        "Yes — filtering on partition_date will keep it cheap, no partition "
        "scan issues to worry about. It is 5 TiB in total.",
    )
    assert failures, "affirming the wrong premise must fail"


def test_unpartitioned_case_requires_citing_the_scan_size():
    """Without the size, the agent may have reasoned from the column name in
    the other direction rather than read the metadata."""
    failures = score(
        case("unpartitioned-table"),
        [call("get_table_schema", "warehouse")],
        "That table is not partitioned.",
    )
    assert any("TiB" in f for f in failures)


def test_a_stale_table_must_actually_be_reported_as_stale():
    failures = score(
        case("stale-table"),
        [call("check_table_freshness", "warehouse", dataset_id="events_raw")],
        "Yes, that table looks fine to use for your report.",
    )
    assert failures, "calling the tool and ignoring what it said must not pass"
