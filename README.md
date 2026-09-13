# weblogic-mcp

An MCP server that exposes a small, audited slice of the **Oracle WebLogic Server** REST
management API to an AI agent — four tools, a least-privilege service account, and a JSON audit
record for every call, including the ones it refuses.

Built and verified against **WebLogic Server 15.1.1.0** (Generic, JDK 21). It talks only to
WebLogic's RESTful Management Services, so it also works against 14.1.2 domains.

> This repository assumes you already have a WebLogic domain running. It does not install
> WebLogic and contains no Oracle binaries.

## Why WebLogic is a good fit for MCP

Oracle removed the browser-based WebLogic Administration Console in 14.1.2, and 15.1.1 ships
without it. Administration is now REST, WLST, or the separate WebLogic Remote Console. The
management surface is already an HTTP API — so an MCP server over it is a thin, explicit wrapper
rather than screen-scraping, and the interesting work is in scoping and auditing it.

## Tools

| Tool | Reads | Notes |
|---|---|---|
| `list_servers` | `serverLifeCycleRuntimes` + `edit/servers` | Every configured server with state, port, machine. The only view that includes stopped servers. |
| `get_server_health` | `serverRuntimes/{name}` | Health state, failed subsystem, symptoms. Returns lifecycle state instead when the server is not RUNNING. |
| `get_jvm_stats` | `serverRuntimes/{name}/JVMRuntime` | Heap current/max/free, free %, uptime, Java version. Bytes converted to MB. |
| `control_server` | `serverLifeCycleRuntimes/{name}/{action}` | `start`, `shutdown`, `force_shutdown`, through Node Manager. Destructive — see guardrails. |

### Three WebLogic behaviours the tool contracts are built around

1. **`serverRuntimes` contains only RUNNING servers.** A stopped server is absent entirely — not
   present with `state: SHUTDOWN`. Listing servers must read `serverLifeCycleRuntimes`; health and
   JVM statistics read `serverRuntimes`. Get this backwards and you build a tool that silently
   forgets about stopped servers.
2. **`serverRuntimes` can be briefly empty** while a managed server registers with the domain
   runtime service. "No runtime record" means *not currently running*, not *error*.
3. **`X-Requested-By` is mandatory** on state-changing requests; WebLogic rejects them as CSRF
   otherwise. The header value is arbitrary — its presence is the point.

## Requirements

- A running WebLogic 15.1.1 (or 14.1.2) domain whose REST management API you can reach.
- Python 3.10+.
- Node Manager running and your managed servers assigned to a Machine — `control_server` starts
  servers *through* Node Manager. Without it, start calls have nothing to drive.

## Install

```bash
git clone https://github.com/thetechnicalsavage/weblogic-mcp.git
cd weblogic-mcp
uv venv && uv pip install -e .          # or: python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env                    # then fill in WLS_PASSWORD
```

## Create the service account

Do not point this at your domain administrator. See **[docs/service-account.md](docs/service-account.md)**
for the WLST script, the group choice, and how to prove the privilege boundary.

Short version: create a user in `Operators` + `Monitors` with `scripts/create-mcp-user.py`.

## Run

Stdio, for a local client:

```bash
.venv/bin/wls-mcp
```

Streamable HTTP, to serve a client on another machine:

```bash
.venv/bin/wls-mcp --transport streamable-http --host 0.0.0.0 --port 8765
```

Over HTTP the server has no authentication of its own. Put it behind a reverse proxy that
terminates TLS and checks a bearer token, or keep it on a private network.

## Configure your client

`scripts/wls-mcp-launcher.sh` reads credentials from `secrets/mcp.env` at startup, so **no
password is written into your client's config file** — a file people screenshot.

Create `secrets/mcp.env` (gitignored):

```bash
mkdir -p secrets
( umask 077 && cat > secrets/mcp.env <<'EOF'
WLS_MCP_USERNAME=wlsmcp
WLS_MCP_PASSWORD=your-service-account-password
WLS_BASE_URL=http://your-admin-host:7001
EOF
)
```

### Codex CLI

```bash
codex mcp add weblogic -- /abs/path/to/weblogic-mcp/scripts/wls-mcp-launcher.sh
```

The resulting `~/.codex/config.toml` entry contains no secret:

```toml
[mcp_servers.weblogic]
command = "/abs/path/to/weblogic-mcp/scripts/wls-mcp-launcher.sh"
```

For a remote server over HTTP:

```bash
codex mcp add weblogic --url http://host:8765/mcp --bearer-token-env-var WLS_MCP_TOKEN
```

### Claude Code

