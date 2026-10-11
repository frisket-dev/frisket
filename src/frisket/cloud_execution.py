"""Secret-free local port for account-authorized Cloud execution.

Media and LLM adapters receive this port after a routed attempt has already
fixed its payer.  They never receive the device bearer or Cloud origin and
cannot choose another account, lane, provider, or route revision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Literal,
    Mapping,
    Protocol,
    TypeAlias,
    runtime_checkable,
)

import httpx
from pydantic import ValidationError

from frisket.contracts.http.cloud_connection import (
    CloudDocumentConvertOptions,
    CloudDocumentExecutionMetadata,
    CloudErrorResponse,
    CloudExecutionResponse,
    CloudLLMExecutionRequest,
    CloudLLMRequest,
    CloudLane,
    CloudOCRExecutionMetadata,
    CloudOCROptions,
    CloudQuoteChallenge,
    CloudTranscriptionExecutionMetadata,
)
from frisket.contracts.transcription_sidecar import TranscriptionOptions

if TYPE_CHECKING:
    from frisket.ops.base import OpContext


CloudMediaCapability = Literal["ocr", "transcribe", "document.convert"]
CloudMediaOptions: TypeAlias = (
    CloudOCROptions | TranscriptionOptions | CloudDocumentConvertOptions
)


@dataclass(frozen=True, slots=True)
class CloudPayerSubject:
    """The exact Cloud payer and route admitted before effect."""

    user_id: int
    account_id: int
    lane: CloudLane
    provider: str
    engine: str
    route_revision: str

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or self.user_id < 1:
            raise ValueError("Cloud payer user_id must be a positive integer")
        if isinstance(self.account_id, bool) or self.account_id < 1:
            raise ValueError("Cloud payer account_id must be a positive integer")
        for name in ("provider", "engine", "route_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"Cloud payer {name} must be a clean string")

    def to_snapshot(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "account_id": self.account_id,
            "lane": self.lane,
            "provider": self.provider,
            "engine": self.engine,
            "route_revision": self.route_revision,
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> CloudPayerSubject:
        if set(value) != {
            "user_id",
            "account_id",
            "lane",
            "provider",
            "engine",
            "route_revision",
        }:
            raise ValueError("Cloud payer snapshot has unexpected fields")
        lane = value["lane"]
        if lane not in {"account_key", "frisket_credits"}:
            raise ValueError("Cloud payer lane is invalid")
        return cls(
            user_id=value["user_id"],
            account_id=value["account_id"],
            lane=lane,
            provider=value["provider"],
            engine=value["engine"],
            route_revision=value["route_revision"],
        )


@dataclass(frozen=True, slots=True)
class CloudQuoteAuthority:
    """Existing local consent that may answer one exact Cloud quote.

    Presence means the action/Ask gate already authorized this route.
    ``maximum_charge_micro=None`` is an explicitly authorized unbounded cost,
    distinct from an absent authority.  The transport still compares every
    quote payer/route field to :class:`CloudPayerSubject` before echoing it.
    """

    maximum_charge_micro: int | None

    def __post_init__(self) -> None:
        if self.maximum_charge_micro is not None and (
            isinstance(self.maximum_charge_micro, bool) or self.maximum_charge_micro < 0
        ):
            raise ValueError("Cloud quote ceiling must be non-negative")


@dataclass(frozen=True, slots=True)
class CloudExecutionAttempt:
    """Durable request identity plus the authority fixed by local admission.

    ``request_id`` is persisted before the first POST and reused for status
    recovery.  ``may_confirm_quote`` is true only when the existing action or
    Ask approval covers this exact route and spend; the transport must never
    infer it merely because Cloud returned a quote challenge.
    """

    request_id: str
    payer: CloudPayerSubject
    quote_authority: CloudQuoteAuthority | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or self.request_id != self.request_id.strip()
        ):
            raise ValueError("Cloud request_id must be a clean string")


@dataclass(frozen=True, slots=True)
class CloudMediaExecutionBinding:
    """The transport and authority injected into one media row call."""

    transport: CloudExecutionTransport
    attempt: CloudExecutionAttempt
    capability: CloudMediaCapability


@dataclass(frozen=True, slots=True)
class CloudMediaUpload:
    """One already-open upload without exposing its local path to Cloud."""

    file_name: str
    content_type: str
    stream: BinaryIO

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_name, str)
            or not self.file_name
            or self.file_name in {".", ".."}
            or "/" in self.file_name
            or "\\" in self.file_name
            or self.file_name != self.file_name.strip()
        ):
            raise ValueError("Cloud upload file_name must be a plain file name")
        if (
            not isinstance(self.content_type, str)
            or not self.content_type
            or self.content_type != self.content_type.strip()
            or "\r" in self.content_type
            or "\n" in self.content_type
        ):
            raise ValueError("Cloud upload content_type must be a clean string")
        if not callable(getattr(self.stream, "read", None)):
            raise TypeError("Cloud upload stream must be readable")


class CloudExecutionRefused(RuntimeError):
    """A typed pre-effect Cloud refusal safe to surface locally."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CloudConfirmationRequired(CloudExecutionRefused):
    """Cloud quoted exact terms that current local authority cannot confirm."""

    def __init__(self, challenge: CloudQuoteChallenge) -> None:
        super().__init__("needs_confirmation", "Cloud execution needs confirmation")
        self.challenge = challenge


