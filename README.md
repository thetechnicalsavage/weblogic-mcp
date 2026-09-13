# wls-mcp

An MCP server that exposes a small, audited slice of the **Oracle WebLogic Server** REST
management API to an AI agent — four tools, a least-privilege service account, and a JSON audit
record for every call.

Built and verified against **WebLogic Server 15.1.1.0** (Generic, JDK 21, Oracle Linux 9)
running in Docker.

## Why WebLogic, why now

Oracle removed the browser-based WebLogic Administration Console in 14.1.2, and 15.1.1 ships
without it. Day-to-day administration is REST, WLST, or the separate WebLogic Remote Console.
That makes WebLogic a natural fit for MCP: the management surface is already an HTTP API, so
this server is a thin, explicit wrapper rather than screen-scraping — and the interesting work
is in scoping and auditing it, not in plumbing.

## Tools

| Tool | Reads | Notes |
|---|---|---|
| `list_servers` | `serverLifeCycleRuntimes` + `edit/servers` | Every configured server with state, port, machine. The only view that includes stopped servers. |
| `get_server_health` | `serverRuntimes/{name}` | Health state, failed subsystem, symptoms. Returns lifecycle state instead when the server is not RUNNING. |
| `get_jvm_stats` | `serverRuntimes/{name}/JVMRuntime` | Heap current/max/free, free %, uptime, Java version. Bytes converted to MB. |
| `control_server` | `serverLifeCycleRuntimes/{name}/{action}` | `start`, `shutdown`, `force_shutdown`, via Node Manager. Destructive; see guardrails. |

Three WebLogic behaviours the tool contracts are built around:

1. **`serverRuntimes` only contains RUNNING servers.** A stopped server is absent entirely — not
   present with `state: SHUTDOWN`. So listing servers must read `serverLifeCycleRuntimes`, while
   health and JVM stats read `serverRuntimes`.
2. **`serverRuntimes` can be briefly empty** while a managed server registers with the domain
   runtime service. "No runtime record" means *not currently running*, not *error*.
3. **`X-Requested-By` is mandatory** on state-changing requests; WebLogic rejects them as CSRF
   otherwise. The header value is arbitrary — its presence is the point.

## Guardrails

`control_server` is the only tool that changes anything, and it is fenced:

- **Least-privilege account.** The server authenticates as a dedicated WebLogic user in the
  `Operators` and `Monitors` groups — never the domain administrator. That account can read
  runtime state and drive server lifecycle, and is refused when it tries to open a configuration
  edit session or create resources (verified: `403` on both).
- **Allowlist.** `WLS_ALLOWED_SERVERS` restricts which servers are addressable at all. Unset
  means the whole domain.
- **Read-only mode.** `WLS_READ_ONLY=true` disables `control_server` outright.
- **Administration Server confirmation.** Stopping the AdminServer also stops the REST API this
  server depends on, so nothing here can start it again. It requires `confirm=true`, and the
  response carries the host-level recovery command. `WLS_ALLOW_ADMIN_SHUTDOWN=false` forbids it
  entirely.
- **No retry on lifecycle calls.** GETs retry with capped exponential backoff; `start` and
  `shutdown` are sent exactly once.

## Audit trail

Every invocation emits exactly one JSON record, on success, refusal, or error:

```json
{"ts":"2026-09-13T12:41:07.882Z","tool":"control_server","principal":"wlsmcp",
 "target":"ms1","arguments":{"server_name":"ms1","action":"shutdown","confirm":false},
 "outcome":"success","duration_ms":9183}
```

Records go to stderr, and to `WLS_AUDIT_LOG` as JSON Lines if set. **Nothing is ever written to
stdout** — under the stdio transport, stdout is the MCP protocol channel, and a stray `print`
corrupts the session.

## Install

```bash
uv venv && uv pip install -e .
cp .env.example .env   # then fill in WLS_PASSWORD
```

## Run

Stdio, for a local client:

```bash
wls-mcp
```

Streamable HTTP, to serve a client on another machine:

```bash
wls-mcp --transport streamable-http --host 0.0.0.0 --port 8765
```

Over HTTP the server has no authentication of its own — put it behind a reverse proxy that
terminates TLS and checks a bearer token, or keep it on a private network.

## Wiring it into a client

**Codex CLI**

```bash
codex mcp add weblogic \
  --env WLS_BASE_URL=http://localhost:7001 \
  --env WLS_USERNAME=wlsmcp \
  --env WLS_PASSWORD=... \
  -- /path/to/.venv/bin/wls-mcp
```

or over HTTP:

```bash
codex mcp add weblogic --url http://host:8765/mcp --bearer-token-env-var WLS_MCP_TOKEN
```

**Claude Code**

```bash
claude mcp add weblogic --env WLS_USERNAME=wlsmcp --env WLS_PASSWORD=... -- /path/to/.venv/bin/wls-mcp
```

## Configuration

See `.env.example`. Credentials are read only from the environment, never from argv, so they do
not show up in `ps`.

## Tests

```bash
uv pip install -e ".[dev]" && pytest
```

Unit tests mock the WebLogic REST API with `respx`; no live server needed.

## Licence

MIT. The Oracle WebLogic Server binaries are **not** included or redistributed here — you pull
Oracle's image yourself after accepting the licence on Oracle Container Registry. See
`../RUNBOOK.md` for the full domain build.
