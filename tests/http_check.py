# v1.0 - Verify the streamable-HTTP transport against a LIVE WebLogic domain.
# Start the server first:  wls-mcp --transport streamable-http --host 127.0.0.1 --port 8765
import asyncio, json, os, sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.getenv("WLS_MCP_URL", "http://127.0.0.1:8765/mcp")


async def main():
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[streamable-http] connected to {init.server_info.name} {init.server_info.version} at {URL}")

            tools = [t.name for t in (await session.list_tools()).tools]
            print(f"[streamable-http] tools: {tools}")
            assert len(tools) == 4, tools

            data = json.loads((await session.call_tool("list_servers", {})).content[0].text)
            for srv in data["servers"]:
                print(f"   {srv['name']:14s} {srv['state']}")

            health = json.loads(
                (await session.call_tool("get_server_health", {"server_name": "AdminServer"})).content[0].text
            )
            print(f"   health(AdminServer) = {health['health']}")
            assert health["health"] == "ok"

    print("STREAMABLE-HTTP CHECK PASSED")


try:
    asyncio.run(main())
except AssertionError as exc:
    print(f"FAILED: {exc}")
    sys.exit(1)
