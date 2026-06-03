#!/usr/bin/env bash
# Run the BigQuery MCP server over HTTP (instead of stdio), so a remote or
# containerized client can connect by URL — e.g. nanoclaw agents reaching it at
# http://host.docker.internal:8765/mcp. Auth stays here on the host via ADC, so
# no Google credentials enter the container.
#
# Usage:
#   BQ_PROJECT=your-gcp-project ./run-http.sh
#
# Prereqs: ./.venv created (see README) and `gcloud auth application-default login`.
set -euo pipefail
cd "$(dirname "$0")"

: "${BQ_PROJECT:?Set BQ_PROJECT to your GCP project ID}"
export BQ_MCP_TRANSPORT="${BQ_MCP_TRANSPORT:-http}"
export BQ_MCP_HOST="${BQ_MCP_HOST:-0.0.0.0}"
export BQ_MCP_PORT="${BQ_MCP_PORT:-8765}"

echo "Serving BigQuery MCP for project '$BQ_PROJECT' on http://$BQ_MCP_HOST:$BQ_MCP_PORT/mcp"
exec ./.venv/bin/python server.py
