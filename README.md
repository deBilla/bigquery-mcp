# BigQuery MCP

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server over
Google BigQuery. It lets an AI client (Claude Desktop, Claude Code, …) answer
plain-language data questions by discovering schema and running `SELECT` queries.

The AI does the natural-language → SQL translation; this server just safely
executes against BigQuery.

## Tools exposed

| Tool | Purpose |
|------|---------|
| `list_datasets` | List datasets in the project |
| `list_tables` | List tables/views in a dataset |
| `get_table_schema` | Columns, types, row count, size for a table |
| `run_query` | Run a validated, read-only `SELECT` and return rows |

## Safety

- Every query is **dry-run first** to validate and estimate bytes scanned.
- **Only `SELECT` / `WITH`** statements run — no writes, DDL, or DML.
- **Cost confirmation:** a query estimated to scan more than `BQ_WARN_BYTES`
  (default 1 GB) does **not** run. Instead it returns
  `status: "confirmation_required"` with the estimated scan size and dollar
  cost, so the client can tell the user and ask before proceeding. Re-call with
  `confirm_expensive=true` to run it.
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

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# One-time auth (uses your own Google credentials):
gcloud auth application-default login
```

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

## Register with a client

> Replace `/path/to/bigquery-mcp` with the absolute path to your checkout, and
> `your-gcp-project` with your GCP project ID.

### Claude Code

```bash
claude mcp add bigquery \
  --env BQ_PROJECT=your-gcp-project \
  -- /path/to/bigquery-mcp/.venv/bin/python /path/to/bigquery-mcp/server.py
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bigquery": {
      "command": "/path/to/bigquery-mcp/.venv/bin/python",
      "args": ["/path/to/bigquery-mcp/server.py"],
      "env": { "BQ_PROJECT": "your-gcp-project" }
    }
  }
}
```

Then restart the client and ask, e.g.:
> "Which datasets are available? In the `sales` dataset, how many rows does the `orders` table have?"

## Authentication

Uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials).
Each user runs `gcloud auth application-default login` once; queries execute
under **their own** BigQuery/IAM permissions, so existing access controls decide
who can see what. For server deployments, point `GOOGLE_APPLICATION_CREDENTIALS`
at a service-account key with BigQuery Data Viewer + Job User roles instead.

## License

MIT — see [LICENSE](LICENSE).
