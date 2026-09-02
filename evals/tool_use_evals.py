#!/usr/bin/env python3
"""Ask the question a user would ask, and check what the agent did with it.

The client under test is the Claude Code CLI, because that is the client people
actually use -- driving the API with a hand-rolled tool loop would evaluate a
harness nobody runs.

The server's own audit log *is* the trajectory record, so this needs no
instrumentation beyond pointing ``BQ_MCP_AUDIT_LOG`` at a per-case file. What
gets scored is which tool ran, against which environment, with which arguments.

Usage:
    python evals/tool_use_evals.py                 # every case
    python evals/tool_use_evals.py default-env     # one case
    python evals/tool_use_evals.py --list

Needs live GCP credentials and the `claude` CLI, and spends real model tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scoring import CASES, Case, score  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).resolve().parent / "results"
SERVER = ROOT / ".venv" / "bin" / "data-platform-mcp"

# Every tool this server exposes, named the way the client addresses them.
TOOLS = [
    "list_environments", "list_datasets", "list_tables",
    "get_table_schema", "check_table_freshness", "run_query",
]
SERVER_NAME = "evalbq"
ALLOWED = ",".join(f"mcp__{SERVER_NAME}__{t}" for t in TOOLS)


def build_config(case: Case, audit: Path, config_path: Path) -> None:
    """Write an MCP config that starts this server with the case's settings."""
    env = {
        "BQ_MCP_AUDIT_LOG": str(audit),
        "BQ_MCP_ENVIRONMENTS": json.dumps(case.environments),
        **case.env,
    }
    config_path.write_text(json.dumps({
        "mcpServers": {
            SERVER_NAME: {"command": str(SERVER), "args": [], "env": env}
        }
    }))


def read_trajectory(audit: Path) -> list[dict]:
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]


def run_case(case: Case, timeout: int = 300) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    audit = RESULTS / f"{case.name}.audit.jsonl"
    config = RESULTS / f"{case.name}.mcp.json"
    audit.unlink(missing_ok=True)
    build_config(case, audit, config)

    command = [
        "claude", "-p", case.prompt,
        "--mcp-config", str(config),
        # Without this the developer's own MCP servers join the session, and
        # this server would be present twice under different configuration.
        "--strict-mcp-config",
        "--allowedTools", ALLOWED,
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            cwd=str(ROOT),
        )
        reply, error = completed.stdout.strip(), completed.stderr.strip()
    except subprocess.TimeoutExpired:
        reply, error = "", f"timed out after {timeout}s"
    elapsed = time.perf_counter() - started

    trajectory = read_trajectory(audit)
    failures = score(case, trajectory, reply)
    if error and not reply:
        failures.insert(0, f"the client produced no reply: {error[:200]}")

    return {
        "case": case.name,
        "passed": not failures,
        "failures": failures,
        "seconds": round(elapsed, 1),
        "calls": [
            {
                "tool": r.get("tool"),
                "environment": r.get("environment"),
                "arguments": r.get("arguments"),
                "error": r.get("error"),
            }
            for r in trajectory
        ],
        "reply": reply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="cases to run (default: all)")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help=(
            "re-score the saved replies from the last run instead of asking "
            "again. Tightening an assertion is the common reason to re-run, "
            "and it needs no model tokens to check."
        ),
    )
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.list:
        for case in CASES:
            print(f"{case.name}\n    {case.why}\n")
        return 0

    selected = [c for c in CASES if not args.names or c.name in args.names]
    unknown = set(args.names) - {c.name for c in CASES}
    if unknown:
        print(f"unknown case(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    if not SERVER.exists():
        print(f"server not found at {SERVER}; run: pip install -e .", file=sys.stderr)
        return 2

    saved = {}
    if args.rescore:
        path = RESULTS / "last-run.json"
        if not path.exists():
            print(f"no saved run at {path}", file=sys.stderr)
            return 2
        saved = {r["case"]: r for r in json.loads(path.read_text())}

    results = []
    for case in selected:
        print(f"--- {case.name} ---", flush=True)
        if args.rescore:
            previous = saved.get(case.name)
            if previous is None:
                print(f"[skip] {case.name} not in the saved run\n")
                continue
            result = dict(previous)
            result["failures"] = score(case, previous["calls"], previous["reply"])
            result["passed"] = not result["failures"]
        else:
            result = run_case(case, timeout=args.timeout)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {case.name} ({result['seconds']}s)")
        for call in result["calls"]:
            env = call["environment"] or "-"
            note = f"  !{call['error']}" if call["error"] else ""
            print(f"    {call['tool']:<24} env={env}{note}")
        for failure in result["failures"]:
            print(f"    ✗ {failure}")
        print(flush=True)

    if not args.rescore:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "last-run.json").write_text(json.dumps(results, indent=2))

    passed = sum(1 for r in results if r["passed"])
    print(f"{passed}/{len(results)} cases passed")
    print(f"full transcripts: {RESULTS / 'last-run.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
