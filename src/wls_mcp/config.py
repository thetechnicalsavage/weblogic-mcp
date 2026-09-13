# v1.1 - changelog: WLS_ALLOW_ADMIN_SHUTDOWN now defaults to false (secure by default), and
#        WLS_DESTRUCTIVE_TOKEN adds a break-glass secret that an agent cannot talk its way past.
# v1.0
"""Configuration, read once from the environment at startup.

Credentials are never read from the command line, so they do not appear in `ps` output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in _TRUE


def _seconds(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}")
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    base_url: str
    username: str
    password: str
    verify_tls: bool = True
    read_only: bool = False
    allow_admin_shutdown: bool = False
    destructive_token: str | None = None
    allowed_servers: tuple[str, ...] = ()
    timeout: float = 30.0
    lifecycle_timeout: float = 180.0
    audit_log_path: str | None = None
    admin_server_name: str = "AdminServer"

    @classmethod
    def from_env(cls) -> "Config":
        username = os.getenv("WLS_USERNAME")
        password = os.getenv("WLS_PASSWORD")
        missing = [n for n, v in (("WLS_USERNAME", username), ("WLS_PASSWORD", password)) if not v]
        if missing:
            raise SystemExit(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Set them to the least-privilege WebLogic service account, never to the domain admin."
            )
        allowed = tuple(s.strip() for s in os.getenv("WLS_ALLOWED_SERVERS", "").split(",") if s.strip())
        return cls(
            base_url=os.getenv("WLS_BASE_URL", "http://localhost:7001").rstrip("/"),
            username=username,  # type: ignore[arg-type]
            password=password,  # type: ignore[arg-type]
            verify_tls=_flag("WLS_VERIFY_TLS", True),
            read_only=_flag("WLS_READ_ONLY", False),
            allow_admin_shutdown=_flag("WLS_ALLOW_ADMIN_SHUTDOWN", False),
            destructive_token=os.getenv("WLS_DESTRUCTIVE_TOKEN") or None,
            allowed_servers=allowed,
            timeout=_seconds("WLS_TIMEOUT_SECONDS", 30.0),
            lifecycle_timeout=_seconds("WLS_LIFECYCLE_TIMEOUT_SECONDS", 180.0),
            audit_log_path=os.getenv("WLS_AUDIT_LOG") or None,
            admin_server_name=os.getenv("WLS_ADMIN_SERVER_NAME", "AdminServer"),
        )

    @property
    def management_url(self) -> str:
        return f"{self.base_url}/management/weblogic/latest"

    def is_allowed(self, server_name: str) -> bool:
        """An empty allowlist means every server in the domain is in scope."""
        return not self.allowed_servers or server_name in self.allowed_servers
