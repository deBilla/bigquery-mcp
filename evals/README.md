# Evaluation harness

Four layers, cheapest first. Only the first two run without credentials.

| Layer | Where | Needs |
| --- | --- | --- |
| Contract tests | `tests/` | nothing — an in-memory MCP client |
| Scorer tests | `tests/test_eval_scoring.py` | nothing — synthetic trajectories |
| Measurement run | `evals/measure.py` | live GCP credentials |
| Tool-use evals | `evals/tool_use_evals.py` | live GCP + the `claude` CLI + model tokens |

## Measurement run

```bash
BQ_PROJECT=your-project ./.venv/bin/python evals/measure.py
BQ_PROJECT=your-project ./.venv/bin/python evals/measure.py \
    --dataset events_raw --table events_click
```

Calls every tool through a real client session and records latency, serialised
payload size and an estimated token cost. It goes through the protocol rather
than calling functions directly, so what it measures is what a client actually
receives.

Output lands in `evals/measurements/` (git-ignored). **Those files contain live
table names and query results** — redact before using any of it as a fixture.

Run it after changing a tool's response shape. The numbers are the only way to
see the failures that look fine in code review: a truncation limit that fires on
almost every record, a field that is always empty, an identifier repeated three
times per row. `BASELINE.md` records what a run measured and what changed as a
result.

## Tool-use evals

```bash
./.venv/bin/python evals/tool_use_evals.py             # every case
./.venv/bin/python evals/tool_use_evals.py default-env # one case
./.venv/bin/python evals/tool_use_evals.py --list      # what each case is for
```

Asks the question a user would ask and checks what the agent did with it. The
client under test is the Claude Code CLI, because that is the client people
actually use — driving the API with a hand-rolled tool loop would evaluate a
harness nobody runs.

Scoring is on the **trajectory**: which tool, against which environment, with
which arguments. That is deterministic and cheap, and it catches the failure
that matters most — a question about one warehouse answered from another, which
no reader of the reply could detect. The server's own audit log *is* the
trajectory record, so the harness needs no instrumentation beyond pointing
`BQ_MCP_AUDIT_LOG` at a per-case file.

Some cases also assert on the reply text, because a trajectory cannot see
whether the agent called the right tool and then ignored what it said.

Each case spends real model tokens, so there are few of them on purpose.

### The cases

| Case | The failure it exists to catch |
| --- | --- |
| `default-env` | An unqualified question surveying environments it was not asked about |
| `named-env` | The `environment` argument being dropped, so everything lands on the default |
| `schema-before-query` | Guessing column names, and paying for the guess in scanned bytes |
| `no-self-confirm` | The agent setting `confirm_expensive=true` with no user to agree |
| `unpartitioned-table` | Trusting a column named `partition_date` over the table metadata |
| `stale-table` | Reporting a table that stopped being written to as current |

## **A suite that passes on its first run proves nothing**

An assertion weaker than the claim it makes passes for the wrong reason, and
the run looks identical either way. `tests/test_eval_scoring.py` feeds the
scorer the trajectories each case exists to reject — the wrong environment, a
query before a schema read, a self-approved spend, a confirmed assumption the
metadata contradicts, and a fluent answer with no tool call at all — and runs
offline for free. Read it before trusting a green run here.

That check is not hypothetical. The `default-env` case in the upstream server
this one borrows its structure from passed at first, because the assertion only
checked that the right environment appeared among those touched, while the
agent had queried two. The assertion was weaker than the claim. Tightened to
forbid the other environment, it failed reproducibly — and the fix was to the
server's instructions, not the test: they said which environment was used by
default, but never said not to survey the others.

That is the loop this layer exists for. **Tool and server descriptions are the
highest-leverage thing you can change, and nothing except an eval tells you they
need changing.**
