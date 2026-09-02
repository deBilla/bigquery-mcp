# BigQuery MCP

[![CI](https://github.com/deBilla/bigquery-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/deBilla/bigquery-mcp/actions/workflows/ci.yml)

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server over
Google BigQuery. It lets an AI client (Claude Code, Claude Desktop, …) answer
plain-language data questions by discovering schema and running `SELECT` queries.

The AI does the natural-language → SQL translation; this server just safely
executes against BigQuery under **your own** Google credentials.

- **Repo:** https://github.com/deBilla/bigquery-mcp
- **Clone (SSH):** `git@github.com:deBilla/bigquery-mcp.git`

---

## Tools exposed

| Tool | Purpose | Cost |
|------|---------|------|
| `list_datasets` | List datasets in the project | free |
| `list_tables` | List tables/views in a dataset | free |
| `get_table_schema` | Columns (nested paths expanded), **partitioning**, size, row count | free |
| `check_table_freshness` | When each table was last written — catches stale sources | free |
| `run_query` | Run a validated, read-only `SELECT` and return rows | scans data |

Only `run_query` costs anything, so the discovery tools are the ones to spend
first. Two of them exist to prevent specific, repeated mistakes:

- **`get_table_schema` reports partitioning from table metadata, never from
  column names.** A table with a `partition_date` column may not be partitioned
  — in which case no `WHERE` clause reduces the scan and every query reads the
  whole table. The response flags this explicitly when the table is large.
- **`check_table_freshness` finds tables that stopped being written to** without
  being dropped. Those return stale data rather than an error, which is the
  failure mode nobody notices.

---

## Quick start (per user)

Each person runs their own local copy. Queries execute under **their own**
BigQuery/IAM permissions, so existing access controls decide who can see what.
You need Python 3.11+ and the `gcloud` CLI installed.

### 1. Install

The package is published as **`data-platform-mcp`** (`bigquery-mcp` was already
taken on PyPI by an unrelated project). Once a release is tagged, no checkout is
needed — the client can fetch and run it directly:

```bash
uvx data-platform-mcp --version
```

> **Not yet published.** No version tag has been pushed, so use the source
> install below until one is. See [Releasing](#releasing).

**From source:**

```bash
git clone git@github.com:deBilla/bigquery-mcp.git
cd bigquery-mcp

python3 -m venv .venv
./.venv/bin/pip install -e .
```

Either way you get a `data-platform-mcp` command, which is what the client runs.

### 2. Authenticate to Google (one time)

Uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).
Run this once; queries then execute as you.

```bash
gcloud auth application-default login
```

> Your account needs **BigQuery Data Viewer** + **BigQuery Job User** on the
> project you intend to query.

### 3. Register with your AI client

Replace `your-gcp-project` with your GCP project ID.

**Claude Code** — once published:

```bash
claude mcp add bigquery \
  --env BQ_PROJECT=your-gcp-project \
  -- uvx data-platform-mcp
```

From a source install, point at the checkout instead (replace
`/abs/path/bigquery-mcp`):

```bash
claude mcp add bigquery \
  --env BQ_PROJECT=your-gcp-project \
  -- /abs/path/bigquery-mcp/.venv/bin/data-platform-mcp
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "/abs/path/bigquery-mcp/.venv/bin/data-platform-mcp",
      "args": [],
      "env": { "BQ_PROJECT": "your-gcp-project" }
    }
  }
}
```

### 4. Restart the client and ask a question

> "Which datasets are available? In the `sales` dataset, how many rows does the
> `orders` table have?"

---

## Safety

- Every query is **dry-run first** to validate it and estimate bytes scanned.
- **Only `SELECT` / `WITH`** statements run — no writes, DDL, or DML.
- **Cost confirmation:** a query estimated to scan more than `BQ_WARN_BYTES`
  (default 1 GB) does **not** run. It returns `status: "confirmation_required"`
  with the estimated scan size and dollar cost so the client can ask before
  proceeding. Re-call with `confirm_expensive=true` to run it.
- **Hard cap:** queries above `BQ_MAX_BYTES_BILLED` (default 5 GB) never run,
  even with confirmation — a runaway-cost backstop.
- Optional **dataset allowlist** restricts what can be read.
- **Refusals are protocol errors.** Anything the server declines to do — a
  non-`SELECT` statement, a disallowed dataset, a query over the hard cap —
  arrives with MCP's `isError` set, so it cannot be mistaken for a result.
  `confirmation_required` is the deliberate exception: it is a normal result,
  because the agent is meant to relay it and come back.
- **Responses are size-bounded.** `run_query` stops adding rows once the
  serialised response reaches ~40k characters and sets `stopped_for_size`, so a
  wide result cannot quietly consume the whole context window. A partial answer
  always says that it is partial.
- **SQL is never written to the audit log** — only a hash and a length. Query
  text routinely contains the user IDs or emails it filters on.

### Cost-confirmation flow

```
run_query(sql)
   │  dry run estimates the scan
   ├── ≤ 1 GB ........... runs, returns rows + estimated_cost_usd
   ├── 1–5 GB .......... status: confirmation_required (size + $ estimate) → ask user
   │                      → run_query(sql, confirm_expensive=true) runs it
   └── > 5 GB ........... rejected, never runs
```

---

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `BQ_PROJECT` | _(ADC project)_ | GCP project ID whose BigQuery datasets you query. Falls back to the project associated with your credentials; tools error with instructions if neither is set. |
| `BQ_LOCATION` | `US` | BigQuery location |
| `BQ_WARN_BYTES` | `1073741824` (1 GB) | Above this, ask the user to confirm before running |
| `BQ_MAX_BYTES_BILLED` | `5368709120` (5 GB) | Hard per-query scan cap — never exceeded |
| `BQ_COST_PER_TIB_USD` | `6.25` | On-demand price used to render the cost estimate |
| `BQ_ROW_LIMIT` | `200` | Default rows returned |
| `BQ_DATASET_ALLOWLIST` | _(empty = all)_ | Comma-separated dataset IDs |
| `BQ_MCP_TRANSPORT` | `stdio` | `stdio` (subprocess) or `http`/`sse` (serve over network) |
| `BQ_MCP_HOST` | `127.0.0.1` | Bind host when transport is `http`/`sse`. `run-http.sh` overrides this to `0.0.0.0` so containers can reach it — see the security note below. |
| `BQ_MCP_PORT` | `8765` | Bind port when transport is `http`/`sse` |
| `BQ_MCP_AUDIT_LOG` | `~/.local/state/data-platform-mcp/audit.jsonl` | JSONL record of every tool call. `off` disables it. SQL text is never written — only a hash and length. |
| `BQ_MCP_LOG_LEVEL` | `INFO` | Verbosity of the stderr log |

By default the server speaks **stdio** — the right choice when a client spawns
it (Claude Code, Claude Desktop), and what the Quick start above uses.

---

## Advanced: serve over HTTP

To reach the server from a **remote or containerized** client instead of having
each client spawn its own, run it over HTTP:

```bash
BQ_PROJECT=your-gcp-project ./run-http.sh
# Serving … on http://0.0.0.0:8765/mcp
```

Clients then connect by URL (Claude Code):

```bash
claude mcp add --transport http bigquery http://<host>:8765/mcp
```

> ⚠️ **Security:** the HTTP endpoint has **no authentication**, and every query
> runs under the **host's** ADC credentials — not the connecting user's. Anyone
> who can reach the port gets full read access to `BQ_PROJECT` under your
> identity. Only expose it on a trusted network (bind `BQ_MCP_HOST=127.0.0.1`
> and use an SSH tunnel/VPN, or an authenticating proxy). See
> [docs/nanoclaw.md](docs/nanoclaw.md) for the containerized-client setup this
> mode was designed for.

For server deployments, point `GOOGLE_APPLICATION_CREDENTIALS` at a
service-account key with BigQuery Data Viewer + Job User roles instead of using
personal ADC.

---

## Development

```bash
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

The suite needs **no credentials and no network** — every test runs against
fakes in `tests/conftest.py`, so it is deterministic and free. Layers:

| File | Covers |
|------|--------|
| `test_protocol.py` | The MCP contract through a real in-memory client session: tool set, read-only annotations, generated schemas, `isError` on refusal |
| `test_query_guard.py` | The cost gate — what runs, what is refused, what is handed back to the user, and what the caller is told about limits |
| `test_payload_shape.py` | Response shapes against fake tables, including the partitioning trap and nested-field flattening |
| `test_observability.py` | The audit trail, and the promise that SQL text never reaches it |
| `test_config.py` | Settings resolution, and the missing-project error that used to be an import-time crash |
| `test_errors.py` | Auth failures carry the command that fixes them |
| `test_formatting.py` | The size and cost figures a user is asked to approve |

A suite that passes on its first run proves nothing, so the guarantees above
were checked by breaking them: reverting refusals to error-shaped returns,
logging raw SQL, guessing partitioning from column names, removing the response
budget, dropping `functools.wraps` from the audit wrapper, letting confirmation
bypass the hard cap, silencing stale-table detection, and removing the
allowlist check. Each one fails the suite.

## Releasing

Version numbers live in two files and CI refuses a tag where they disagree — a
mismatch would ship a tag pointing at different code than the package claims.
(`__version__` is read from the installed distribution, so it cannot drift.)

```bash
# 1. bump both to the same value
#      pyproject.toml   project.version
#      server.json      version  AND  packages[0].version

# 2. tag and push
git tag v0.2.0 && git push origin v0.2.0
```

The tag triggers `.github/workflows/release.yml`, which verifies the versions
agree, builds, publishes to PyPI via **Trusted Publishing**, then registers the
release with the **MCP registry**. Neither step stores a token: PyPI uses OIDC
from this repository and the `pypi` environment, and the registry uses GitHub
OIDC. Both need one-time setup before the first release:

- **PyPI:** add a trusted publisher at
  <https://pypi.org/manage/account/publishing/> for repository
  `deBilla/bigquery-mcp`, workflow `release.yml`, environment `pypi`.
- **GitHub:** create the `pypi` environment in repository settings.

### What CI checks

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Checks |
|-----|--------|
| `test` | The suite on Python 3.11, 3.12 and 3.13 — with no GCP credentials on the runner, which is the point |
| `safety` | No credential-shaped strings in tracked files; `.env`/`.mcp.json` untracked; **no mutating BigQuery client calls anywhere in `src/`** |
| `package` | Builds, `twine check`s, asserts no local config leaked into the sdist, then installs the wheel into a clean venv and drives the real protocol — 5 tools, every one annotated read-only and documented, instructions intact |

The last one is the important one: it catches a package that installs cleanly
and dies on its first request, which is a failure no unit test sees.

## License

MIT — see [LICENSE](LICENSE).
