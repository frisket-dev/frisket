"""Installation-scoped Frisket Cloud connection storage and HTTP client."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from filelock import FileLock
from pydantic import ValidationError

from frisket.contracts.http.cloud_connection import (
    CloudAccountProviderKeysResponse,
    CloudCapabilitiesResponse,
    CloudDeviceAuthorizationRequest,
    CloudDeviceAuthorizationResponse,
    CloudOAuthError,
    CloudSessionResponse,
    CloudTokenRequest,
    CloudTokenResponse,
)
from frisket.team.security.secrets import (
    decrypt_secret,
    encrypt_secret,
    load_or_create_secret_key,
)


DEFAULT_CLOUD_ORIGIN = "https://app.frisket.dev"
CLOUD_PROFILE_DIR_ENV = "FRISKET_CLOUD_PROFILE_DIR"
CLOUD_ORIGIN_ENV = "FRISKET_CLOUD_ORIGIN"
_STATE_VERSION = 1
_STATE_FILENAME = "cloud_connection.json"
_KEY_FILENAME = "cloud_connection.key"


class CloudConnectionStoreError(RuntimeError):
    """The installation connection state cannot be read or updated safely."""


class CloudClientError(RuntimeError):
    """A safe, credential-free Cloud transport failure."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class StoredCloudConnection:
    access_token: str = field(repr=False)
    session: CloudSessionResponse


@dataclass(frozen=True)
class StoredCloudProfile:
    connection: StoredCloudConnection | None
    preference: str
    capabilities: dict[int, CloudCapabilitiesResponse] = field(default_factory=dict)


