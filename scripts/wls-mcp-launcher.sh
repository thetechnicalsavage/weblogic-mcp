#!/bin/bash
# v1.1 - changelog: parse the secrets file as KEY=VALUE instead of sourcing it. Sourcing runs
#        the file as shell, so anything that printed to stdout would corrupt the MCP stdio
#        channel before the server ever started.
# v1.0 - Launch the WebLogic MCP server for a stdio client (Codex, Claude Code, Claude Desktop).
# Credentials are sourced from secrets/mcp.env at launch, so no password is ever written into
# the client's own config file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="${WLS_SECRETS_FILE:-$ROOT/secrets/mcp.env}"

if [ ! -r "$SECRETS" ]; then
  echo "wls-mcp-launcher: cannot read $SECRETS" >&2
  exit 1
fi

# Read KEY=VALUE pairs without executing the file.
while IFS='=' read -r key value; do
  case "$key" in
    ''|\#*) continue ;;
    WLS_*)   export "$key=$value" ;;
  esac
done < "$SECRETS"

export WLS_BASE_URL="${WLS_BASE_URL:-http://127.0.0.1:7001}"
export WLS_USERNAME="${WLS_MCP_USERNAME:?WLS_MCP_USERNAME missing from $SECRETS}"
export WLS_PASSWORD="${WLS_MCP_PASSWORD:?WLS_MCP_PASSWORD missing from $SECRETS}"
export WLS_AUDIT_LOG="${WLS_AUDIT_LOG:-$ROOT/logs/audit.jsonl}"
mkdir -p "$(dirname "$WLS_AUDIT_LOG")"

exec "$ROOT/.venv/bin/wls-mcp" "$@"
