# v1.4 - changelog: use fullmatch so a trailing newline cannot satisfy the name pattern; clip
#        and cap the list_servers payload too.
# v1.3 - changelog: URL-encode server names into REST paths and reject malformed ones; require
#        confirm is exactly True; clip WebLogic-controlled strings and collections before they
#        reach the model or the audit log.
# v1.2 - changelog: raise ToolError for deliberate refusals and WebLogic failures, so the reason
#        reaches the model instead of being swallowed as a generic 'Error executing tool'.
# v1.1 - changelog: use the SDK's native snake_case ToolAnnotations field names.
# v1.0
"""Four tools over the WebLogic REST management API, with an allowlist and an audit trail."""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .audit import audit, configure_logging
from .client import WlsClient, WlsError
from .config import Config

log = logging.getLogger("wls_mcp.server")

CONFIG = Config.from_env()
CLIENT = WlsClient(CONFIG)

ACTIONS = {"start": "start", "shutdown": "shutdown", "force_shutdown": "forceShutdown"}

# WebLogic server names are identifiers. Anything else is a caller error - and, unvalidated,
# a way to steer the REST path somewhere it was not meant to go.
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Text and collections below come from WebLogic, not from us. They are shown to a model, so they
# are clipped: partly to bound response size, partly because untrusted text should not be handed
# to an LLM in unbounded quantity.
MAX_TEXT = 500
MAX_ITEMS = 50