```bash
claude mcp add weblogic -- /abs/path/to/weblogic-mcp/scripts/wls-mcp-launcher.sh
```

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "weblogic": {
      "command": "/abs/path/to/weblogic-mcp/scripts/wls-mcp-launcher.sh"
    }
  }
}
```

### GitHub Copilot (VS Code)

`.vscode/mcp.json`:

```json
{
  "servers": {
    "weblogic": {
      "type": "stdio",
      "command": "/abs/path/to/weblogic-mcp/scripts/wls-mcp-launcher.sh"
    }
  }
}
```

## Guardrails

`control_server` is the only tool that changes anything, and it is fenced five ways:

| Guardrail | Effect |
|---|---|
| **Least-privilege account** | The WebLogic realm itself refuses configuration edits and deployments — `403`, regardless of what the MCP layer does. |
| **`WLS_ALLOWED_SERVERS`** | Allowlist of addressable server names. Unset means the whole domain. |
| **`WLS_READ_ONLY=true`** | Disables `control_server` entirely. |
| **AdminServer confirmation** | Stopping the Administration Server requires `confirm=true`, and the response carries the recovery command. `WLS_ALLOW_ADMIN_SHUTDOWN=false` forbids it outright. |
| **Name validation** | Server names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and are percent-encoded into the REST path, so a name cannot steer the request elsewhere. |

Additionally: GET requests retry with capped exponential backoff, while **lifecycle POSTs are
sent exactly once** — a start must never be replayed. WebLogic-controlled strings are clipped to
500 characters and collections to 50 items before they reach the model, and response bodies over
4 MiB are refused rather than decoded.

Tool annotations mark the three read tools `read_only_hint` and `control_server`
`destructive_hint`, so a client can surface that in its approval UI.

### Refusals must be visible to the model

A tool that raises a plain exception gets wrapped by the MCP SDK as
`UnexpectedToolError("Error executing tool …")` — the message is treated as a crash detail and
**stays on the server**. Deliberate refusals raise `ToolError`, whose text is forwarded verbatim:

```
Error executing tool control_server: 'AdminServer' is the Administration Server. Stopping it
also stops the REST API that this MCP server uses, so no tool here can start it again.
Re-issue with confirm=true only if that is genuinely intended.
```

An agent can act on that. It cannot act on `Error executing tool control_server`. If your
guardrails are invisible to the model, they are not guardrails — they are just failures.

## Audit trail

One JSON record per invocation — success, refusal, or error:

```json
{"ts":"2026-09-13T12:59:32.789Z","tool":"control_server","principal":"wlsmcp",
 "target":"AdminServer","arguments":{"server_name":"AdminServer","action":"shutdown","confirm":false},
 "outcome":"refused","duration_ms":0}
```

Records go to stderr, and to `WLS_AUDIT_LOG` as JSON Lines if set. **Nothing is ever written to
stdout** — under the stdio transport stdout is the MCP protocol channel, and a stray `print`
corrupts the session.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WLS_BASE_URL` | `http://localhost:7001` | Admin server base URL |
| `WLS_USERNAME` / `WLS_PASSWORD` | — | Service account. Required. |
| `WLS_ALLOWED_SERVERS` | all | Comma-separated allowlist |
| `WLS_READ_ONLY` | `false` | `true` disables `control_server` |
| `WLS_ALLOW_ADMIN_SHUTDOWN` | `true` | `false` forbids stopping the AdminServer |
| `WLS_ADMIN_SERVER_NAME` | `AdminServer` | Name of the admin server |
| `WLS_VERIFY_TLS` | `true` | Set `false` only against a demo certificate |
| `WLS_TIMEOUT_SECONDS` | `30` | Read timeout |
| `WLS_LIFECYCLE_TIMEOUT_SECONDS` | `180` | Start/stop timeout — these are slow |
| `WLS_AUDIT_LOG` | — | Path for JSON Lines audit records |
| `WLS_MCP_TRANSPORT` / `_HOST` / `_PORT` | `stdio` / `127.0.0.1` / `8765` | Transport |

Credentials are read only from the environment, never from argv, so they do not appear in `ps`.

## Tests

```bash
uv pip install -e ".[dev]"
pytest                              # unit tests, WebLogic mocked with respx - no live domain
python tests/live_check.py          # end-to-end over the real MCP stdio protocol
python tests/http_check.py          # same, over streamable HTTP
```

## Production notes

- Enable WebLogic's administration port and serve the management API over TLS. `WLS_BASE_URL`
  and `WLS_VERIFY_TLS` exist so that is configuration, not a code change.
- Do not publish WebLogic's ports on `0.0.0.0`. If WebLogic runs in Docker, note that Docker
  inserts its own iptables rules ahead of `ufw`, so a host firewall allowlist does **not**
  protect a published container port.
- Use patched WebLogic images (`container-registry.oracle.com/middleware/weblogic_cpu`) rather
  than GA ones for anything beyond a proof of concept.

## Licence

MIT.
