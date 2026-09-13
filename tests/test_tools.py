# v1.0
"""Unit tests for the four tools. No live WebLogic: the REST API is mocked with respx."""

import dataclasses

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from wls_mcp import server
from wls_mcp.client import WlsError

BASE = "http://wls.test:7001/management/weblogic/latest"
LIFECYCLE = f"{BASE}/domainRuntime/serverLifeCycleRuntimes"
RUNTIMES = f"{BASE}/domainRuntime/serverRuntimes"


@pytest.fixture(autouse=True)
def _quiet_audit(caplog):
    caplog.set_level("CRITICAL", logger="wls_mcp.audit")


@pytest.fixture
def config_override():
    """Swap server.CONFIG for a variant, and restore it afterwards."""
    original = server.CONFIG

    def apply(**changes):
        server.CONFIG = dataclasses.replace(original, **changes)
        return server.CONFIG

    yield apply
    server.CONFIG = original


# ---- list_servers ---------------------------------------------------------


@respx.mock
def test_list_servers_merges_lifecycle_and_config():
    respx.get(LIFECYCLE).mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"name": "AdminServer", "state": "RUNNING"}, {"name": "ms1", "state": "SHUTDOWN"}]},
        )
    )
    respx.get(f"{BASE}/edit/servers").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"name": "AdminServer", "listenPort": 7001, "machine": None},
                    {"name": "ms1", "listenPort": 8001, "machine": ["machines", "machine1"]},
                ]
            },
        )
    )

    result = server.list_servers()

    assert result["server_count"] == 2
    admin, ms1 = result["servers"]
    assert admin["is_admin_server"] is True
    assert ms1["state"] == "SHUTDOWN", "a stopped server must still be listed"
    assert ms1["machine"] == "machine1", "machine identity array is flattened to its last element"


# ---- health / jvm ---------------------------------------------------------


@respx.mock
def test_health_of_stopped_server_reports_state_not_error():
    respx.get(f"{LIFECYCLE}/ms1").mock(return_value=httpx.Response(200, json={"state": "SHUTDOWN"}))

    result = server.get_server_health("ms1")

    assert result["health"] is None
    assert result["state"] == "SHUTDOWN"
    assert "RUNNING" in result["note"]


@respx.mock
def test_health_of_running_server_surfaces_symptoms():
    respx.get(f"{LIFECYCLE}/ms1").mock(return_value=httpx.Response(200, json={"state": "RUNNING"}))
    respx.get(f"{RUNTIMES}/ms1").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "ms1",
                "state": "RUNNING",
                "healthState": {"state": "warn", "subsystemName": "JDBC", "symptoms": ["pool exhausted"]},
            },
        )
    )

    result = server.get_server_health("ms1")

    assert result["health"] == "warn"
    assert result["failed_subsystem"] == "JDBC"
    assert result["symptoms"] == ["pool exhausted"]


@respx.mock
def test_jvm_stats_convert_bytes_to_megabytes():
    respx.get(f"{LIFECYCLE}/ms1").mock(return_value=httpx.Response(200, json={"state": "RUNNING"}))
    respx.get(f"{RUNTIMES}/ms1/JVMRuntime").mock(
        return_value=httpx.Response(
            200,
            json={
                "heapSizeCurrent": 536870912,   # 512 MB
                "heapSizeMax": 1073741824,      # 1024 MB
                "heapFreeCurrent": 268435456,   # 256 MB
                "heapFreePercent": 50,
                "uptime": 90000,                # 90 s
                "javaVersion": "21.0.8",
                "javaVendor": "Oracle Corporation",
            },
        )
    )

    result = server.get_jvm_stats("ms1")

    assert result["heap_current_mb"] == 512.0
    assert result["heap_max_mb"] == 1024.0
    assert result["heap_free_mb"] == 256.0
    assert result["uptime_seconds"] == 90