class _CloudOutcomeUncertain(CloudExecutionRefused):
    """The create response was not observed, so the same key may be replayed."""

    def __init__(self) -> None:
        super().__init__(
            "cloud_unreachable", "Frisket Cloud is unavailable", retryable=True
        )


@runtime_checkable
class CloudExecutionTransport(Protocol):
    """Execute or recover one fixed Cloud purchase.

    Implementations own bearer authentication, exact multipart/JSON wire
    encoding, quote echo, bounded status polling, and GET recovery.  A POST
    with an uncertain outcome is recovered with ``request_id``; it is never
    changed into a fresh purchase.
    """

    async def execute_llm(
        self,
        attempt: CloudExecutionAttempt,
        request: CloudLLMRequest,
    ) -> CloudExecutionResponse: ...

    async def execute_media(
        self,
        attempt: CloudExecutionAttempt,
        *,
        capability: CloudMediaCapability,
        engine: str,
        options: CloudMediaOptions,
        uploads: Sequence[CloudMediaUpload],
    ) -> CloudExecutionResponse: ...

    async def recover(
        self, attempt: CloudExecutionAttempt, execution_id: str
    ) -> CloudExecutionResponse: ...

    async def cancel(
        self, attempt: CloudExecutionAttempt, execution_id: str
    ) -> CloudExecutionResponse: ...


