# BigQuery MCP

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server over
Google BigQuery. It lets an AI client (Claude Code, Claude Desktop, …) answer
plain-language data questions by discovering schema and running `SELECT` queries.

The AI does the natural-language → SQL translation; this server just safely
executes against BigQuery under **your own** Google credentials.

- **Repo:** https://github.com/deBilla/bigquery-mcp
- **Clone (SSH):** `git@github.com:deBilla/bigquery-mcp.git`

---

## Tools exposed

| Tool | Purpose |
|------|---------|
| `list_datasets` | List datasets in the project |
| `list_tables` | List tables/views in a dataset |
| `get_table_schema` | Columns, types, row count, size for a table |
| `run_query` | Run a validated, read-only `SELECT` and return rows |

---

## Quick start (per user)

Each person runs their own local copy. Queries execute under **their own**
BigQuery/IAM permissions, so existing access controls decide who can see what.
You need Python 3.10+ and the `gcloud` CLI installed.

### 1. Clone and install

```bash
git clone git@github.com:deBilla/bigquery-mcp.git
cd bigquery-mcp

python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### 2. Authenticate to Google (one time)

Uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).
Run this once; queries then execute as you.

```bash
gcloud auth application-default login
```

> Your account needs **BigQuery Data Viewer** + **BigQuery Job User** on the
> project you intend to query.

### 3. Register with your AI client

Replace `/abs/path/bigquery-mcp` with the absolute path to your checkout, and
`your-gcp-project` with your GCP project ID.

**Claude Code**

```bash
claude mcp add bigquery \
  --env BQ_PROJECT=your-gcp-project \
  -- /abs/path/bigquery-mcp/.venv/bin/python /abs/path/bigquery-mcp/server.py
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "/abs/path/bigquery-mcp/.venv/bin/python",
      "args": ["/abs/path/bigquery-mcp/server.py"],
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
| `BQ_PROJECT` | _(required)_ | GCP project ID whose BigQuery datasets you query |
| `BQ_LOCATION` | `US` | BigQuery location |
| `BQ_WARN_BYTES` | `1073741824` (1 GB) | Above this, ask the user to confirm before running |
| `BQ_MAX_BYTES_BILLED` | `5368709120` (5 GB) | Hard per-query scan cap — never exceeded |
| `BQ_COST_PER_TIB_USD` | `6.25` | On-demand price used to render the cost estimate |
| `BQ_ROW_LIMIT` | `200` | Default rows returned |
| `BQ_DATASET_ALLOWLIST` | _(empty = all)_ | Comma-separated dataset IDs |
| `BQ_MCP_TRANSPORT` | `stdio` | `stdio` (subprocess) or `http`/`sse` (serve over network) |
| `BQ_MCP_HOST` | `0.0.0.0` | Bind host when transport is `http`/`sse` |
| `BQ_MCP_PORT` | `8765` | Bind port when transport is `http`/`sse` |

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

## License

MIT — see [LICENSE](LICENSE).