# ---- control_server guardrails -------------------------------------------


@respx.mock
def test_stopping_admin_server_requires_confirmation():
    with pytest.raises(ToolError, match="confirm=true"):
        server.control_server("AdminServer", "shutdown")
    assert not respx.calls, "refusal must happen before any request reaches WebLogic"


@respx.mock
def test_stopping_admin_server_proceeds_with_confirmation_and_returns_recovery():
    respx.get(f"{LIFECYCLE}/AdminServer").mock(return_value=httpx.Response(200, json={"state": "RUNNING"}))
    respx.post(f"{LIFECYCLE}/AdminServer/shutdown").mock(
        return_value=httpx.Response(200, json={"taskStatus": "TASK COMPLETED", "progress": "success", "completed": True})
    )

    result = server.control_server("AdminServer", "shutdown", confirm=True)

    assert result["state_after"] == "SHUTDOWN"
    assert "docker restart" in result["recovery"]


@respx.mock
def test_admin_shutdown_can_be_disabled_entirely(config_override):
    config_override(allow_admin_shutdown=False)
    with pytest.raises(ToolError, match="disabled"):
        server.control_server("AdminServer", "shutdown", confirm=True)


@respx.mock
def test_read_only_mode_blocks_every_state_change(config_override):
    config_override(read_only=True)
    with pytest.raises(ToolError, match="read-only"):
        server.control_server("ms1", "start")
    assert not respx.calls


@respx.mock
def test_allowlist_blocks_servers_outside_scope(config_override):
    config_override(allowed_servers=("ms1",))
    with pytest.raises(ToolError, match="allowlist"):
        server.control_server("ms2", "start")
    assert not respx.calls


@respx.mock
def test_unknown_action_is_rejected_before_any_call():
    with pytest.raises(ToolError, match="Unknown action"):
        server.control_server("ms1", "restart")
    assert not respx.calls


@respx.mock
def test_lifecycle_post_sends_csrf_header_and_is_not_retried():
    respx.get(f"{LIFECYCLE}/ms1").mock(return_value=httpx.Response(200, json={"state": "SHUTDOWN"}))
    route = respx.post(f"{LIFECYCLE}/ms1/start").mock(
        return_value=httpx.Response(200, json={"taskStatus": "TASK COMPLETED", "progress": "success"})
    )

    server.control_server("ms1", "start")

    assert route.call_count == 1
    assert route.calls[0].request.headers["X-Requested-By"] == "wls-mcp"


@respx.mock
def test_lifecycle_failure_is_not_retried():
    respx.get(f"{LIFECYCLE}/ms1").mock(return_value=httpx.Response(200, json={"state": "SHUTDOWN"}))
    route = respx.post(f"{LIFECYCLE}/ms1/start").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(ToolError):
        server.control_server("ms1", "start")
    assert route.call_count == 1, "a start must never be sent twice"


# ---- error mapping --------------------------------------------------------


@respx.mock
def test_denied_operation_names_the_principal():
    respx.get(f"{LIFECYCLE}/ms1").mock(
        return_value=httpx.Response(
            403, json={"wls:errorsDetails": [{"title": "HTTP 403 Forbidden", "detail": "Failed to start an edit session."}]}
        )
    )

    with pytest.raises(ToolError, match="wlsmcp"):
        server.get_server_health("ms1")


@respx.mock
def test_transient_get_failure_is_retried(monkeypatch):
    monkeypatch.setattr("wls_mcp.client.time.sleep", lambda _: None)
    route = respx.get(f"{LIFECYCLE}/ms1").mock(
        side_effect=[httpx.ConnectError("reset"), httpx.Response(200, json={"state": "SHUTDOWN"})]
    )

    result = server.get_server_health("ms1")

    assert route.call_count == 2
    assert result["state"] == "SHUTDOWN"


def test_wls_error_carries_http_status():
    assert WlsError("nope", 403).http_status == 403