class HttpCloudExecutionTransport:
    """Fixed-origin Cloud execution transport backed by installation storage."""

    _TERMINAL = frozenset(
        {"completed", "failed", "cancelled", "reconciliation_required"}
    )

    def __init__(
        self,
        store: Any,
        origin: str,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.5,
        max_poll_seconds: float = 30 * 60,
    ) -> None:
        from frisket.cloud_connection import validate_cloud_origin

        self._store = store
        self._origin = validate_cloud_origin(origin)
        self._http_transport = http_transport
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._max_poll_seconds = max_poll_seconds

    async def execute_llm(
        self,
        attempt: CloudExecutionAttempt,
        request: CloudLLMRequest,
    ) -> CloudExecutionResponse:
        self._require_engine(attempt, request.model)
        body = CloudLLMExecutionRequest(
            account_id=attempt.payer.account_id,
            route_revision=attempt.payer.route_revision,
            request=request,
        ).model_dump(mode="json", by_alias=True)

        async def send(confirmation: str | None) -> httpx.Response:
            return await self._request(
                "POST",
                "/api/desktop/v1/llm-executions",
                attempt=attempt,
                confirmation=confirmation,
                json=body,
            )

        response = await self._create(attempt, send)
        response = await self._await_terminal(attempt, response)
        return self._validate_response(attempt, response, "llm.complete")

    async def execute_media(
        self,
        attempt: CloudExecutionAttempt,
        *,
        capability: CloudMediaCapability,
        engine: str,
        options: CloudMediaOptions,
        uploads: Sequence[CloudMediaUpload],
    ) -> CloudExecutionResponse:
        self._require_engine(attempt, engine)
        upload_list = tuple(uploads)
        if not upload_list or (capability != "ocr" and len(upload_list) != 1):
            expected = (
                "one or more files" if capability == "ocr" else "exactly one file"
            )
            raise CloudExecutionRefused(
                "invalid_request", f"Cloud {capability} requires {expected}"
            )
        metadata = self._media_metadata(attempt, capability, engine, options)
        positions = [self._stream_position(upload) for upload in upload_list]

        async def send(confirmation: str | None) -> httpx.Response:
            for upload, position in zip(upload_list, positions, strict=True):
                upload.stream.seek(position)
            return await self._request(
                "POST",
                "/api/desktop/v1/media-executions",
                attempt=attempt,
                confirmation=confirmation,
                data={
                    "metadata": metadata.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                },
                files=[
                    (
                        "files",
                        (upload.file_name, upload.stream, upload.content_type),
                    )
                    for upload in upload_list
                ],
            )

        response = await self._create(attempt, send)
        response = await self._await_terminal(attempt, response)
        return self._validate_response(attempt, response, capability)

    async def recover(
        self, attempt: CloudExecutionAttempt, execution_id: str
    ) -> CloudExecutionResponse:
        response = await self._request(
            "GET",
            self._execution_path(execution_id),
            attempt=attempt,
        )
        return self._validate_response(attempt, self._parse_execution(response))

    async def cancel(
        self, attempt: CloudExecutionAttempt, execution_id: str
    ) -> CloudExecutionResponse:
        response = await self._request(
            "DELETE",
            self._execution_path(execution_id),
            attempt=attempt,
        )
        return self._validate_response(attempt, self._parse_execution(response))

    async def _create(
        self, attempt: CloudExecutionAttempt, send: Any
    ) -> CloudExecutionResponse:
        response = await self._retry_uncertain_create(send, None)
        if response.status_code == 409:
            if self._error_code(response) != "needs_confirmation":
                self._raise_error(response)
            challenge = self._parse_quote(response)
            authority = attempt.quote_authority
            if authority is None or not self._quote_is_authorized(
                attempt, challenge, authority
            ):
                raise CloudConfirmationRequired(challenge)
            response = await self._retry_uncertain_create(send, challenge.confirmation)
        return self._parse_execution(response)

    async def _retry_uncertain_create(
        self, send: Any, confirmation: str | None
    ) -> httpx.Response:
        last_error: _CloudOutcomeUncertain | None = None
        for _attempt_number in range(2):
            try:
                return await send(confirmation)
            except _CloudOutcomeUncertain as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _await_terminal(
        self, attempt: CloudExecutionAttempt, response: CloudExecutionResponse
    ) -> CloudExecutionResponse:
        if response.status in self._TERMINAL:
            return response
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_poll_seconds
        current = response
        while current.status not in self._TERMINAL:
            if loop.time() >= deadline:
                raise CloudExecutionRefused(
                    "cloud_execution_pending",
                    "Cloud execution is still running",
                    retryable=True,
                )
            await asyncio.sleep(self._poll_interval)
            current = await self.recover(attempt, current.execution_id)
        return current

    async def _request(
        self,
        method: str,
        path: str,
        *,
        attempt: CloudExecutionAttempt,
        confirmation: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        token = self._access_token(attempt)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": attempt.request_id,
        }
        if confirmation is not None:
            headers["X-Frisket-Confirmation"] = confirmation
        try:
            async with httpx.AsyncClient(
                base_url=self._origin,
                transport=self._http_transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise _CloudOutcomeUncertain() from exc
        if response.is_redirect:
            raise CloudExecutionRefused(
                "cloud_redirect_refused", "Frisket Cloud returned an unsafe redirect"
            )
        if response.status_code in {200, 202, 409}:
            return response
        self._raise_error(response)
        raise AssertionError("Cloud error projection returned")

    def _access_token(self, attempt: CloudExecutionAttempt) -> str:
        from frisket.cloud_connection import CloudConnectionStoreError

        try:
            connection = self._store.load().connection
        except CloudConnectionStoreError as exc:
            raise CloudExecutionRefused(
                "connection_expired", "Reconnect Frisket Cloud"
            ) from exc
        if connection is None:
            raise CloudExecutionRefused(
                "connection_expired", "Connect Frisket Cloud before running"
            )
        try:
            expires_at = datetime.fromisoformat(
                connection.session.expires_at.replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise CloudExecutionRefused(
                "connection_expired", "Reconnect Frisket Cloud"
            ) from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        payer = attempt.payer
        if expires_at.astimezone(UTC) <= datetime.now(UTC):
            raise CloudExecutionRefused("connection_expired", "Reconnect Frisket Cloud")
        if connection.session.user.id != payer.user_id:
            raise CloudExecutionRefused(
                "account_forbidden",
                "The signed-in Cloud user differs from the admitted payer",
            )
        if not any(
            account.id == payer.account_id for account in connection.session.accounts
        ):
            raise CloudExecutionRefused(
                "account_forbidden",
                "The admitted Cloud account is no longer available",
            )
        return connection.access_token

    @staticmethod
    def _parse_execution(response: httpx.Response) -> CloudExecutionResponse:
        try:
            return CloudExecutionResponse.model_validate(response.json())
        except (ValueError, TypeError, ValidationError) as exc:
            raise CloudExecutionRefused(
                "cloud_protocol_error", "Frisket Cloud returned an invalid execution"
            ) from exc

    @staticmethod
    def _parse_quote(response: httpx.Response) -> CloudQuoteChallenge:
        try:
            return CloudQuoteChallenge.model_validate(response.json())
        except (ValueError, TypeError, ValidationError) as exc:
            raise CloudExecutionRefused(
                "cloud_protocol_error", "Frisket Cloud returned an invalid quote"
            ) from exc

    @staticmethod
    def _error_code(response: httpx.Response) -> object:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return None
        return payload.get("error") if isinstance(payload, dict) else None

    @staticmethod
    def _quote_is_authorized(
        attempt: CloudExecutionAttempt,
        challenge: CloudQuoteChallenge,
        authority: CloudQuoteAuthority,
    ) -> bool:
        payer = attempt.payer
        quote = challenge.quote
        if (
            quote.account_id != payer.account_id
            or quote.route_revision != payer.route_revision
            or quote.lane != payer.lane
            or quote.provider != payer.provider
            or quote.engine != payer.engine
        ):
            return False
        ceiling = authority.maximum_charge_micro
        charge = quote.estimated_charge_micro
        return ceiling is None or (charge is not None and charge <= ceiling)

    @staticmethod
    def _media_metadata(
        attempt: CloudExecutionAttempt,
        capability: CloudMediaCapability,
        engine: str,
        options: CloudMediaOptions,
    ) -> Any:
        common = {
            "account_id": attempt.payer.account_id,
            "route_revision": attempt.payer.route_revision,
            "capability": capability,
            "engine": engine,
            "options": options,
        }
        if capability == "ocr" and isinstance(options, CloudOCROptions):
            return CloudOCRExecutionMetadata(**common)
        if capability == "transcribe" and isinstance(options, TranscriptionOptions):
            return CloudTranscriptionExecutionMetadata(**common)
        if capability == "document.convert" and isinstance(
            options, CloudDocumentConvertOptions
        ):
            return CloudDocumentExecutionMetadata(**common)
        raise CloudExecutionRefused(
            "invalid_request", "Cloud media options do not match the capability"
        )

    @staticmethod
    def _stream_position(upload: CloudMediaUpload) -> int:
        if not callable(getattr(upload.stream, "seek", None)) or not callable(
            getattr(upload.stream, "tell", None)
        ):
            raise CloudExecutionRefused(
                "invalid_request", "Cloud upload stream must be seekable"
            )
        try:
            return int(upload.stream.tell())
        except (OSError, ValueError) as exc:
            raise CloudExecutionRefused(
                "invalid_request", "Cloud upload stream position is unavailable"
            ) from exc

    @staticmethod
    def _require_engine(attempt: CloudExecutionAttempt, engine: str) -> None:
        if engine != attempt.payer.engine:
            raise CloudExecutionRefused(
                "route_changed", "Cloud request engine differs from its admitted route"
            )

    @staticmethod
    def _validate_response(
        attempt: CloudExecutionAttempt,
        response: CloudExecutionResponse,
        capability: str | None = None,
    ) -> CloudExecutionResponse:
        payer = attempt.payer
        route = response.route
        if (
            (capability is not None and route.capability != capability)
            or route.engine != payer.engine
            or route.provider != payer.provider
            or route.lane != payer.lane
            or route.route_revision != payer.route_revision
        ):
            raise CloudExecutionRefused(
                "route_changed", "Cloud response differs from the admitted route"
            )
        receipt = response.receipt
        if receipt is not None and (
            receipt.user_id != payer.user_id
            or receipt.account_id != payer.account_id
            or receipt.engine != payer.engine
            or receipt.provider != payer.provider
            or receipt.lane != payer.lane
        ):
            raise CloudExecutionRefused(
                "route_changed", "Cloud response differs from the admitted payer"
            )
        return response

    @staticmethod
    def _execution_path(execution_id: str) -> str:
        if (
            not execution_id
            or execution_id != execution_id.strip()
            or "/" in execution_id
            or "\\" in execution_id
        ):
            raise CloudExecutionRefused(
                "invalid_request", "Cloud execution id is invalid"
            )
        return f"/api/desktop/v1/executions/{execution_id}"

    @staticmethod
    def _raise_error(response: httpx.Response) -> None:
        try:
            error = CloudErrorResponse.model_validate(response.json())
        except (ValueError, TypeError, ValidationError):
            raise CloudExecutionRefused(
                "cloud_unavailable",
                "Frisket Cloud could not complete the request",
                retryable=response.status_code >= 500,
            ) from None
        raise CloudExecutionRefused(
            error.error,
            error.message,
            retryable=error.retryable or response.status_code >= 500,
        )


def cloud_media_execution(
    ctx: OpContext,
    capability: CloudMediaCapability,
    engine: str,
) -> tuple[CloudExecutionTransport, CloudExecutionAttempt]:
    """Return the exact Cloud media binding injected by the runner."""

    binding = ctx.cloud_execution
    if not isinstance(binding, CloudMediaExecutionBinding):
        raise CloudExecutionRefused(
            "cloud_execution_unbound",
            "Cloud media execution requires an admitted paid attempt",
        )
    if binding.capability != capability:
        raise CloudExecutionRefused(
            "route_changed", "Cloud media capability differs from its admitted route"
        )
    if binding.attempt.payer.engine != engine:
        raise CloudExecutionRefused(
            "route_changed", "Cloud media engine differs from its admitted route"
        )
    return binding.transport, binding.attempt


def cloud_execution_accounting(
    attempt: CloudExecutionAttempt,
    response: CloudExecutionResponse,
    *,
    capability: str,
) -> dict[str, Any]:
    """Project one server receipt into the existing local accounting envelope.

    The Cloud charge remains a server-authored user charge in ``units`` and
    the top-level result cost.  ``provider_cost_usd`` carries only the
    provider cost when Cloud supplied it, so an unknown provider cost never
    becomes a fabricated zero and local code never bills the account again.
    """

    receipt = response.receipt
    if receipt is None:
        raise CloudExecutionRefused(
            "cloud_receipt_missing", "Cloud execution returned no accounting receipt"
        )
    payer = attempt.payer
    route = response.route
    if (
        receipt.user_id != payer.user_id
        or receipt.account_id != payer.account_id
        or receipt.lane != payer.lane
        or receipt.provider != payer.provider
        or receipt.engine != payer.engine
        or route.lane != payer.lane
        or route.provider != payer.provider
        or route.engine != payer.engine
        or route.route_revision != payer.route_revision
        or route.capability != capability
    ):
        raise CloudExecutionRefused(
            "route_changed", "Cloud receipt differs from the admitted payer route"
        )
    usage = receipt.usage
    if usage.kind != capability:
        raise CloudExecutionRefused(
            "route_changed", "Cloud receipt usage differs from the admitted capability"
        )

    from frisket.ai.models.metadata import ModelCallMeta

    provider_cost = (
        receipt.provider_cost_micro / 1_000_000
        if receipt.provider_cost_micro is not None
        else None
    )
    units = usage.model_dump(mode="json", exclude={"kind"})
    units.update(
        {
            "requests": 1,
            "cloud_charge_micro": receipt.charge_micro,
            "cloud_user_id": receipt.user_id,
            "cloud_account_id": receipt.account_id,
            "cloud_lane": receipt.lane,
        }
    )
    fact = ModelCallMeta(
        id=response.execution_id,
        capability=capability,
        engine=receipt.engine,
        provider=receipt.provider,
        provider_kind="platform_api",
        model_ids=[receipt.engine],
        credential_source=(
            "org_byok" if receipt.lane == "account_key" else "platform_key"
        ),
        provider_reported_cost_usd=provider_cost,
        provider_cost_usd=provider_cost,
        cost_source="provider_reported" if provider_cost is not None else "unknown",
        units=units,
        request_id=response.execution_id,
        warnings=(
            [] if provider_cost is not None else ["Cloud provider cost is unknown"]
        ),
        duration_ms=None,
    ).as_dict()
    return {
        "cost": receipt.charge_micro / 1_000_000,
        "cost_source": "cloud_receipt",
        "model_calls": [fact],
        "cloud_receipt": receipt.model_dump(mode="json"),
    }