def _clip_text(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"... [truncated, {len(value)} chars]"
    return value


def _clip_list(values: Any) -> list[Any]:
    if not isinstance(values, list):
        return []
    clipped = [_clip_text(v) for v in values[:MAX_ITEMS]]
    if len(values) > MAX_ITEMS:
        clipped.append(f"... [truncated, {len(values)} items total]")
    return clipped

mcp = MCPServer(
    name="weblogic",
    version="1.0.0",
    instructions=(
        "Read-only inspection and lifecycle control for an Oracle WebLogic Server domain, over "
        "the WebLogic REST management API. Use list_servers first to discover server names and "
        "their current state; health and JVM statistics are only available for RUNNING servers."
    ),
)


def _check_server_allowed(server_name: str) -> None:
    if not isinstance(server_name, str) or not server_name.strip():
        raise ToolError("server_name is required and must be a string.")
    if not SERVER_NAME_PATTERN.fullmatch(server_name):
        raise ToolError(
            f"'{_clip_text(server_name)}' is not a valid WebLogic server name. Names must start "
            "with a letter or digit and contain only letters, digits, dots, underscores or hyphens."
        )
    if not CONFIG.is_allowed(server_name):
        raise ToolError(
            f"Server '{server_name}' is not in this MCP server's allowlist "
            f"({', '.join(CONFIG.allowed_servers)}). Refusing."
        )


def _segment(server_name: str) -> str:
    """Percent-encode a server name for use as a single REST path segment."""
    return quote(server_name, safe="")


def _lifecycle_state(server_name: str) -> str | None:
    """Authoritative state for any configured server, running or not."""
    body = CLIENT.get(f"/domainRuntime/serverLifeCycleRuntimes/{_segment(server_name)}", fields=["state"])
    return body.get("state")


def _guarded(tool: str, arguments: dict[str, Any], target: str | None, work):
    """Run a tool body, emitting exactly one audit record either way."""
    started = time.monotonic()
    try:
        result = work()
    except WlsError as exc:
        audit(
            tool=tool, principal=CONFIG.username, target=target, arguments=arguments,
            outcome="error", duration_ms=int((time.monotonic() - started) * 1000),
            http_status=exc.http_status, error=str(exc),
        )
        # Re-raised as ToolError so the WebLogic message itself reaches the model.
        raise ToolError(str(exc)) from exc
    except ToolError as exc:
        audit(
            tool=tool, principal=CONFIG.username, target=target, arguments=arguments,
            outcome="refused", duration_ms=int((time.monotonic() - started) * 1000), error=str(exc),
        )
        raise
    audit(
        tool=tool, principal=CONFIG.username, target=target, arguments=arguments,
        outcome="success", duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


@mcp.tool(
    description=(
        "List every configured server in the WebLogic domain with its current lifecycle state "
        "(RUNNING, SHUTDOWN, STARTING, FAILED_NOT_RESTARTABLE, ...), listen port and assigned "
        "machine. This is the only view that includes stopped servers."
    ),
    annotations=ToolAnnotations(title="List WebLogic servers", read_only_hint=True),
)
def list_servers() -> dict[str, Any]:
    def work() -> dict[str, Any]:
        states = CLIENT.get("/domainRuntime/serverLifeCycleRuntimes", fields=["name", "state"])
        config = CLIENT.get("/edit/servers", fields=["name", "listenPort", "machine"])

        by_name = {item["name"]: item for item in config.get("items", []) if item.get("name")}
        all_items = states.get("items", [])
        servers = []
        for item in all_items[:MAX_ITEMS]:
            name = item.get("name")
            details = by_name.get(name, {})
            machine = details.get("machine")
            servers.append(
                {
                    "name": _clip_text(name),
                    "state": _clip_text(item.get("state")),
                    "listen_port": details.get("listenPort"),
                    "machine": _clip_text(machine[-1]) if isinstance(machine, list) and machine else None,
                    "is_admin_server": name == CONFIG.admin_server_name,
                    "in_scope": CONFIG.is_allowed(name),
                }
            )
        result = {"domain_url": CONFIG.base_url, "server_count": len(all_items), "servers": servers}
        if len(all_items) > MAX_ITEMS:
            result["note"] = f"Showing the first {MAX_ITEMS} of {len(all_items)} servers."
        return result

    return _guarded("list_servers", {}, None, work)


@mcp.tool(
    description=(
        "Report the health of one WebLogic server: overall health state (ok, warn, critical, "
        "failed, overloaded), the subsystem at fault if any, and reported symptoms. A server "
        "that is not RUNNING has no health data; its lifecycle state is returned instead."
    ),
    annotations=ToolAnnotations(title="Get WebLogic server health", read_only_hint=True),
)
def get_server_health(server_name: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        _check_server_allowed(server_name)
        state = _lifecycle_state(server_name)
        if state != "RUNNING":
            return {
                "server": server_name,
                "state": state,
                "health": None,
                "note": "Health data is only published by a RUNNING server.",
            }
        body = CLIENT.get(
            f"/domainRuntime/serverRuntimes/{_segment(server_name)}", fields=["name", "state", "healthState"]
        )
        health = body.get("healthState") or {}
        return {
            "server": server_name,
            "state": body.get("state", state),
            "health": health.get("state"),
            "failed_subsystem": _clip_text(health.get("subsystemName")),
            "symptoms": _clip_list(health.get("symptoms")),
        }

    return _guarded("get_server_health", {"server_name": server_name}, server_name, work)


@mcp.tool(
    description=(
        "Return JVM heap and runtime statistics for one RUNNING WebLogic server: current and "
        "maximum heap, free heap and free percentage, uptime, and the Java version in use. "
        "Byte values are also given in MB for readability."
    ),
    annotations=ToolAnnotations(title="Get WebLogic JVM statistics", read_only_hint=True),
)
def get_jvm_stats(server_name: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        _check_server_allowed(server_name)
        state = _lifecycle_state(server_name)
        if state != "RUNNING":
            return {
                "server": server_name,
                "state": state,
                "jvm": None,
                "note": "JVM statistics are only published by a RUNNING server.",
            }
        body = CLIENT.get(
            f"/domainRuntime/serverRuntimes/{_segment(server_name)}/JVMRuntime",
            fields=[
                "heapSizeCurrent", "heapFreeCurrent", "heapFreePercent",
                "heapSizeMax", "uptime", "javaVersion", "javaVendor",
            ],
        )
        mb = lambda v: round(v / 1_048_576, 1) if isinstance(v, (int, float)) else None
        uptime_ms = body.get("uptime")
        return {
            "server": server_name,
            "state": state,
            "java_version": _clip_text(body.get("javaVersion")),
            "java_vendor": _clip_text(body.get("javaVendor")),
            "uptime_seconds": round(uptime_ms / 1000) if isinstance(uptime_ms, (int, float)) else None,
            "heap_current_mb": mb(body.get("heapSizeCurrent")),
            "heap_max_mb": mb(body.get("heapSizeMax")),
            "heap_free_mb": mb(body.get("heapFreeCurrent")),
            "heap_free_percent": body.get("heapFreePercent"),
        }

    return _guarded("get_jvm_stats", {"server_name": server_name}, server_name, work)


@mcp.tool(
    description=(
        "Start or stop a WebLogic server through Node Manager. action is one of: 'start', "
        "'shutdown' (graceful), 'force_shutdown' (immediate, use to clear a wedged server). "
        "Stopping the Administration Server also stops the REST API this tool depends on, so it "
        "requires confirm=true and cannot be undone through this tool."
    ),
    annotations=ToolAnnotations(
        title="Start or stop a WebLogic server", read_only_hint=False, destructive_hint=True, idempotent_hint=False
    ),
)
def control_server(server_name: str, action: str, confirm: bool = False) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        if CONFIG.read_only:
            raise ToolError("This MCP server runs in read-only mode (WLS_READ_ONLY); refusing to change state.")
        if not isinstance(action, str) or action not in ACTIONS:
            raise ToolError(f"Unknown action '{action}'. Use one of: {', '.join(sorted(ACTIONS))}.")
        _check_server_allowed(server_name)

        is_admin = server_name == CONFIG.admin_server_name
        stopping = action in ("shutdown", "force_shutdown")
        if is_admin and stopping:
            if not CONFIG.allow_admin_shutdown:
                raise ToolError(
                    f"Stopping '{server_name}' is disabled on this MCP server (WLS_ALLOW_ADMIN_SHUTDOWN=false)."
                )
            # Exactly True. Not "true", not 1 - a confirmation gate should never be satisfied
            # by something that merely looks truthy.
            if confirm is not True:
                raise ToolError(
                    f"'{server_name}' is the Administration Server. Stopping it also stops the REST API "
                    "that this MCP server uses, so no tool here can start it again. Re-issue with "
                    "confirm=true only if that is genuinely intended."
                )

        before = _lifecycle_state(server_name)
        body = CLIENT.invoke(
            f"/domainRuntime/serverLifeCycleRuntimes/{_segment(server_name)}/{ACTIONS[action]}"
        )

        result: dict[str, Any] = {
            "server": server_name,
            "action": action,
            "state_before": before,
            "task_status": _clip_text(body.get("taskStatus")),
            "progress": _clip_text(body.get("progress")),
            "completed": body.get("completed"),
            "task_error": _clip_text(body.get("taskError")),
            "description": _clip_text(body.get("description")),
        }
        if is_admin and stopping:
            result["recovery"] = (
                "The Administration Server is down and cannot be restarted through this MCP server. "
                "Restart it on the host, e.g. `docker restart <weblogic-container>`."
            )
            result["state_after"] = "SHUTDOWN"
            return result
        try:
            result["state_after"] = _lifecycle_state(server_name)
        except WlsError as exc:
            result["state_after"] = None
            result["note"] = f"Action submitted, but the follow-up state read failed: {exc}"
        return result

    return _guarded(
        "control_server",
        {"server_name": server_name, "action": action, "confirm": confirm},
        server_name,
        work,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="wls-mcp", description="MCP server for Oracle WebLogic Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.getenv("WLS_MCP_TRANSPORT", "stdio"),
        help="stdio for a local client such as Codex or Claude Desktop; streamable-http to serve over the network.",
    )
    parser.add_argument("--host", default=os.getenv("WLS_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WLS_MCP_PORT", "8765")))
    args = parser.parse_args()

    configure_logging(os.getenv("WLS_LOG_LEVEL", "INFO"), CONFIG.audit_log_path)
    log.info(
        "starting transport=%s domain=%s principal=%s read_only=%s allowlist=%s",
        args.transport, CONFIG.base_url, CONFIG.username, CONFIG.read_only,
        ",".join(CONFIG.allowed_servers) or "(all servers)",
    )

    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="streamable-http", host=args.host, port=args.port)
    finally:
        CLIENT.close()


if __name__ == "__main__":
    main()
