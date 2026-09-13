# v1.0
"""Structured audit trail.

Two rules drive this module:

1. Nothing may ever be written to stdout. Under the stdio transport, stdout *is* the MCP
   protocol channel, and a stray print corrupts the session.
2. Every tool invocation produces exactly one audit record, whether it succeeded or failed.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_AUDIT = logging.getLogger("wls_mcp.audit")


def configure_logging(level: str = "INFO", audit_log_path: str | None = None) -> None:
    """Send diagnostics to stderr, and audit records to stderr plus an optional file."""
    root = logging.getLogger("wls_mcp")
    root.setLevel(level.upper())
    root.propagate = False
    if root.handlers:
        return

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s"))
    root.addHandler(stderr)

    if audit_log_path:
        try:
            handler = logging.FileHandler(audit_log_path, encoding="utf-8")
        except OSError as exc:
            root.warning("audit log file %s unusable (%s); auditing to stderr only", audit_log_path, exc)
        else:
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.addFilter(lambda record: record.name == "wls_mcp.audit")
            root.addHandler(handler)


def audit(
    *,
    tool: str,
    principal: str,
    outcome: str,
    duration_ms: int,
    arguments: dict[str, Any] | None = None,
    target: str | None = None,
    http_status: int | None = None,
    error: str | None = None,
) -> None:
    """Emit one JSON audit record. Arguments are tool parameters only, never credentials."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "tool": tool,
        "principal": principal,
        "target": target,
        "arguments": arguments or {},
        "outcome": outcome,
        "duration_ms": duration_ms,
        "http_status": http_status,
        "error": error,
    }
    _AUDIT.info(json.dumps({k: v for k, v in record.items() if v is not None}, sort_keys=True))
