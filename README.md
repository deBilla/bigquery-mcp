# BigQuery MCP

[![CI](https://github.com/deBilla/bigquery-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/deBilla/bigquery-mcp/actions/workflows/ci.yml)

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server over
Google BigQuery. It lets an AI client (Claude Code, Claude Desktop, …) answer
plain-language data questions by discovering schema and running `SELECT` queries.

The AI does the natural-language → SQL translation; this server just safely
executes against BigQuery under **your own** Google credentials.

- **Docs:** https://debilla.github.io/bigquery-mcp/
- **Repo:** https://github.com/deBilla/bigquery-mcp
- **PyPI:** [`data-platform-mcp`](https://pypi.org/project/data-platform-mcp/)
- **MCP registry:** mcp-name: io.github.deBilla/data-platform-mcp

---

## Tools exposed

| Tool | Purpose | Cost |
|------|---------|------|
| `list_datasets` | List datasets in the project | free |
| `list_tables` | List tables/views in a dataset | free |
| `get_table_schema` | Columns (nested paths expanded), **partitioning**, size, row count | free |
| `check_table_freshness` | When each table was last written — catches stale sources | free |
| `list_environments` | Which BigQuery environments are configured, and the default | free |
| `list_scheduled_queries` | Which scheduled query writes a table, and whether it is disabled or failing | free |
| `get_scheduled_query` | One query's SQL, destination and recent runs | free |
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
- **`list_scheduled_queries` says why.** A stale table is usually a scheduled
  query that was disabled or is failing, and that lives in a different API
  (BigQuery Data Transfer) needing `roles/bigquerydatatransfer.viewer`. Without
  that role the two tools return an error naming it and everything else works
  normally. Most scheduled queries declare no destination because they write
  with DDL, so the target is read out of the SQL and reported as
  `writes_to_from_sql` — a heuristic, labelled as one.

---

## Environments

One server answers questions about several targets — a warehouse and its
staging copy, or two regions of the same project. Every tool takes an optional
`environment`; omitting it uses the default.

```toml
# ~/.config/data-platform-mcp/config.toml
default_environment = "warehouse"

[environments.warehouse]
project = "my-data-platform"
impersonate = "data-platform-mcp-ro@my-data-platform.iam.gserviceaccount.com"
dataset_allowlist = ["sales", "events"]

[environments.central]          # same project, different region
project = "my-data-platform"
location = "us-central1"
```

See [`config.toml.example`](config.toml.example) for every setting, or set
`BQ_MCP_ENVIRONMENTS` to the same structure as JSON. **A single `BQ_PROJECT`
still works unchanged** — it becomes one environment named `default`.

An environment can be named by its own name, an alias, the built-in shorthands
(`prod`, `stg`, `dev`, `live`) or its project id. An **unknown** name is an
error naming the valid options, never a silent fall back to the default: a typo
that answered a production question from staging would be invisible in the
reply. Every result echoes back the environment it came from.

Regions are why this matters most here. BigQuery cannot query across locations,
and its error for trying names neither location, so it reads as a missing
table. One environment per location; `doctor` reports which datasets are where.

---

## Read-only as a property of the identity

The SELECT-only guard and the `readOnlyHint` annotations are promises about
this code. Pointing the server at a service account that holds only
`roles/bigquery.jobUser` and a dataset-scoped `roles/bigquery.dataViewer` makes
it a fact about the credentials — enforced by IAM whatever the code does, and
whatever your own roles allow:

```bash
data-platform-mcp setup --project my-data-platform --datasets sales,events
```

Creates the account, grants those two roles, and gives you
`roles/iam.serviceAccountTokenCreator` on it so the server can impersonate it.
Add `--dry-run` to see the commands first; it is safe to re-run.

With `--datasets`, the dataset allowlist stops being an `if` statement in this
process and becomes a grant Google enforces.

---

## macOS setup

**Terminal.app is not Xcode.** It ships with every Mac. What does *not* ship is
the **Xcode Command Line Tools**, and an analyst's laptop usually has neither
those nor Homebrew. Nothing here needs them — but it is easy to trip over by
accident, because `git`, `make`, `clang` and the stock `/usr/bin/python3` are
stubs for that bundle: running any of them pops a system dialog offering to
install about a gigabyte of developer tooling.

None of the commands below invoke one. They use only utilities macOS already
has — `curl`, `tar`, `sh`, `uname` — because both installs are self-contained:

| Install | Why it needs nothing else |
| --- | --- |
| `uv` | A standalone binary. Its installer never mentions Python, and it downloads its own to run the server. |
| Google Cloud CLI | The macOS tarball bundles its own Python (`.install/bundled-python3-unix-darwin-*`). |

The whole terminal requirement is the three blocks below, once.

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
which uvx      # note this absolute path — Claude Desktop will need it
```

Typically `/Users/<you>/.local/bin/uvx`.

### 2. Install the Google Cloud CLI

Pick the build for your chip — `uname -m` prints `arm64` for Apple Silicon,
`x86_64` for Intel:

```bash
# Apple Silicon
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-arm.tar.gz
tar -xzf google-cloud-cli-darwin-arm.tar.gz

# Intel — same, with the other file
# curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-x86_64.tar.gz
# tar -xzf google-cloud-cli-darwin-x86_64.tar.gz

./google-cloud-sdk/install.sh --quiet
```

Avoid `brew install --cask google-cloud-sdk`: Homebrew itself requires the
Command Line Tools, which is the thing this section exists to avoid.

### 3. Authenticate

```bash
./google-cloud-sdk/bin/gcloud auth application-default login
./google-cloud-sdk/bin/gcloud auth application-default set-quota-project your-gcp-project
```

This writes a credentials file that the Google libraries read directly.
**`gcloud` does not need to be on your `PATH` afterwards** — it is needed once,
here. That is why a GUI-launched Claude Desktop can query BigQuery even though
it cannot see your shell.

> Your account needs **BigQuery Job User** on the project the query runs in, and
> **BigQuery Data Viewer** on each dataset it reads — often a different project.

### 4. Check it worked

```bash
BQ_PROJECT=your-gcp-project uvx data-platform-mcp doctor
```

Then register with your client: [Claude Desktop](#claude-desktop) or
[Claude Code](#4-register-with-your-ai-client).

### Alternative: no terminal at all for the analyst

If even that is too much, an admin can do the credential half centrally and
the analyst installs nothing but `uv` — skipping step 2 and step 3 entirely.
(Nothing about this is macOS-specific; it works the same on any OS.)

```bash
# the admin, once, on their own machine
data-platform-mcp setup --project your-gcp-project --datasets sales,events
gcloud iam service-accounts keys create analyst-key.json \
  --iam-account data-platform-mcp-ro@your-gcp-project.iam.gserviceaccount.com
```

The analyst saves that file and points the config at it:

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["data-platform-mcp"],
      "env": {
        "BQ_PROJECT": "your-gcp-project",
        "GOOGLE_APPLICATION_CREDENTIALS": "/Users/YOU/keys/analyst-key.json"
      }
    }
  }
}
```

**The trade-off is real and worth stating.** A key file is a long-lived
credential sitting on a laptop, where `gcloud auth application-default login`
issues short-lived tokens tied to a person. It is defensible here because the
account created by `setup --datasets` can only read the datasets you name, and
because a key can be revoked centrally the moment a laptop is lost — but it is
strictly weaker, and it is a per-analyst secret, so do not put it in a shared
config file or a repository.


---

## Linux

The same two installs, with no equivalent of the Command Line Tools problem:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
tar -xzf google-cloud-cli-linux-x86_64.tar.gz
./google-cloud-sdk/install.sh --quiet
./google-cloud-sdk/bin/gcloud auth application-default login
```

Then [check it worked](#4-check-it-worked) and register with your client.

---

## Windows

> **Not verified end to end.** The download URLs and install locations below
> were checked; the flow itself has not been run on a Windows machine. CI tests
> Linux only. Treat this as a careful derivation, not a tested recipe — and
> please open an issue if a step is wrong.

### 1. Install uv

In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

This installs `uv.exe` and `uvx.exe` into `%USERPROFILE%\.local\bin`. Confirm
the exact path, because the Desktop config needs it in full:

```powershell
(Get-Command uvx).Source
```

### 2. Install the Google Cloud CLI

Download and run
[GoogleCloudSDKInstaller.exe](https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe).
Leave **"Bundled Python"** ticked — it is what lets the SDK run without a
separate Python install, the same property the macOS tarball has.

### 3. Authenticate

In a **new** PowerShell window, so it picks up the updated `PATH`:

```powershell
gcloud auth application-default login
gcloud auth application-default set-quota-project your-gcp-project
```

This writes credentials to
`%APPDATA%\gcloud\application_default_credentials.json`, which the Google
libraries read directly — so `gcloud` need not be on `PATH` afterwards.

### 4. Check it worked

```powershell
$env:BQ_PROJECT="your-gcp-project"; uvx data-platform-mcp doctor
```

### 5. Configure Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` — create it if absent.
**Backslashes must be doubled in JSON**, and the path must be absolute:

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "C:\\Users\\YOU\\.local\\bin\\uvx.exe",
      "args": ["data-platform-mcp"],
      "env": {
        "BQ_PROJECT": "your-gcp-project"
      }
    }
  }
}
```

Replace `C:\Users\YOU\...` with what `(Get-Command uvx).Source` printed, with
each `\` written as `\\`. Then fully quit and reopen Claude Desktop.

If it fails, the logs are in `%APPDATA%\Claude\logs\`. `ENOENT` there means the
`command` path is wrong or its backslashes were not doubled — the same failure
macOS has, with one extra way to get it wrong.

---

## Quick start (per user)

Each person runs their own local copy. Queries execute under **their own**
BigQuery/IAM permissions, so existing access controls decide who can see what.

Install the prerequisites for your platform first — [macOS](#macos-setup),
[Linux](#linux), [Windows](#windows) — then come back here.

### 1. Install

The package is published as **`data-platform-mcp`** (`bigquery-mcp` was already
taken on PyPI by an unrelated project). No checkout is needed — the client can
fetch and run it directly:

```bash
uvx data-platform-mcp --version
```

**From source**, for development:

```bash
git clone git@github.com:deBilla/bigquery-mcp.git
cd bigquery-mcp

