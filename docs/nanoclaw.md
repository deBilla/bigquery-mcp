# Connecting from a containerized client (nanoclaw)

[nanoclaw](https://nanoclaw.dev) runs each agent in an **isolated Docker
container**. A stdio MCP server would have to run *inside* that container —
needing the server code, a Python runtime, and Google credentials all baked in.

Instead, run this server **on the host over HTTP** and have the container
connect to it by URL. The server (and its credentials) stay outside the
container; the agent reaches it through Docker's host gateway.

```
  ┌─ container (agent) ─┐         ┌──────────── host ────────────┐
  │  Claude Agent SDK   │  HTTP   │  server.py  (HTTP transport) │
  │  mcpServers: {…url} ─┼────────▶  http://…:8765/mcp           │
  └─────────────────────┘         │  ADC creds (gcloud login)    │
        host.docker.internal      └──────────────────────────────┘
```

**Why this is nice:** no Python in the agent image, no service-account key
mounted into the container, and Google auth uses your existing host `gcloud`
login. Access is naturally **per-group** — only groups whose `container.json`
lists the server can use it.

## 1. Run the server on the host

```bash
BQ_PROJECT=your-gcp-project ./run-http.sh
# Serving … on http://0.0.0.0:8765/mcp
```

Keep it running (a launchd/systemd unit or `tmux` is fine for a long-lived host).

## 2. Add it to a group's `container.json`

Edit `groups/<group>/container.json` in your nanoclaw checkout and add a
**remote** MCP server entry (note `type` + `url` instead of `command`):

```json
{
  "mcpServers": {
    "data-platform": {
      "type": "http",
      "url": "http://host.docker.internal:8765/mcp",
      "instructions": "Use these tools to answer data questions by querying BigQuery. Discover datasets/tables/schema first, then run read-only SELECT queries. If a query is flagged as costly, tell the user the estimated size and cost and ask before confirming."
    }
  }
}
```

Only the groups you add this to get data access — that's your access control.

## Requirements on the nanoclaw side

This needs a one-line capability that nanoclaw's `McpServerConfig` gained:
support for **remote** (`type`/`url`) servers in addition to stdio ones. If your
nanoclaw predates that, the same change is: make `command` optional and add
optional `type`/`url`/`headers` to `McpServerConfig` (host `container-config.ts`
and the agent-runner's `types.ts`/`config.ts`/`index.ts`), and cast to the SDK's
`McpServerConfig` union where it's passed to `query()`.

## Notes

- `host.docker.internal` resolves to the host on Docker Desktop (macOS/Windows)
  automatically; nanoclaw adds `--add-host=host.docker.internal:host-gateway`
  on Linux. No extra setup either way.
- Bind to `127.0.0.1` instead of `0.0.0.0` if you only need local access and
  your Docker setup still routes the gateway to it; `0.0.0.0` is the safe default
  for reaching it from a container.
- For SSE clients instead of streamable-HTTP, set `BQ_MCP_TRANSPORT=sse` and use
  `"type": "sse"` with URL path `/sse`.
