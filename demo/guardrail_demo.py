# v1.0
"""Demonstrate, live, that the WebLogic MCP server refuses what it should refuse.

Every check below runs over the real MCP protocol against a real WebLogic domain. Nothing is
mocked and nothing is simulated - each refusal is the server actually saying no.

    python demo/guardrail_demo.py
"""

import asyncio
import json
import os
import pathlib
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = str(ROOT / ".venv" / "bin" / "wls-mcp")
AUDIT = ROOT / "logs" / "demo-audit.jsonl"
BASE_URL = os.getenv("WLS_BASE_URL", "http://127.0.0.1:7001")

GREEN, RED, GREY, BOLD, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"
results = []


def record(name, expectation, passed, detail=""):
    results.append(passed)
    mark = f"{GREEN}PASS{OFF}" if passed else f"{RED}FAIL{OFF}"
    print(f"  {mark}  {name}")
    print(f"        expected: {expectation}")
    if detail:
        print(f"        {GREY}{detail[:160]}{OFF}")


def _credentials():
    env = {}
    for line in (ROOT / "secrets" / "mcp.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    return env["WLS_MCP_USERNAME"], env["WLS_MCP_PASSWORD"]


USER, PASSWORD = _credentials()


def server_env(**overrides):
    env = {
        "PATH": os.environ["PATH"],
        "WLS_BASE_URL": BASE_URL,
        "WLS_USERNAME": USER,
        "WLS_PASSWORD": PASSWORD,
        "WLS_AUDIT_LOG": str(AUDIT),
    }
    env.update(overrides)
    return env


async def call(env, tool, args):
    """One tool call in its own MCP session, so each scenario gets its own server config."""
    async with stdio_client(StdioServerParameters(command=SERVER, args=[], env=env)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return result.is_error, result.content[0].text


async def main():
    AUDIT.parent.mkdir(exist_ok=True)
    AUDIT.write_text("")

    print(f"\n{BOLD}WebLogic MCP server - guardrail demonstration{OFF}")
    print(f"{GREY}domain {BASE_URL}   principal {USER}{OFF}\n")

    print(f"{BOLD}1. Baseline: the read tools work{OFF}")
    err, text = await call(server_env(), "list_servers", {})
    servers = json.loads(text)["servers"] if not err else []
    record("list_servers returns the domain",
           "allowed",
           not err and len(servers) >= 2,
           " ".join(f"{s['name']}={s['state']}" for s in servers))

    print(f"\n{BOLD}2. Stopping the Administration Server needs explicit confirmation{OFF}")
    err, text = await call(server_env(), "control_server", {"server_name": "AdminServer", "action": "shutdown"})
    record("AdminServer shutdown without confirm", "REFUSED", err, text)

    print(f"\n{BOLD}3. A server name cannot be used to steer the REST path{OFF}")
    err, text = await call(server_env(), "control_server",
                           {"server_name": "../../edit/servers", "action": "start"})
    record("path traversal in server_name", "REFUSED", err, text)

    print(f"\n{BOLD}4. An allowlist confines the server to named targets{OFF}")
    env = server_env(WLS_ALLOWED_SERVERS="ms1")
    err, text = await call(env, "control_server", {"server_name": "AdminServer", "action": "start"})
    record("AdminServer while allowlist is 'ms1'", "REFUSED", err, text)
    err, text = await call(env, "get_server_health", {"server_name": "ms1"})
    record("ms1 while allowlist is 'ms1'", "allowed", not err, text)

    print(f"\n{BOLD}5. Read-only mode removes every state change{OFF}")
    err, text = await call(server_env(WLS_READ_ONLY="true"), "control_server",
                           {"server_name": "ms1", "action": "start"})
    record("start ms1 with WLS_READ_ONLY=true", "REFUSED", err, text)

    print(f"\n{BOLD}6. WebLogic itself denies what the account may not do{OFF}")
    print(f"{GREY}        (not the MCP layer - this is the security realm refusing the service account){OFF}")
    denied = []
    async with httpx.AsyncClient(auth=(USER, PASSWORD), timeout=30) as client:
        for label, path, payload in (
            ("open a configuration edit session", "/edit/changeManager/startEdit", {}),
            ("create a JDBC datasource", "/edit/JDBCSystemResources", {"name": "demoDS"}),
        ):
            response = await client.post(
                f"{BASE_URL}/management/weblogic/latest{path}",
                json=payload,
                headers={"X-Requested-By": "demo", "Content-Type": "application/json"},
            )
            body = response.text
            refused = response.status_code == 403 or "403" in body
            denied.append(refused)
            record(f"{USER} tries to {label}", "DENIED by WebLogic", refused,
                   f"HTTP {response.status_code} {body[:90]}")

    print(f"\n{BOLD}7. Every attempt above is in the audit trail{OFF}")
    records = [json.loads(line) for line in AUDIT.read_text().splitlines() if line.strip()]
    refusals = [r for r in records if r["outcome"] == "refused"]
    print(f"        {len(records)} audit records written, {len(refusals)} of them refusals\n")
    print(f"        {'tool':<18}{'target':<22}{'outcome':<10}{'ms':>6}")
    for r in records:
        print(f"        {r['tool']:<18}{str(r.get('target') or '-'):<22}{r['outcome']:<10}{r['duration_ms']:>6}")
    record("refusals are recorded, not silently dropped", "at least 4 refusals logged", len(refusals) >= 4)

    secret_leak = PASSWORD in AUDIT.read_text()
    record("no credential appears in the audit log", "clean", not secret_leak)

    print()
    if all(results):
        print(f"{GREEN}{BOLD}ALL {len(results)} GUARDRAIL CHECKS PASSED{OFF}\n")
    else:
        print(f"{RED}{BOLD}{results.count(False)} of {len(results)} CHECKS FAILED{OFF}\n")
        sys.exit(1)


asyncio.run(main())
