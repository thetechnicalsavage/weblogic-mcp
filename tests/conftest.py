# v1.1 - changelog: force the test environment instead of using setdefault. With setdefault, a
#        real WLS_BASE_URL exported in the developer's shell leaked into the mocked tests and
#        every respx route missed, producing nine confusing failures.
# v1.0
"""Environment must exist before wls_mcp.server is imported: it builds its Config at import time."""

import os

# Deliberately unconditional: unit tests must never reach a real WebLogic domain.
os.environ["WLS_BASE_URL"] = "http://wls.test:7001"
os.environ["WLS_USERNAME"] = "wlsmcp"
os.environ["WLS_PASSWORD"] = "not-a-real-password"
for leaked in ("WLS_ALLOWED_SERVERS", "WLS_READ_ONLY", "WLS_ALLOW_ADMIN_SHUTDOWN", "WLS_AUDIT_LOG"):
    os.environ.pop(leaked, None)