python3 -m venv .venv
./.venv/bin/pip install -e .
```

Either way you get a `data-platform-mcp` command, which is what the client runs.

### 2. Authenticate to Google (one time)

Covered in the platform sections above: [macOS step 3](#3-authenticate), or the
equivalent `gcloud auth application-default login` elsewhere. Queries then run
under your own credentials via
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).

**Using a service-account key instead?** Set `GOOGLE_APPLICATION_CREDENTIALS`
to its path — but set it **where the MCP server is launched**, not in a shell:

```jsonc
// in your client's MCP config, alongside BQ_PROJECT
"env": {
  "BQ_PROJECT": "your-gcp-project",
  "GOOGLE_APPLICATION_CREDENTIALS": "/absolute/path/to/key.json"
}
```

The client spawns the server as a subprocess with only the environment its
config declares. Exporting the variable in a terminal has no effect on it —
that is a distinct failure from having no credentials at all, and it looks
identical from the outside.

### 3. Check your setup

```bash
BQ_PROJECT=your-gcp-project data-platform-mcp doctor
```

Checks credentials, job permission, dataset visibility and — the one that
catches people — **dataset regions**. BigQuery cannot query a dataset from a
different location, and its own error names neither the location it wanted nor
the one the dataset is in, so it reads as a missing table. `doctor` names both:

```
[  ok  ] run a query in my-project (location US)
[  ok  ] 39 datasets visible (no allowlist; all are readable)
[ warn ] 6 of 39 datasets are outside location US
         US-CENTRAL1: analytics_raw, business_data, ds_public, pg_public, public, recommendations
         BigQuery cannot query these from US, and cannot join them with
         datasets that are in it.
         Fix:  set BQ_LOCATION to the region you need, and run a separate
               server for datasets in another one.