def cloud_profile_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the installation profile directory, independent of workspace."""

    values = os.environ if env is None else env
    explicit = values.get(CLOUD_PROFILE_DIR_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise CloudConnectionStoreError(
                f"{CLOUD_PROFILE_DIR_ENV} must be an absolute path"
            )
        return path
    if sys.platform == "win32":
        base = values.get("APPDATA", "").strip()
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "Frisket"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Frisket"
    base = values.get("XDG_CONFIG_HOME", "").strip()
    return (Path(base).expanduser() if base else Path.home() / ".config") / "frisket"


def cloud_origin(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return validate_cloud_origin(values.get(CLOUD_ORIGIN_ENV, DEFAULT_CLOUD_ORIGIN))


def validate_cloud_origin(value: str) -> str:
    """Admit one fixed HTTPS origin, or loopback HTTP for development."""

    raw = value.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Frisket Cloud origin") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            import ipaddress

            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in ({"https"} if not loopback else {"http", "https"})
    ):
        raise ValueError("invalid Frisket Cloud origin")
    authority = hostname
    if ":" in hostname and not hostname.startswith("["):
        authority = f"[{hostname}]"
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


class CloudConnectionStore:
    """Atomic owner-only profile state with an encrypted bearer token."""

    def __init__(self, profile_dir: str | Path | None = None) -> None:
        self.profile_dir = (
            Path(profile_dir) if profile_dir is not None else cloud_profile_dir()
        )
        if not self.profile_dir.is_absolute():
            raise CloudConnectionStoreError("Cloud profile directory must be absolute")
        self.path = self.profile_dir / _STATE_FILENAME
        self.key_path = self.profile_dir / "secrets" / _KEY_FILENAME
        self.lock_path = self.profile_dir / f".{_STATE_FILENAME}.lock"

    def load(self) -> StoredCloudProfile:
        with self._lock():
            return self._load_unlocked()

    def save_connection(
        self, access_token: str, session: CloudSessionResponse
    ) -> StoredCloudProfile:
        if not access_token:
            raise ValueError("Cloud access token cannot be empty")
        with self._lock():
            preference = self._preference_unlocked()
            capabilities: dict[int, CloudCapabilitiesResponse] = {}
            try:
                current = self._load_unlocked()
            except CloudConnectionStoreError:
                current = None
            if (
                current is not None
                and current.connection is not None
                and current.connection.session.user.id == session.user.id
            ):
                capabilities = current.capabilities
            previous = self.path.read_bytes() if self.path.exists() else None
            key = load_or_create_secret_key(self.key_path)
            payload = {
                "version": _STATE_VERSION,
                "preference": preference,
                "connection": {
                    "access_token": encrypt_secret(access_token, master_key=key),
                    "session": session.model_dump(mode="json", by_alias=True),
                },
                "capabilities": self._capabilities_payload(
                    session.user.id, capabilities
                ),
            }
            try:
                self._write_unlocked(payload)
            except OSError:
                try:
                    self._restore_unlocked(previous)
                except OSError:
                    pass
                raise
            return StoredCloudProfile(
                connection=StoredCloudConnection(access_token, session),
                preference=preference,
                capabilities=capabilities,
            )

    def save_capabilities(
        self,
        capabilities: CloudCapabilitiesResponse,
        *,
        expected_user_id: int,
    ) -> StoredCloudProfile:
        if type(expected_user_id) is not int or expected_user_id < 1:
            raise ValueError("expected Cloud user id must be a positive integer")
        with self._lock():
            current = self._load_unlocked()
            if current.connection is None:
                raise CloudConnectionStoreError(
                    "Frisket Cloud must be connected before capabilities can be saved"
                )
            if current.connection.session.user.id != expected_user_id:
                raise CloudConnectionStoreError(
                    "Frisket Cloud user changed while capabilities were fetched"
                )
            if not any(
                account.id == capabilities.account_id
                for account in current.connection.session.accounts
            ):
                raise CloudConnectionStoreError(
                    "Frisket Cloud account changed while capabilities were fetched"
                )
            snapshots = {**current.capabilities, capabilities.account_id: capabilities}
            key = load_or_create_secret_key(self.key_path)
            payload = {
                "version": _STATE_VERSION,
                "preference": current.preference,
                "connection": {
                    "access_token": encrypt_secret(
                        current.connection.access_token, master_key=key
                    ),
                    "session": current.connection.session.model_dump(
                        mode="json", by_alias=True
                    ),
                },
                "capabilities": self._capabilities_payload(
                    current.connection.session.user.id, snapshots
                ),
            }
            self._write_unlocked(payload)
            return StoredCloudProfile(
                connection=current.connection,
                preference=current.preference,
                capabilities=snapshots,
            )

    def clear_connection(self) -> StoredCloudProfile:
        with self._lock():
            preference = self._preference_unlocked()
            payload = {
                "version": _STATE_VERSION,
                "preference": preference,
                "connection": None,
                "capabilities": None,
            }
            self._write_unlocked(payload)
            return StoredCloudProfile(
                connection=None, preference=preference, capabilities={}
            )

    def set_preference(self, preference: str) -> StoredCloudProfile:
        if preference not in {"local_first", "cloud_first"}:
            raise ValueError("invalid Cloud execution preference")
        with self._lock():
            current = self._load_unlocked()
            payload: dict[str, Any] = {
                "version": _STATE_VERSION,
                "preference": preference,
                "connection": None,
                "capabilities": None,
            }
            if current.connection is not None:
                key = load_or_create_secret_key(self.key_path)
                payload["connection"] = {
                    "access_token": encrypt_secret(
                        current.connection.access_token, master_key=key
                    ),
                    "session": current.connection.session.model_dump(
                        mode="json", by_alias=True
                    ),
                }
                payload["capabilities"] = self._capabilities_payload(
                    current.connection.session.user.id, current.capabilities
                )
            self._write_unlocked(payload)
            return StoredCloudProfile(
                connection=current.connection,
                preference=preference,
                capabilities=current.capabilities,
            )

    def _lock(self) -> FileLock:
        self._ensure_profile_dir()
        return FileLock(str(self.lock_path), timeout=-1)

    def _ensure_profile_dir(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(0o700)
        except OSError:
            pass

    def _load_unlocked(self) -> StoredCloudProfile:
        if not self.path.exists():
            return StoredCloudProfile(connection=None, preference="local_first")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != _STATE_VERSION
            ):
                raise ValueError("unsupported state version")
            preference = payload.get("preference", "local_first")
            if preference not in {"local_first", "cloud_first"}:
                raise ValueError("invalid preference")
            value = payload.get("connection")
            if value is None:
                return StoredCloudProfile(connection=None, preference=preference)
            if not isinstance(value, dict):
                raise ValueError("connection must be an object")
            ciphertext = value.get("access_token")
            if not isinstance(ciphertext, str):
                raise ValueError("connection token is missing")
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise ValueError("invalid Cloud connection key")
            token = decrypt_secret(ciphertext, master_key=key)
            session = CloudSessionResponse.model_validate(value.get("session"))
            capabilities = self._load_capabilities(
                payload.get("capabilities"), session.user.id
            )
            return StoredCloudProfile(
                connection=StoredCloudConnection(token, session),
                preference=preference,
                capabilities=capabilities,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            raise CloudConnectionStoreError(
                "Frisket Cloud connection state is unreadable; reconnect this installation"
            ) from exc

    def _preference_unlocked(self) -> str:
        """Recover a valid preference without requiring a readable connection."""

        if not self.path.exists():
            return "local_first"
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            preference = (
                payload.get("preference") if isinstance(payload, dict) else None
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return "local_first"
        return (
            preference
            if preference in {"local_first", "cloud_first"}
            else "local_first"
        )

    @staticmethod
    def _capabilities_payload(
        user_id: int, capabilities: Mapping[int, CloudCapabilitiesResponse]
    ) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "by_account_id": {
                str(account_id): response.model_dump(mode="json", by_alias=True)
                for account_id, response in capabilities.items()
            },
        }

    @staticmethod
    def _load_capabilities(
        value: object, signed_user_id: int
    ) -> dict[int, CloudCapabilitiesResponse]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("capabilities must be an object")
        if value.get("user_id") != signed_user_id:
            return {}
        by_account_id = value.get("by_account_id")
        if not isinstance(by_account_id, dict):
            raise ValueError("capability snapshots must be an object")
        capabilities: dict[int, CloudCapabilitiesResponse] = {}
        for key, raw in by_account_id.items():
            if not isinstance(key, str) or not key.isdecimal():
                raise ValueError("capability account id must be an integer")
            account_id = int(key)
            response = CloudCapabilitiesResponse.model_validate(raw)
            if response.account_id != account_id:
                raise ValueError("capability account id does not match its key")
            capabilities[account_id] = response
        return capabilities

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self._replace_unlocked(encoded)

    def _restore_unlocked(self, previous: bytes | None) -> None:
        if previous is not None:
            self._replace_unlocked(previous)
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        self._sync_profile_dir()

    def _replace_unlocked(self, encoded: bytes) -> None:
        self._ensure_profile_dir()
        fd, temp_name = tempfile.mkstemp(
            dir=str(self.profile_dir), prefix=".cloud-connection-", suffix=".tmp"
        )
        try:
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self._sync_profile_dir()
        except OSError:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _sync_profile_dir(self) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(self.profile_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class CloudConnectionClient:
    """Narrow fixed-origin client for the Desktop Cloud namespace."""

    def __init__(
        self,
        origin: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.origin = validate_cloud_origin(origin or cloud_origin())
        self._http = httpx.Client(
            base_url=self.origin,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def issue_device_authorization(
        self, device_name: str
    ) -> CloudDeviceAuthorizationResponse:
        body = CloudDeviceAuthorizationRequest(device_name=device_name)
        response = self._request(
            "POST",
            "/api/desktop/v1/device-authorizations",
            json=body.model_dump(mode="json"),
        )
        return self._parse(response, CloudDeviceAuthorizationResponse)

    def poll_token(self, device_code: str) -> CloudTokenResponse | CloudOAuthError:
        body = CloudTokenRequest(
            grant_type="urn:ietf:params:oauth:grant-type:device_code",
            device_code=device_code,
        )
        response = self._request(
            "POST",
            "/api/desktop/v1/token",
            data=body.model_dump(mode="json"),
            allowed_statuses={200, 400},
        )
        if response.status_code == 400:
            return self._parse(response, CloudOAuthError)
        return self._parse(response, CloudTokenResponse)

    def session(self, access_token: str) -> CloudSessionResponse:
        response = self._request(
            "GET",
            "/api/desktop/v1/session",
            headers=self._bearer(access_token),
        )
        return self._parse(response, CloudSessionResponse)

    def capabilities(
        self, access_token: str, account_id: int
    ) -> CloudCapabilitiesResponse:
        if isinstance(account_id, bool) or account_id < 1:
            raise ValueError("Cloud account id must be a positive integer")
        response = self._request(
            "GET",
            f"/api/desktop/v1/accounts/{account_id}/capabilities",
            headers=self._bearer(access_token),
            params={"include_unconfigured": "true"},
        )
        capabilities = self._parse(response, CloudCapabilitiesResponse)
        if capabilities.account_id != account_id:
            raise CloudClientError("invalid_cloud_response")
        return capabilities

    def provider_keys(
        self, access_token: str, account_id: int
    ) -> CloudAccountProviderKeysResponse:
        if isinstance(account_id, bool) or account_id < 1:
            raise ValueError("Cloud account id must be a positive integer")
        response = self._request(
            "GET",
            f"/api/desktop/v1/accounts/{account_id}/provider-keys",
            headers=self._bearer(access_token),
        )
        return self._parse(response, CloudAccountProviderKeysResponse)

    def revoke_session(self, access_token: str) -> bool:
        response = self._request(
            "DELETE",
            "/api/desktop/v1/session",
            headers=self._bearer(access_token),
            allowed_statuses={200, 204, 401},
        )
        return response.status_code in {200, 204}

    def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] = {200},
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudClientError("cloud_unreachable", retryable=True) from exc
        if response.is_redirect:
            raise CloudClientError(
                "cloud_redirect_refused", status=response.status_code
            )
        if response.status_code not in allowed_statuses:
            raise CloudClientError(
                "cloud_unauthorized"
                if response.status_code in {401, 403}
                else "cloud_rate_limited"
                if response.status_code == 429
                else "cloud_request_failed",
                status=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        return response

    @staticmethod
    def _bearer(access_token: str) -> dict[str, str]:
        if not access_token or "\r" in access_token or "\n" in access_token:
            raise CloudClientError("invalid_local_credential")
        return {"Authorization": f"Bearer {access_token}"}

    @staticmethod
    def _parse(response: httpx.Response, model: type[Any]) -> Any:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise CloudClientError("invalid_cloud_response") from exc


__all__ = [
    "CLOUD_ORIGIN_ENV",
    "CLOUD_PROFILE_DIR_ENV",
    "DEFAULT_CLOUD_ORIGIN",
    "CloudClientError",
    "CloudConnectionClient",
    "CloudConnectionStore",
    "CloudConnectionStoreError",
    "StoredCloudConnection",
    "StoredCloudProfile",
    "cloud_origin",
    "cloud_profile_dir",
    "validate_cloud_origin",
]
