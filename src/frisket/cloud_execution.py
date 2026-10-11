"""Secret-free local port for account-authorized Cloud execution.

Media and LLM adapters receive this port after a routed attempt has already
fixed its payer.  They never receive the device bearer or Cloud origin and
cannot choose another account, lane, provider, or route revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
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

from frisket.contracts.http.cloud_connection import (
    CloudDocumentConvertOptions,
    CloudExecutionResponse,
    CloudLLMRequest,
    CloudLane,
    CloudOCROptions,
    CloudQuoteChallenge,
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
            isinstance(self.maximum_charge_micro, bool)
            or self.maximum_charge_micro < 0
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
        self, attempt: CloudExecutionAttempt
    ) -> CloudExecutionResponse: ...

    async def cancel(
        self, attempt: CloudExecutionAttempt
    ) -> CloudExecutionResponse: ...


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
        warnings=([] if provider_cost is not None else ["Cloud provider cost is unknown"]),
        duration_ms=None,
    ).as_dict()
    return {
        "cost": receipt.charge_micro / 1_000_000,
        "cost_source": "cloud_receipt",
        "model_calls": [fact],
        "cloud_receipt": receipt.model_dump(mode="json"),
    }