```

A dataset in another region is a warning; one on your `BQ_DATASET_ALLOWLIST` is
a failure, because no tool call could ever read it.

### 4. Register with your AI client

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

**Claude Desktop** — see the dedicated section below; it needs absolute paths.

### 5. Restart the client and ask a question

> "Which datasets are available? In the `sales` dataset, how many rows does the
> `orders` table have?"

---

## Claude Desktop

Most of a data team will use Desktop rather than the CLI, and it has one
failure mode the CLI does not.

**Claude Desktop does not inherit your shell `PATH`.** It launches from the
Finder, so `uvx`, `python` and anything installed by Homebrew or `uv` are
invisible to it. A config that says `"command": "uvx"` fails with `ENOENT` —
the server never starts, and the error names the command rather than the
reason. **Every path in this file must be absolute.**

What does *not* break: **credentials**. Application Default Credentials are a
file that the Google libraries read directly, so `gcloud` does not need to be
on `PATH` for queries to work — it is only needed once, in a terminal, to
create that file. Verified by running this server with an entirely empty
environment: the query succeeded.

### 1. Install and authenticate

Do the platform setup first — [macOS](#macos-setup) (two pastes, no Xcode
tools needed) or [Linux](#linux), [Windows](#windows). You need two things
from it: the **absolute path** that `which uvx` printed, and a completed
`gcloud auth application-default login`.

### 2. Edit the config

Claude Desktop's **Settings → Connectors** lists hosted connectors; a local
server like this one is not added there. It goes in a JSON file instead:

**Settings → Developer → Edit Config** opens it. Or edit it directly:

| OS | File |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

**The file usually already exists and holds your Desktop preferences.** Add
`mcpServers` as one more top-level key — do not replace the file, or you will
lose those settings. If it genuinely does not exist, create it with just the
block below.

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["data-platform-mcp"],
      "env": {
        "BQ_PROJECT": "your-gcp-project"
      }
    }
  }
}
```

