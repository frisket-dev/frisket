"""HTTP client for the operator-token admin API.

Every call maps transport and HTTP failures onto the remote CLI's exit-code
contract: 2 for auth/pairing failures, 3 for validation errors, 1 for
everything else. Error messages are single lines with no payload echo.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_TIMEOUT = 15.0


class RemoteError(Exception):
    exit_code = 1


class RemoteAuthError(RemoteError):
    exit_code = 2


class RemoteValidationError(RemoteError):
    exit_code = 3


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
    return f"HTTP {response.status_code}"


class RemoteClient:
    def __init__(self, url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT):
        self._base = url.strip().rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if authenticated else {}
        try:
            response = httpx.request(
                method,
                f"{self._base}{path}",
                headers=headers,
                json=json,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise RemoteError(
                f"cannot reach {self._base} ({type(exc).__name__})"
            ) from exc
        if response.status_code in (401, 403):
            raise RemoteAuthError(
                f"server rejected the operator token ({_detail(response)})"
            )
        if response.status_code == 503:
            raise RemoteError(f"server is not ready ({_detail(response)})")
        if 400 <= response.status_code < 500:
            raise RemoteValidationError(_detail(response))
        if response.status_code >= 500:
            raise RemoteError(f"server error ({_detail(response)})")
        try:
            return response.json()
        except ValueError as exc:
            raise RemoteError("server returned a non-JSON response") from exc

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/ping")

    def ready(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self._base}/api/ready", timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise RemoteError(
                f"cannot reach {self._base} ({type(exc).__name__})"
            ) from exc
        try:
            return response.json()
        except ValueError as exc:
            raise RemoteError("server returned a non-JSON readiness response") from exc

    def rotate_token(self) -> dict[str, Any]:
        return self._request("POST", "/api/admin/token/rotate")

    def users_add(self, email: str, *, role: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"email": email}
        if role is not None:
            body["role"] = role
        return self._request("POST", "/api/admin/users", json=body)

    def users_list(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/users")

    def users_remove(self, email: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/admin/users/{email}")

    def users_reset(self, email: str) -> dict[str, Any]:
        return self._request("POST", f"/api/admin/users/{email}/reset")

    def users_role(self, email: str, role: str) -> dict[str, Any]:
        return self._request(
            "PUT", f"/api/admin/users/{email}/role", json={"role": role}
        )

    def secrets_list(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/secrets")

    def secrets_set(self, provider: str, key: str) -> dict[str, Any]:
        return self._request("PUT", f"/api/admin/secrets/{provider}", json={"key": key})

    def secrets_unset(self, provider: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/admin/secrets/{provider}")

    def media_proxy_get(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/media-proxy")

    def media_proxy_set(self, url: str) -> dict[str, Any]:
        return self._request("PUT", "/api/admin/media-proxy", json={"url": url})

    def media_proxy_clear(self) -> dict[str, Any]:
        return self._request("DELETE", "/api/admin/media-proxy")

    def media_proxy_check(self) -> dict[str, Any]:
        return self._request("POST", "/api/admin/media-proxy/check")


__all__ = [
    "RemoteAuthError",
    "RemoteClient",
    "RemoteError",
    "RemoteValidationError",
]
