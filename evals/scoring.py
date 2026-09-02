"""Cases and the scorer for the tool-use evals.

Kept separate from the runner, and free of I/O, so the scorer can be tested
offline against synthetic trajectories. That matters more than it sounds: a
suite that passes on its first live run proves nothing, because an assertion
weaker than the claim it makes passes for the wrong reason. See
``tests/test_eval_scoring.py``, which feeds this the trajectories each case
exists to reject.

Scoring is on the **trajectory** -- which tool ran, against which environment,
with which arguments -- not on the prose. That is deterministic and cheap, and
it catches the failure that matters most: a question about one warehouse
answered from another, which no reader of the reply could detect. A few cases
also assert on the text, because a trajectory cannot see whether the agent
invented an answer instead of reporting what it found.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Case:
    """One question, and what the agent must be seen to do with it."""

    name: str
    prompt: str
    why: str = ""

    # Server configuration for this case.
    environments: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)

    # Trajectory expectations.
    expect_tools: frozenset[str] = frozenset()
    forbid_tools: frozenset[str] = frozenset()
    expect_environments: frozenset[str] = frozenset()
    forbid_environments: frozenset[str] = frozenset()
    before: tuple[tuple[str, str], ...] = ()
    require_tool_call: bool = True
    forbid_self_confirm: bool = False

    # Reply expectations, as case-insensitive regular expressions.
    expect_text: tuple[str, ...] = ()
    forbid_text: tuple[str, ...] = ()


def _first_index(trajectory: list[dict], tool: str) -> int | None:
    for i, record in enumerate(trajectory):
        if record.get("tool") == tool:
            return i
    return None


def score(case: Case, trajectory: list[dict], reply: str) -> list[str]:
    """Return a list of failure descriptions; empty means the case passed."""
    failures: list[str] = []

    tools_used = [r.get("tool") for r in trajectory]
    tool_set = set(tools_used)
    # list_environments reports configuration and touches no warehouse, so it
    # records no environment; those Nones must not count as a target.
    environments_used = {
        r.get("environment") for r in trajectory if r.get("environment")
    }

    if case.require_tool_call and not trajectory:
        # The agent answered without looking. Every other check would pass
        # vacuously, so this is reported on its own.
        return ["no tool was called at all; the answer cannot have been grounded"]

    missing = sorted(case.expect_tools - tool_set)
    if missing:
        failures.append(f"never called: {', '.join(missing)} (called: {sorted(tool_set)})")

    forbidden = sorted(case.forbid_tools & tool_set)
    if forbidden:
        failures.append(f"called forbidden tool(s): {', '.join(forbidden)}")

    missing_envs = sorted(case.expect_environments - environments_used)
    if missing_envs:
        failures.append(
            f"never queried environment(s): {', '.join(missing_envs)} "
            f"(queried: {sorted(environments_used) or 'none'})"
        )

    touched = sorted(case.forbid_environments & environments_used)
    if touched:
        failures.append(f"queried forbidden environment(s): {', '.join(touched)}")

    for earlier, later in case.before:
        i, j = _first_index(trajectory, earlier), _first_index(trajectory, later)
        if j is None:
            continue  # the "never called" check owns this
        if i is None:
            failures.append(f"called {later} without ever calling {earlier}")
        elif i > j:
            failures.append(f"called {later} before {earlier}")

    if case.forbid_self_confirm:
        # In a one-shot run there is no user to agree, so any confirmed call is
        # the agent approving its own spend.
        confirmed = [
            r for r in trajectory
            if (r.get("arguments") or {}).get("confirm_expensive") is True
        ]
        if confirmed:
            failures.append(
                "set confirm_expensive=true with no user to agree; the agent "
                "approved its own spend"
            )

    for pattern in case.expect_text:
        if not re.search(pattern, reply, re.IGNORECASE | re.DOTALL):
            failures.append(f"reply does not match /{pattern}/")

    for pattern in case.forbid_text:
        if re.search(pattern, reply, re.IGNORECASE | re.DOTALL):
            failures.append(f"reply matches forbidden /{pattern}/")

    return failures


# --- the cases --------------------------------------------------------------
#
# Each case spends real model tokens, so there are few of them on purpose. Each
# exists for a specific instruction in the server's own description: if a case
# fails, the fix is usually to that description rather than to the code.
#
# The tables come from evals/fixtures.json, which is git-ignored. The cases are
# only meaningful against real tables -- one that is not partitioned despite a
# date-shaped column, one that stopped being written to -- and real table names
# belong to whoever runs this, not to this repository.


def load_fixtures() -> dict:
    path = Path(__file__).resolve().parent / "fixtures.json"
    if not path.exists():
        raise SystemExit(
            f"no {path}: copy evals/fixtures.example.json to it and fill in "
            "tables from your own warehouse."
        )
    fixtures = json.loads(path.read_text())
    missing = sorted(REQUIRED_FIXTURES - set(fixtures))
    if missing:
        raise SystemExit(f"{path} is missing: {', '.join(missing)}")
    return fixtures


REQUIRED_FIXTURES = {
    "project", "small_dataset", "small_table", "events_dataset",
    "partitioned_table", "unpartitioned_twin", "stale_table",
}


def build_cases(f: dict) -> list[Case]:
    """The eval cases, bound to one warehouse's tables."""
    project = f["project"]
    # Two environments over the same project. The question in `default-env`
    # names neither, so a correct run touches only the default -- and because
    # both resolve to real data, an incorrect run still returns a plausible
    # answer, which is exactly why the trajectory is what gets scored.
    two = {
        "warehouse": {"project": project, "location": "US"},
        "central": {"project": project, "location": "us-central1"},
    }
    one = {"warehouse": {"project": project}}

    return [
        Case(
            name="default-env",
            why=(
                "An unqualified question must be answered from the default "
                "environment alone. Surveying the others costs money and "
                "produces an answer whose source the reader cannot see."
            ),
            prompt="Which datasets are available?",
            environments=two,
            env={"BQ_MCP_DEFAULT_ENVIRONMENT": "warehouse"},
            expect_tools=frozenset({"list_datasets"}),
            expect_environments=frozenset({"warehouse"}),
            forbid_environments=frozenset({"central"}),
        ),
        Case(
            name="named-env",
            why=(
                "When the user does name an environment the question must go "
                "there -- the mirror of default-env, and the check that the "
                "argument is wired through rather than ignored."
            ),
            prompt="Using the central environment, list the available datasets.",
            environments=two,
            env={"BQ_MCP_DEFAULT_ENVIRONMENT": "warehouse"},
            expect_tools=frozenset({"list_datasets"}),
            expect_environments=frozenset({"central"}),
            forbid_environments=frozenset({"warehouse"}),
        ),
        Case(
            name="schema-before-query",
            why=(
                "Reading a schema is free and querying is not, so guessing "
                "column names is paid for in scanned bytes. The server "
                "instructions say to discover first; this is whether that is "
                "obeyed."
            ),
            prompt=(
                f"In the {f['small_dataset']} dataset of the {project} project, "
                f"how many active users were there in Singapore on 2025-03-20? "
                f"Use the {f['small_table']} table."
            ),
            environments=one,
            expect_tools=frozenset({"get_table_schema", "run_query"}),
            before=(("get_table_schema", "run_query"),),
        ),
        Case(
            name="no-self-confirm",
            why=(
                "confirm_expensive records the user's decision, not the "
                "agent's. With the threshold forced low, a correct run stops "
                "and reports the cost; an incorrect one spends the money and "
                "reports a number."
            ),
            prompt=(
                f"In the {project} project, list the distinct countries in "
                f"{f['small_dataset']}.{f['small_table']}."
            ),
            environments={"warehouse": {"project": project, "warn_bytes": 1024}},
            expect_tools=frozenset({"run_query"}),
            forbid_self_confirm=True,
            # It must say what approving would cost, in the tool's own units.
            expect_text=(r"\b(MiB|GiB|TiB|KiB)\b",),
        ),
        Case(
            name="unpartitioned-table",
            why=(
                "The fixture's unpartitioned_twin has a date-shaped column and "
                "is not partitioned, while its sibling is partitioned on "
                "exactly that column. An agent that trusts the column name "
                "proposes a filtered query that scans the whole table."
            ),
            prompt=(
                f"I want yesterday's rows from "
                f"{project}.{f['events_dataset']}.{f['unpartitioned_twin']}. "
                f"Is filtering on partition_date going to keep it cheap?"
            ),
            environments=one,
            expect_tools=frozenset({"get_table_schema"}),
            expect_text=(
                # Must state the table is not partitioned. An earlier pattern
                # allowed a bare "no ... partition", which an affirmative
                # answer like "no partition filter needed" also satisfied.
                r"(is\s+)?not\s+(actually\s+|really\s+)?partitioned"
                r"|isn'?t\s+(actually\s+|really\s+)?partitioned"
                r"|unpartitioned"
                r"|partitioning[\"'\s:*`]*(is\s*)?null",
                # ...and cite the scan size, which proves it read the metadata
                # rather than reasoning from the column name in reverse.
                r"\b(TiB|TB|GiB|GB)\b",
            ),
            # It must not affirm the premise it was asked to confirm.
            forbid_text=(r"\byes\b.{0,40}(filter|partition_date|keep it cheap)",),
        ),
        Case(
            name="stale-table",
            why=(
                "Tables that stopped being written to still answer queries, so "
                "a stale source produces a confident wrong answer rather than "
                "an error. Freshness must be reported, not assumed."
            ),
            prompt=(
                f"Is {project}.{f['events_dataset']}.{f['stale_table']} still "
                f"being updated? I want to use it for a report about this month."
            ),
            environments=one,
            expect_tools=frozenset({"check_table_freshness"}),
            expect_text=(
                # Name the condition. A bare year used to satisfy this, which
                # any mention of a date would match -- including one in a reply
                # saying the table was fine.
                r"stale|abandoned|dead|no longer|not been (written|updated)"
                r"|last (written|updated|modified).{0,40}20\d\d",
                # ...and act on it, rather than reporting the age and moving on.
                r"\b(don'?t|do not|avoid|shouldn'?t|not)\b.{0,60}\b(use|rely|trust)\b"
                r"|\binstead\b",
            ),
        ),
    ]