Replace `/Users/YOU/.local/bin/uvx` with what `which uvx` printed. On Windows
the path looks like `C:\\Users\\YOU\\.local\\bin\\uvx.exe`, and backslashes must be
doubled in JSON.

Merged into a file that already has settings, it looks like this — `mcpServers`
sits alongside whatever is there, not instead of it:

```json
{
  "preferences": { "...": "your existing settings, left alone" },
  "mcpServers": {
    "bigquery": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["data-platform-mcp"],
      "env": { "BQ_PROJECT": "your-gcp-project" }
    }
  }
}
```

Check it still parses before restarting — a stray comma disables **every**
server, silently:

```bash
python3 -m json.tool ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

### 3. Restart Claude Desktop

Fully quit and reopen — reloading the window is not enough. The server appears
under the tools icon in the message box.

### Managing several warehouses

Rather than growing the JSON, put the environments in
`~/.config/data-platform-mcp/config.toml` (see
[Environments](#environments)). The Desktop config then needs no `env` block at
all, and is identical on every machine:

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["data-platform-mcp"]
    }
  }
}
```

This is the better shape for a team: one config file to share, and the JSON
stops carrying project ids.

### When it does not work

Desktop hides the reason, so check in this order:

1. **Run the doctor in a terminal.** It reports credentials, roles, dataset
   visibility and regions in one pass, and is the fastest way to tell a setup
   problem from a Desktop problem:
   ```bash
   BQ_PROJECT=your-gcp-project /Users/YOU/.local/bin/uvx data-platform-mcp doctor
   ```
2. **Read the logs.** macOS: `~/Library/Logs/Claude/mcp*.log`. `ENOENT` or
   "command not found" there means the `command` path is wrong — go back to
   `which uvx`.
3. **Check the JSON parses.** A trailing comma silently disables every server:
   ```bash
   python3 -m json.tool ~/Library/Application\ Support/Claude/claude_desktop_config.json
   ```


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
| `BQ_MCP_ENVIRONMENTS` | _(none)_ | JSON map of environment name to settings. Takes precedence over the config file. |
| `BQ_MCP_DEFAULT_ENVIRONMENT` | _(safest, else first)_ | Environment used when a call omits `environment`. Prefers a staging/dev environment when unset. |
| `BQ_MCP_CONFIG` | `~/.config/data-platform-mcp/config.toml` | Path to the TOML config file |
| `BQ_IMPERSONATE_SERVICE_ACCOUNT` | _(none)_ | Read-only service account to impersonate |
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
| `test_diagnostics.py` | `doctor`'s report, including the region and allowlist failures it exists to catch early |
| `test_environments.py` | Routing between environments, per-environment limits, and impersonation targeting |
| `test_config.py` | The environment registry, aliases, the TOML file, and the missing-project error that used to be an import-time crash |
| `test_errors.py` | Auth failures carry the command that fixes them |
| `test_formatting.py` | The size and cost figures a user is asked to approve |
| `test_eval_scoring.py` | The eval scorer, fed the trajectories each case exists to reject |

### Evals

Two further layers need live credentials, so they are not part of `pytest`:
`evals/measure.py` records what a client actually receives from each tool, and
`evals/tool_use_evals.py` asks real questions through the `claude` CLI and
scores the **trajectory** from the server's own audit log — which tool ran,
against which environment, with which arguments.

```bash
./.venv/bin/python evals/measure.py                     # payload sizes
./.venv/bin/python evals/tool_use_evals.py              # 6 cases, spends tokens
./.venv/bin/python evals/tool_use_evals.py --rescore    # re-score saved replies, free
```

See [`evals/README.md`](evals/README.md) for what each case catches and
[`evals/BASELINE.md`](evals/BASELINE.md) for what the last run measured. Tool
and server descriptions are the highest-leverage thing to change in this
server, and nothing except an eval tells you they need changing.

### Mutation testing

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
