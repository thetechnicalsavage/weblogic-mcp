# v1.2 - changelog: apply the body-size cap to error responses too. Previously only successful
#        bodies were checked, so an oversized 4xx/5xx body still reached the error parser.
# v1.1 - changelog: cap the accepted response body size before decoding JSON.
# v1.0
"""Thin HTTP client for the WebLogic RESTful Management Services.

Endpoint notes that are easy to get wrong, all verified against WebLogic 15.1.1.0:

* ``serverLifeCycleRuntimes`` lists *every configured* server with its state. This is the only
  place a SHUTDOWN server appears.
* ``serverRuntimes`` lists only servers that are currently RUNNING. A stopped server is absent
  entirely, and the collection can be briefly empty while a server registers with the domain
  runtime service.
* Any state-changing request must carry ``X-Requested-By``; WebLogic rejects it as CSRF otherwise.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import Config

log = logging.getLogger("wls_mcp.client")

_RETRYABLE_ATTEMPTS = 3
_BACKOFF_START = 0.5
_BACKOFF_CAP = 4.0
# A management response should never be large. Refusing an oversized body keeps a misconfigured
# or hostile endpoint from pushing an unbounded payload into the model's context.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class WlsError(RuntimeError):
    """A WebLogic call failed in a way the caller should see verbatim."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class WlsClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.management_url,
            auth=(config.username, config.password),
            verify=config.verify_tls,
            timeout=config.timeout,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    # ---- public API -------------------------------------------------------

    def get(self, path: str, fields: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, str] = {"links": "none"}
        if fields:
            params["fields"] = ",".join(fields)
        return self._request("GET", path, retry=True, params=params)

    def invoke(self, path: str) -> dict[str, Any]:
        """POST a lifecycle action. Never retried: re-sending a start or shutdown is not safe."""
        return self._request(
            "POST",
            path,
            retry=False,
            json={},
            timeout=self._config.lifecycle_timeout,
            headers={"Content-Type": "application/json", "X-Requested-By": "wls-mcp"},
        )

    # ---- internals --------------------------------------------------------

    def _request(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> dict[str, Any]:
        attempts = _RETRYABLE_ATTEMPTS if retry else 1
        backoff = _BACKOFF_START
        last: WlsError | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TimeoutException:
                last = WlsError(f"WebLogic did not respond within the timeout for {method} {path}.")
            except httpx.ConnectError as exc:
                last = WlsError(f"Cannot reach WebLogic at {self._config.base_url}: {exc}")
            except httpx.HTTPError as exc:
                last = WlsError(f"HTTP transport error calling {method} {path}: {exc}")
            else:
                return self._decode(response, path)

            if attempt < attempts:
                log.warning("%s %s failed (%s); retrying in %.1fs", method, path, last, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)

        raise last  # type: ignore[misc]

    def _decode(self, response: httpx.Response, path: str) -> dict[str, Any]:
        status = response.status_code

        # Checked before anything parses the body, errors included: an oversized failure
        # response is just as capable of flooding the caller as an oversized success.
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise WlsError(
                f"WebLogic returned {len(response.content)} bytes for {path}, above the "
                f"{_MAX_RESPONSE_BYTES} byte limit; refusing to read it.",
                status,
            )

        if status == 401:
            raise WlsError(
                "WebLogic rejected the credentials (401). Check WLS_USERNAME and WLS_PASSWORD.", status
            )
        if status == 404:
            raise WlsError(f"WebLogic has no resource at {path} (404).", status)
        if status >= 400:
            detail = self._error_detail(response)
            if status == 403 or "403" in detail:
                raise WlsError(
                    f"WebLogic denied the operation for user '{self._config.username}': {detail} "
                    "The service account does not hold the required role.",
                    status,
                )
            raise WlsError(f"WebLogic returned {status} for {path}: {detail}", status)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise WlsError(f"WebLogic returned a non-JSON body for {path}: {exc}", status)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """WebLogic wraps failures in an envelope; pull out the human-readable part."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:300].strip() or "(empty response body)"
        details = body.get("wls:errorsDetails") or []
        messages = [
            " ".join(str(d.get(key, "")) for key in ("title", "detail") if d.get(key)).strip()
            for d in details
            if isinstance(d, dict)
        ]
        messages = [m for m in messages if m]
        if messages:
            return " | ".join(messages)
        for key in ("detail", "title", "message"):
            if body.get(key):
                return str(body[key])
        return response.text[:300].strip() or "(no detail supplied)"
