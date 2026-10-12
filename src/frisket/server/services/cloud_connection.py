"""Solo backend service for installation sign-in and project Cloud routing."""

from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable

from frisket.cloud_connection import (
    CloudClientError,
    CloudConnectionClient,
    CloudConnectionStore,
    CloudConnectionStoreError,
    StoredCloudProfile,
)
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudConnectionAccountSummary,
    CloudConnectionCapabilities,
    CloudConnectionIdentity,
    CloudConnectionPreferencesResponse,
    CloudConnectionResponse,
    CloudConnectionSignOutResponse,
    CloudDeviceAuthorizationPollResponse,
    CloudDeviceAuthorizationStartResponse,
    CloudOAuthError,
    ProjectCloudRoutingPatch,
    ProjectCloudRoutingResponse,
)
from frisket.project_settings import read_project_settings, write_project_settings
from frisket.server.workspace import Workspace
from frisket.server.provider_config import PROVIDER_LABELS


class CloudConnectionServiceError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _PendingAuthorization:
    id: str
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: datetime
    poll_interval_seconds: int
    next_poll_at: float


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _money_label(micro: int) -> str:
    return f"${micro / 1_000_000:,.2f} balance"


class CloudConnectionService:
    """One installation connection shared by every local project and panel."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        store: CloudConnectionStore,
        client: CloudConnectionClient,
        device_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace = workspace
        self._store = store
        self._client = client
        self._device_name = (device_name or socket.gethostname() or "Frisket Desktop")[
            :120
        ]
        self._clock = clock
        self._wall_clock = wall_clock
        self._pending: dict[str, _PendingAuthorization] = {}
        self._completed: dict[str, tuple[datetime, CloudConnectionResponse]] = {}
        self._pending_lock = threading.Lock()
        self._connection_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def connection(self) -> CloudConnectionResponse:
        with self._connection_lock:
            return self._refresh_connection()

    def _refresh_connection(self) -> CloudConnectionResponse:
        try:
            profile = self._store.load()
        except CloudConnectionStoreError:
            return self._connection_response("expired", None)
        stored = self._stored_connection(profile)
        connection = profile.connection
        if connection is None or stored.state != "connected":
            return stored
        try:
            session = self._client.session(connection.access_token)
        except CloudClientError as exc:
            if exc.status in {401, 403} or exc.code == "invalid_local_credential":
                return self._connection_response("expired", connection.session)
            return stored
        try:
            self._store.save_connection(connection.access_token, session)
        except (CloudConnectionStoreError, OSError):
            return stored
        return self._connection_response("connected", session)

    def _stored_connection(
        self, profile: StoredCloudProfile | None = None
    ) -> CloudConnectionResponse:
        if profile is None:
            try:
                profile = self._store.load()
            except CloudConnectionStoreError:
                return self._connection_response("expired", None)
        connection = profile.connection
        if connection is None:
            return self._connection_response("disconnected", None)
        expires_at = _parse_time(connection.session.expires_at)
        state = (
            "expired"
            if expires_at is None or expires_at <= self._wall_clock()
            else "connected"
        )
        return self._connection_response(state, connection.session)

    def begin_authorization(self) -> CloudDeviceAuthorizationStartResponse:
        try:
            issued = self._client.issue_device_authorization(self._device_name)
        except CloudClientError as exc:
            raise CloudConnectionServiceError(
                503, "Frisket Cloud is unavailable"
            ) from exc
        now = self._wall_clock()
        pending = _PendingAuthorization(
            id=secrets.token_urlsafe(18),
            device_code=issued.device_code,
            user_code=issued.user_code,
            verification_uri=issued.verification_uri,
            verification_uri_complete=issued.verification_uri_complete,
            expires_at=now + timedelta(seconds=issued.expires_in),
            poll_interval_seconds=issued.interval,
            next_poll_at=self._clock() + issued.interval,
        )
        with self._pending_lock:
            self._discard_expired_pending()
            self._pending[pending.id] = pending
        return self._pending_response(pending)

    def poll_authorization(
        self, authorization_id: str
    ) -> CloudDeviceAuthorizationPollResponse:
        with self._pending_lock:
            pending = self._pending.get(authorization_id)
            if pending is None:
                completed = self._completed.get(authorization_id)
                if completed is not None:
                    expires_at, connection = completed
                    if expires_at > self._wall_clock():
                        return CloudDeviceAuthorizationPollResponse(
                            id=authorization_id,
                            status="connected",
                            connection=connection,
                        )
                    self._completed.pop(authorization_id, None)
                raise CloudConnectionServiceError(
                    404, "Cloud sign-in request not found"
                )
            if pending.expires_at <= self._wall_clock():
                self._pending.pop(authorization_id, None)
                return CloudDeviceAuthorizationPollResponse(
                    id=authorization_id,
                    status="expired",
                    connection=self._stored_connection(),
                )
            if self._clock() < pending.next_poll_at:
                return CloudDeviceAuthorizationPollResponse(
                    id=authorization_id,
                    status="pending",
                    connection=self._stored_connection(),
                )
            try:
                answer = self._client.poll_token(pending.device_code)
            except CloudClientError as exc:
                pending.next_poll_at = self._clock() + pending.poll_interval_seconds
                raise CloudConnectionServiceError(
                    503, "Frisket Cloud sign-in is temporarily unavailable"
                ) from exc
            if isinstance(answer, CloudOAuthError):
                if answer.error == "authorization_pending":
                    pending.next_poll_at = self._clock() + pending.poll_interval_seconds
                    status = "pending"
                elif answer.error == "slow_down":
                    pending.poll_interval_seconds += 5
                    pending.next_poll_at = self._clock() + pending.poll_interval_seconds
                    status = "pending"
                elif answer.error == "access_denied":
                    self._pending.pop(authorization_id, None)
                    status = "declined"
                else:
                    self._pending.pop(authorization_id, None)
                    status = "expired"
                return CloudDeviceAuthorizationPollResponse(
                    id=authorization_id,
                    status=status,
                    connection=self._stored_connection(),
                )
            try:
                with self._connection_lock:
                    session = self._client.session(answer.access_token)
                    self._store.save_connection(answer.access_token, session)
            except (CloudClientError, CloudConnectionStoreError, OSError) as exc:
                try:
                    self._client.revoke_session(answer.access_token)
                except CloudClientError:
                    pass
                raise CloudConnectionServiceError(
                    503, "Frisket Cloud sign-in could not be completed"
                ) from exc
            finally:
                # A token grant is single-use. Never poll the same device code
                # again, even if local persistence failed after token delivery.
                self._pending.pop(authorization_id, None)
            connection = self._stored_connection()
            self._completed[authorization_id] = (pending.expires_at, connection)
            return CloudDeviceAuthorizationPollResponse(
                id=authorization_id,
                status="connected",
                connection=connection,
            )

    def cancel_authorization(self, authorization_id: str) -> bool:
        with self._pending_lock:
            return self._pending.pop(authorization_id, None) is not None

    def sign_out(self) -> CloudConnectionSignOutResponse:
        unreadable_profile = False
        with self._pending_lock:
            self._pending.clear()
            self._completed.clear()
            with self._connection_lock:
                try:
                    profile = self._store.load()
                except CloudConnectionStoreError:
                    profile = None
                    unreadable_profile = True
                connection = profile.connection if profile is not None else None
                try:
                    self._store.clear_connection()
                except (CloudConnectionStoreError, OSError) as exc:
                    raise CloudConnectionServiceError(
                        500, "The local Cloud connection could not be cleared"
                    ) from exc
        revocation = "not_connected"
        warning = None
        if unreadable_profile:
            revocation = "failed"
            warning = (
                "Signed out locally, but the saved Cloud connection was unreadable. "
                "Revoke this device in Frisket Cloud."
            )
        elif connection is not None:
            try:
                revoked = self._client.revoke_session(connection.access_token)
                revocation = "revoked" if revoked else "failed"
                if not revoked:
                    warning = (
                        "Signed out locally; the remote device was already unavailable."
                    )
            except CloudClientError:
                revocation = "failed"
                warning = "Signed out locally, but Frisket Cloud could not confirm remote revocation."
        return CloudConnectionSignOutResponse(
            connection=self._stored_connection(), revocation=revocation, warning=warning
        )

    def preferences(self) -> CloudConnectionPreferencesResponse:
        try:
            preference = self._store.load().preference
        except CloudConnectionStoreError as exc:
            raise CloudConnectionServiceError(500, str(exc)) from exc
        return CloudConnectionPreferencesResponse(preference=preference)

    def set_preference(self, preference: str) -> CloudConnectionPreferencesResponse:
        try:
            profile = self._store.set_preference(preference)
        except (CloudConnectionStoreError, OSError, ValueError) as exc:
            raise CloudConnectionServiceError(
                500, "The Cloud preference could not be saved"
            ) from exc
        return CloudConnectionPreferencesResponse(preference=profile.preference)

    def project_routing(self, project_id: str) -> ProjectCloudRoutingResponse:
        connection = self.connection()
        self._refresh_project_capabilities(project_id, connection)
        return self._project_routing(project_id, connection)

    def _project_routing(
        self, project_id: str, connection: CloudConnectionResponse
    ) -> ProjectCloudRoutingResponse:
        project = self._workspace.get(project_id)
        settings = read_project_settings(project)
        signed_user_id = connection.identity.id if connection.identity else None
        selected = (
            settings.cloud_account_id
            if settings.cloud_user_id == signed_user_id
            else None
        )
        inherited = settings.cloud_execution_preference is None
        if inherited:
            try:
                preference = self.preferences().preference
            except CloudConnectionServiceError:
                preference = (
                    "cloud_first"
                    if settings.cloud_user_id is not None
                    and settings.cloud_account_id is not None
                    else "local_first"
                )
        else:
            preference = settings.cloud_execution_preference
        return ProjectCloudRoutingResponse(
            connection_state=connection.state,
            selected_account_id=selected,
            preference=preference,
            preference_inherited=inherited,
            accounts=[account for account in connection.accounts if account.eligible],
            overlap_provider_labels=self._overlap_provider_labels(project, selected),
            can_mutate=True,
        )

    def patch_project_routing(
        self, project_id: str, patch: ProjectCloudRoutingPatch
    ) -> ProjectCloudRoutingResponse:
        project = self._workspace.get(project_id)
        settings = read_project_settings(project)
        updates: dict[str, object] = {}
        connection = None
        if "preference" in patch.model_fields_set:
            updates["cloud_execution_preference"] = patch.preference
        if "selected_account_id" in patch.model_fields_set:
            connection = self.connection()
            if connection.state != "connected" or connection.identity is None:
                raise CloudConnectionServiceError(409, "Connect Frisket Cloud first")
            eligible_ids = {
                account.id for account in connection.accounts if account.eligible
            }
            if patch.selected_account_id not in eligible_ids:
                raise CloudConnectionServiceError(
                    409, "The selected Cloud account is not available"
                )
            updates["cloud_user_id"] = connection.identity.id
            updates["cloud_account_id"] = patch.selected_account_id
            self._refresh_capabilities(
                patch.selected_account_id,
                connection.identity.id,
            )
        write_project_settings(project, settings.model_copy(update=updates))
        return self._project_routing(
            project_id, connection if connection is not None else self.connection()
        )

    def _refresh_project_capabilities(
        self,
        project_id: str,
        connection: CloudConnectionResponse,
    ) -> None:
        project = self._workspace.get(project_id)
        settings = read_project_settings(project)
        identity = connection.identity
        if (
            connection.state != "connected"
            or identity is None
            or settings.cloud_user_id != identity.id
            or settings.cloud_account_id is None
        ):
            return
        self._refresh_capabilities(
            settings.cloud_account_id,
            identity.id,
        )

    def _refresh_capabilities(
        self,
        account_id: int,
        user_id: int,
    ) -> None:
        try:
            profile = self._store.load()
        except CloudConnectionStoreError:
            return
        connection = profile.connection
        if connection is None or connection.session.user.id != user_id:
            return
        try:
            capabilities = self._client.capabilities(
                connection.access_token, account_id
            )
            self._store.save_capabilities(
                capabilities,
                expected_user_id=user_id,
            )
        except (CloudClientError, CloudConnectionStoreError, OSError, ValueError):
            return

    def _overlap_provider_labels(
        self, project: object, selected_account_id: int | None
    ) -> list[str]:
        if selected_account_id is None:
            return []
        try:
            snapshot = self._store.load().capabilities.get(selected_account_id)
        except CloudConnectionStoreError:
            return []
        if snapshot is None:
            return []
        local = set(self._workspace.router_for(project).providers())
        overlap = local.intersection(route.provider for route in snapshot.routes)
        return sorted(PROVIDER_LABELS.get(provider, provider) for provider in overlap)

    def _connection_response(
        self, state: str, session: object | None
    ) -> CloudConnectionResponse:
        if session is None:
            return CloudConnectionResponse(
                state=state,
                identity=None,
                expires_at=None,
                accounts=[],
                capabilities=self._capabilities(state),
            )
        identity = CloudConnectionIdentity(
            id=session.user.id,
            display_name=session.user.display_name,
            email=session.user.email,
        )
        return CloudConnectionResponse(
            state=state,
            identity=identity,
            expires_at=session.expires_at,
            accounts=[self._account_summary(account) for account in session.accounts],
            capabilities=self._capabilities(state),
        )

    def _account_summary(self, account: CloudAccount) -> CloudConnectionAccountSummary:
        return CloudConnectionAccountSummary(
            id=account.id,
            name=account.name,
            kind=account.kind,
            eligible=True,
            eligibility_reason=None,
            balance_label=_money_label(account.available_micro),
            balance_url=(
                f"{self._client.origin}/funding-accounts?account_id={account.id}"
                if account.manage_billing
                else None
            ),
        )

    @staticmethod
    def _capabilities(state: str) -> CloudConnectionCapabilities:
        return CloudConnectionCapabilities(
            can_connect=state == "disconnected",
            can_reconnect=state == "expired",
            can_disconnect=state in {"connected", "expired"},
        )

    @staticmethod
    def _pending_response(
        pending: _PendingAuthorization,
    ) -> CloudDeviceAuthorizationStartResponse:
        return CloudDeviceAuthorizationStartResponse(
            id=pending.id,
            user_code=pending.user_code,
            verification_uri=pending.verification_uri,
            verification_uri_complete=pending.verification_uri_complete,
            expires_at=pending.expires_at.isoformat(),
            poll_interval_seconds=pending.poll_interval_seconds,
        )

    def _discard_expired_pending(self) -> None:
        now = self._wall_clock()
        expired = [
            key for key, value in self._pending.items() if value.expires_at <= now
        ]
        for key in expired:
            self._pending.pop(key, None)
        expired_completed = [
            key
            for key, (expires_at, _connection) in self._completed.items()
            if expires_at <= now
        ]
        for key in expired_completed:
            self._completed.pop(key, None)


__all__ = ["CloudConnectionService", "CloudConnectionServiceError"]
