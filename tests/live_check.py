# v1.0 - End-to-end check against a LIVE WebLogic domain, driven over the real MCP stdio protocol.
# Not a unit test: requires a running domain. Run with the wls-mcp environment variables set.
import asyncio, json, os, sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.getenv("WLS_MCP_BIN", os.path.abspath(".venv/bin/wls-mcp"))
MS = os.getenv("WLS_TEST_SERVER", "ms1")
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


async def main():
    params = StdioServerParameters(command=SERVER, args=[], env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print(f"\n[handshake] {init.server_info.name} {init.server_info.version}")
            check("server identifies as 'weblogic'", init.server_info.name == "weblogic")

            tools = {t.name: t for t in (await s.list_tools()).tools}
            print(f"\n[tools] {len(tools)} advertised")
            for name, t in tools.items():
                a = t.annotations
                hints = [h for h, v in (("readOnly", a and a.read_only_hint),
                                        ("DESTRUCTIVE", a and a.destructive_hint)) if v]
                print(f"  - {name:18s} required={t.input_schema.get('required', [])} {hints}")
            check("all four tools advertised",
                  set(tools) == {"list_servers", "get_server_health", "get_jvm_stats", "control_server"})
            check("read tools flagged read-only",
                  all(tools[n].annotations.read_only_hint for n in
                      ("list_servers", "get_server_health", "get_jvm_stats")))
            check("control_server flagged destructive",
                  tools["control_server"].annotations.destructive_hint is True)

            async def call(tool, args):
                res = await s.call_tool(tool, args)
                return res, json.loads(res.content[0].text) if not res.is_error else res.content[0].text

            print("\n[list_servers]")
            _, data = await call("list_servers", {})
            for srv in data["servers"]:
                print(f"  {srv['name']:14s} {srv['state']:22s} port={srv['listen_port']} machine={srv['machine']}")
            check("domain reports at least 2 servers", data["server_count"] >= 2)
            check(f"{MS} is present", any(x["name"] == MS for x in data["servers"]))

            print(f"\n[control_server] start {MS}")
            _, d = await call("control_server", {"server_name": MS, "action": "start"})
            print(f"  {d['state_before']} -> {d['state_after']} ({d['progress']})")
            check(f"{MS} reaches RUNNING", d["state_after"] == "RUNNING")

            print(f"\n[get_server_health] {MS}")
            _, d = await call("get_server_health", {"server_name": MS})
            print(f"  state={d['state']} health={d['health']} symptoms={d['symptoms']}")
            check("health reported ok", d["health"] == "ok")

            print(f"\n[get_jvm_stats] {MS}")
            _, d = await call("get_jvm_stats", {"server_name": MS})
            print(f"  java={d['java_version']} heap {d['heap_free_mb']}/{d['heap_max_mb']} MB free ({d['heap_free_percent']}%)")
            check("heap figures present", isinstance(d["heap_free_percent"], int))

            print("\n[guardrail] AdminServer shutdown without confirm")
            res, text = await call("control_server", {"server_name": "AdminServer", "action": "shutdown"})
            print(f"  is_error={res.is_error} :: {str(text)[:110]}")
            check("AdminServer stop refused without confirm", res.is_error is True)

            print("\n[guardrail] unknown action")
            res, text = await call("control_server", {"server_name": MS, "action": "nuke"})
            check("unknown action rejected", res.is_error is True)

            print(f"\n[control_server] shutdown {MS}")
            _, d = await call("control_server", {"server_name": MS, "action": "shutdown"})
            print(f"  {d['state_before']} -> {d['state_after']} ({d['progress']})")
            check(f"{MS} returns to SHUTDOWN", d["state_after"] == "SHUTDOWN")

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        sys.exit(1)
    print("ALL LIVE CHECKS PASSED")


asyncio.run(main())
